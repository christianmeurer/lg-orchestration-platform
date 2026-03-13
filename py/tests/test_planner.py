from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from lg_orch.nodes.planner import _classify_intent, planner

planner_module = importlib.import_module("lg_orch.nodes.planner")


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"request": "test request", "repo_context": {}}
    s.update(overrides)
    return s


# --- _classify_intent tests ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix the login bug", "code_change"),
        ("implement dark mode", "code_change"),
        ("add a new button", "code_change"),
        ("change the color", "code_change"),
        ("refactor the module", "code_change"),
        ("why does this fail", "question"),
        ("how does auth work", "question"),
        ("what is this function", "question"),
        ("explain the architecture", "question"),
        ("research best practices", "research"),
        ("latest updates on React", "research"),
        ("compare frameworks", "research"),
        ("survey existing solutions", "research"),
        ("debug the crash", "debug"),
        ("stack trace analysis", "debug"),
        ("error in production", "debug"),
        ("panic in the handler", "debug"),
        ("exception thrown here", "debug"),
        ("summarize the repo", "analysis"),
        ("show me the stats", "analysis"),
        ("list all files", "analysis"),
    ],
)
def test_classify_intent(text: str, expected: str) -> None:
    assert _classify_intent(text) == expected


def test_classify_intent_case_insensitive() -> None:
    assert _classify_intent("FIX THIS BUG") == "code_change"
    assert _classify_intent("EXPLAIN why") == "question"


def test_classify_intent_empty_string() -> None:
    assert _classify_intent("") == "analysis"


def test_classify_intent_priority_code_change_over_debug() -> None:
    # "fix" matches code_change first, even though "error" could match debug
    assert _classify_intent("fix the error") == "code_change"


# --- planner node tests ---


def test_planner_sets_intent() -> None:
    out = planner(_base_state(request="fix the bug"))
    assert out["intent"] == "code_change"


def test_planner_creates_plan() -> None:
    out = planner(_base_state())
    plan = out["plan"]
    assert isinstance(plan, dict)
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["id"] == "step-1"
    assert plan["rollback"] == "No changes were made."


def test_planner_plan_has_tool_calls() -> None:
    out = planner(_base_state())
    tools = out["plan"]["steps"][0]["tools"]
    assert len(tools) == 2
    assert tools[0]["tool"] == "list_files"
    assert tools[1]["tool"] == "search_files"


def test_planner_pdf_request_prefers_read_file_in_deterministic_plan() -> None:
    out = planner(_base_state(request='Read "FLUX LoRA Training Guide.pdf" and implement'))
    tools = out["plan"]["steps"][0]["tools"]
    assert len(tools) == 2
    assert tools[0]["tool"] == "list_files"
    assert tools[1]["tool"] == "read_file"
    assert tools[1]["input"]["path"] == "FLUX LoRA Training Guide.pdf"


def test_planner_creates_trace_events() -> None:
    out = planner(_base_state())
    events = out.get("_trace_events", [])
    names = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "planner" in names


def test_planner_preserves_state() -> None:
    out = planner(_base_state(repo_context={"test": True}))
    assert out["repo_context"]["test"] is True


def test_planner_clears_reset_flags_after_context_reset() -> None:
    out = planner(
        _base_state(
            context_reset_requested=True,
            plan_discarded=True,
            plan_discard_reason="architecture_mismatch_detected",
            retry_target="context_builder",
            facts=[{"x": 1}],
            plan={"steps": [{"id": "old"}]},
        )
    )
    assert out["context_reset_requested"] is False
    assert out["plan_discarded"] is False
    assert out["plan_discard_reason"] == ""
    assert out["retry_target"] is None
    assert out["facts"] == []
    assert len(out["plan"]["steps"]) == 1


def test_planner_carries_recovery_packet_into_plan() -> None:
    recovery_packet = {
        "failure_class": "verification_failed",
        "failure_fingerprint": "fp-1",
        "rationale": "retry planning with the latest failure context",
        "retry_target": "planner",
        "context_scope": "working_set",
        "plan_action": "keep",
        "loop": 1,
        "origin": "verifier",
        "summary": "verification_failed: test assertion failed",
        "last_check": "test assertion failed",
        "discard_reason": "",
    }
    out = planner(
        _base_state(
            verification={"recovery_packet": recovery_packet},
            recovery_packet=recovery_packet,
        )
    )
    assert out["plan"]["recovery_packet"]["failure_fingerprint"] == "fp-1"
    assert out["plan"]["recovery"]["failure_fingerprint"] == "fp-1"


def test_planner_applies_pre_verification_sliding_window() -> None:
    results = [
        {
            "tool": "search_files",
            "ok": True,
            "exit_code": 0,
            "stdout": f"out-{idx}",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 0,
            "artifacts": {},
        }
        for idx in range(12)
    ]
    out = planner(
        _base_state(
            history_policy={"retain_recent_tool_results": 6},
            tool_results=results,
            provenance=[],
        )
    )
    assert len(out["tool_results"]) == 6
    assert out["tool_results"][0]["stdout"] == "out-6"
    assert out["provenance"][-1]["event"] == "tool_result_window_trim"


def test_planner_records_model_routing_telemetry() -> None:
    out = planner(
        _base_state(
            _models={
                "planner": {
                    "provider": "remote_openai",
                    "model": "gpt-4.1",
                    "temperature": 0.0,
                }
            },
            _model_routing_policy={
                "local_provider": "local",
                "fallback_task_classes": ["context_condensation"],
            },
            telemetry={},
        )
    )
    routes = out.get("telemetry", {}).get("model_routing", [])
    assert len(routes) >= 1
    assert routes[-1]["node"] == "planner"
    assert routes[-1]["task_class"] == "context_condensation"


def test_planner_uses_remote_model_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeInferenceClient:
        def __init__(self, *, base_url: str, api_key: str, timeout_s: int = 60) -> None:
            self.base_url = base_url
            self.api_key = api_key
            self.timeout_s = timeout_s

        def close(self) -> None:
            return None

        def chat_completion(
            self,
            *,
            model: str,
            system_prompt: str,
            user_prompt: str,
            temperature: float,
            max_tokens: int = 1200,
        ) -> str:
            assert model == "anthropic-claude-3.5-haiku"
            assert "planner_output.schema.json" in user_prompt
            assert max_tokens >= 1200
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "remote-step-1",
                            "description": "Inspect changed files.",
                            "tools": [
                                {
                                    "tool": "list_files",
                                    "input": {"path": ".", "recursive": False},
                                }
                            ],
                            "expected_outcome": "Changed files listed.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                }
            )

    monkeypatch.setattr(planner_module, "InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="inspect repository",
            _repo_root=".",
            _models={
                "planner": {
                    "provider": "remote_digitalocean",
                    "model": "anthropic-claude-3.5-haiku",
                    "temperature": 0.1,
                }
            },
            _model_provider_runtime={
                "digitalocean": {
                    "base_url": "https://inference.do-ai.run/v1",
                    "api_key": "test-key",
                    "timeout_s": 30,
                }
            },
        )
    )

    assert out["plan"]["steps"][0]["id"] == "remote-step-1"
    assert out["plan"]["rollback"] == "No rollback needed."


def test_planner_injects_mcp_context_into_remote_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class _FakeInferenceClient:
        def __init__(self, *, base_url: str, api_key: str, timeout_s: int = 60) -> None:
            self.base_url = base_url
            self.api_key = api_key
            self.timeout_s = timeout_s

        def close(self) -> None:
            return None

        def chat_completion(
            self,
            *,
            model: str,
            system_prompt: str,
            user_prompt: str,
            temperature: float,
            max_tokens: int = 1200,
        ) -> str:
            captured["user_prompt"] = user_prompt
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "remote-step-1",
                            "description": "Inspect MCP tools.",
                            "tools": [
                                {
                                    "tool": "list_files",
                                    "input": {"path": ".", "recursive": False},
                                }
                            ],
                            "expected_outcome": "MCP-aware plan generated.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                }
            )

    monkeypatch.setattr(planner_module, "InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="inspect available MCP tools",
            _repo_root=".",
            repo_context={
                "mcp_catalog": "mock.echo - Echoes text back to the caller.",
                "mcp_capabilities": {"server_count": 1, "tool_count": 1},
            },
            _models={
                "planner": {
                    "provider": "remote_digitalocean",
                    "model": "anthropic-claude-3.5-haiku",
                    "temperature": 0.1,
                }
            },
            _model_provider_runtime={
                "digitalocean": {
                    "base_url": "https://inference.do-ai.run/v1",
                    "api_key": "test-key",
                    "timeout_s": 30,
                }
            },
        )
    )

    assert out["plan"]["steps"][0]["id"] == "remote-step-1"
    assert "mcp_catalog:" in captured["user_prompt"]
    assert "mock.echo" in captured["user_prompt"]
    assert "mcp_capabilities:" in captured["user_prompt"]
    assert "mcp_recovery_hints:" not in captured["user_prompt"]


def test_planner_remote_model_prompt_includes_mcp_recovery_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class _FakeInferenceClient:
        def __init__(self, *, base_url: str, api_key: str, timeout_s: int = 60) -> None:
            self.base_url = base_url
            self.api_key = api_key
            self.timeout_s = timeout_s

        def close(self) -> None:
            return None

        def chat_completion(
            self,
            *,
            model: str,
            system_prompt: str,
            user_prompt: str,
            temperature: float,
            max_tokens: int = 1200,
        ) -> str:
            captured["user_prompt"] = user_prompt
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "remote-step-1",
                            "description": "Inspect MCP tools.",
                            "tools": [],
                            "expected_outcome": "MCP-aware plan generated.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                }
            )

    monkeypatch.setattr(planner_module, "InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="inspect available MCP tools",
            _repo_root=".",
            repo_context={
                "mcp_catalog": "mock.echo - Echoes text back to the caller.",
                "mcp_capabilities": {"server_count": 1, "tool_count": 1},
                "mcp_recovery_hints": "candidate_tools: mock.echo",
                "mcp_relevant_tools": [{"server_name": "mock", "name": "echo"}],
            },
            _models={
                "planner": {
                    "provider": "remote_digitalocean",
                    "model": "anthropic-claude-3.5-haiku",
                    "temperature": 0.1,
                }
            },
            _model_provider_runtime={
                "digitalocean": {
                    "base_url": "https://inference.do-ai.run/v1",
                    "api_key": "test-key",
                    "timeout_s": 30,
                }
            },
        )
    )
    assert out["plan"]["steps"][0]["id"] == "remote-step-1"
    assert "mcp_recovery_hints:" in captured["user_prompt"]
    assert "mcp_relevant_tools:" in captured["user_prompt"]


def test_planner_remote_failure_falls_back_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingInferenceClient:
        def __init__(self, *, base_url: str, api_key: str, timeout_s: int = 60) -> None:
            self.base_url = base_url
            self.api_key = api_key
            self.timeout_s = timeout_s

        def close(self) -> None:
            return None

        def chat_completion(
            self,
            *,
            model: str,
            system_prompt: str,
            user_prompt: str,
            temperature: float,
            max_tokens: int = 1200,
        ) -> str:
            raise RuntimeError("remote failed")

    monkeypatch.setattr(planner_module, "InferenceClient", _RaisingInferenceClient)
    out = planner(
        _base_state(
            request="inspect repository",
            _repo_root=".",
            _models={
                "planner": {
                    "provider": "remote_digitalocean",
                    "model": "anthropic-claude-3.5-haiku",
                    "temperature": 0.1,
                }
            },
            _model_provider_runtime={
                "digitalocean": {
                    "base_url": "https://inference.do-ai.run/v1",
                    "api_key": "test-key",
                    "timeout_s": 30,
                }
            },
        )
    )

    assert out["plan"]["steps"][0]["id"] == "step-1"
    assert "deterministic fallback used" in out["plan"]["rollback"]
