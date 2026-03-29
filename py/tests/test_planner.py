from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from lg_orch.nodes._planner_prompt import _default_plan, _format_mcp_tool_catalog
from lg_orch.nodes.planner import (
    _classify_intent,
    _planner_model_output,
    planner,
)

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


def test_planner_code_change_default_plan_sets_coder_handoff() -> None:
    out = planner(_base_state(request="implement dark mode"))
    handoff = out["plan"]["steps"][0]["handoff"]
    assert handoff["producer"] == "planner"
    assert handoff["consumer"] == "coder"
    assert out["active_handoff"]["consumer"] == "coder"


def test_planner_shapes_constraints_from_semantic_memory() -> None:
    out = planner(
        _base_state(
            request="implement approval-safe resume flow",
            repo_context={
                "semantic_memories": [
                    {
                        "kind": "approval_history",
                        "source": "approved",
                        "summary": (
                            "approved apply patch for py/src/lg_orch/remote_api.py"
                            " with checkpoint resume"
                        ),
                        "created_at": "2026-03-18T00:00:00Z",
                    },
                    {
                        "kind": "loop_summary",
                        "source": "verification_failed",
                        "summary": (
                            "previous retry failed in py/src/lg_orch/remote_api.py"
                            " until checkpoint handling was added"
                        ),
                        "created_at": "2026-03-17T00:00:00Z",
                    },
                ]
            },
        )
    )
    step = out["plan"]["steps"][0]
    assert "py/src/lg_orch/remote_api.py" in step["files_touched"]
    assert any(
        criterion
        == ("Approval-sensitive changes preserve checkpoint-backed resume and auditability.")
        for criterion in out["plan"]["acceptance_criteria"]
    )
    handoff = step["handoff"]
    assert any(
        constraint
        == ("Preserve approval and checkpoint compatibility for approval-sensitive mutations.")
        for constraint in handoff["constraints"]
    )
    assert any(entry["kind"] == "semantic_memory" for entry in handoff["evidence"])


def test_planner_uses_cached_procedure_to_shape_verification_and_handoff() -> None:
    out = planner(
        _base_state(
            request="implement fix and run the tests and verify",
            repo_context={
                "cached_procedures": [
                    {
                        "procedure_id": "proc-1",
                        "canonical_name": "run_tests_check_output",
                        "task_class": "testing",
                        "steps": [{"id": "s1", "tools": [{"tool": "run_tests"}]}],
                        "verification": [{"tool": "exec", "input": {"cmd": "pytest", "args": []}}],
                        "use_count": 4,
                        "created_at": "2026-03-18T00:00:00Z",
                    }
                ]
            },
        )
    )
    assert out["plan"]["verification"][0]["tool"] == "exec"
    assert any(
        criterion
        == (
            "Validated procedure memory 'run_tests_check_output' is reused"
            " when compatible with current evidence."
        )
        for criterion in out["plan"]["acceptance_criteria"]
    )
    handoff = out["plan"]["steps"][0]["handoff"]
    assert any(entry["kind"] == "procedure_cache" for entry in handoff["evidence"])
    assert any(
        constraint
        == (
            "Prefer the validated cached procedure 'run_tests_check_output'"
            " when it remains compatible with current evidence."
        )
        for constraint in handoff["constraints"]
    )


def test_planner_records_use_of_selected_cached_procedure(tmp_path: Path) -> None:
    from lg_orch.procedure_cache import ProcedureCache

    db_path = tmp_path / "procedures.sqlite"
    cache = ProcedureCache(db_path=db_path)
    try:
        procedure_id = cache.store_procedure(
            canonical_name="run_tests_check_output",
            request="implement fix and run the tests and verify",
            task_class="testing",
            steps=[{"id": "s1", "tools": [{"tool": "run_tests"}]}],
            verification=[{"tool": "exec", "input": {"cmd": "pytest", "args": []}}],
            created_at="2026-03-18T00:00:00Z",
        )
    finally:
        cache.close()

    out = planner(
        _base_state(
            request="implement fix and run the tests and verify",
            _procedure_cache_path=str(db_path),
            repo_context={
                "cached_procedures": [
                    {
                        "procedure_id": procedure_id,
                        "canonical_name": "run_tests_check_output",
                        "task_class": "testing",
                        "steps": [{"id": "s1", "tools": [{"tool": "run_tests"}]}],
                        "verification": [{"tool": "exec", "input": {"cmd": "pytest", "args": []}}],
                        "use_count": 0,
                        "created_at": "2026-03-18T00:00:00Z",
                    }
                ]
            },
        )
    )
    assert out["plan"]["verification"][0]["tool"] == "exec"

    cache = ProcedureCache(db_path=db_path)
    try:
        results = cache.lookup_procedure(request="implement fix and run the tests and verify")
        assert results[0]["use_count"] == 1
    finally:
        cache.close()


def test_planner_remote_model_prompt_includes_procedural_memory_recall(
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
                            "description": "Use procedural memory.",
                            "tools": [],
                            "expected_outcome": "Procedure-aware plan generated.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="implement fix and run the tests and verify",
            _repo_root=".",
            repo_context={
                "cached_procedures": [
                    {
                        "procedure_id": "proc-1",
                        "canonical_name": "run_tests_check_output",
                        "task_class": "testing",
                        "steps": [{"id": "s1", "tools": [{"tool": "run_tests"}]}],
                        "verification": [{"tool": "exec", "input": {"cmd": "pytest", "args": []}}],
                        "use_count": 4,
                        "created_at": "2026-03-18T00:00:00Z",
                    }
                ]
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
    assert "procedural_memory_recall:" in captured["user_prompt"]
    assert "run_tests_check_output" in captured["user_prompt"]


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
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
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
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
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
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
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


def test_planner_remote_model_prompt_includes_ranked_semantic_memories(
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
                            "description": "Use semantic memory.",
                            "tools": [],
                            "expected_outcome": "Semantic-memory-aware plan generated.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="resume the approved patch flow",
            _repo_root=".",
            repo_context={
                "semantic_memories": [
                    {
                        "kind": "approval_history",
                        "source": "approved",
                        "summary": "approved apply patch resume from checkpoint",
                        "created_at": "2026-03-18T00:00:00Z",
                    },
                    {
                        "kind": "loop_summary",
                        "source": "verification_failed",
                        "summary": "lint failed after patch",
                        "created_at": "2026-03-17T00:00:00Z",
                    },
                ],
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
    assert "semantic_memory_recall:" in captured["user_prompt"]
    assert "approved apply patch resume from checkpoint" in captured["user_prompt"]


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

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _RaisingInferenceClient)
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


def test_planner_model_output_rejects_non_http_base_url() -> None:
    state: dict[str, Any] = {
        "request": "do something",
        "_models": {
            "planner": {
                "provider": "openai_compatible",
                "model": "gpt-4o",
                "temperature": 0.0,
            }
        },
        "_model_provider_runtime": {
            "openai_compatible": {
                "api_key": "sk-test",
                "base_url": "ftp://bad",
                "timeout_s": 60,
            }
        },
    }
    route_decision: dict[str, Any] = {"provider_used": "openai_compatible"}
    result = _planner_model_output(state, route_decision=route_decision)
    assert result == (None, None)


# --- Gap 4: _format_mcp_tool_catalog unit tests ---


def test_format_mcp_tool_catalog_empty_returns_empty_string() -> None:
    assert _format_mcp_tool_catalog([]) == ""


def test_format_mcp_tool_catalog_skips_entries_without_name() -> None:
    result = _format_mcp_tool_catalog([{"description": "no name here"}])
    assert result == ""


def test_format_mcp_tool_catalog_basic_tools() -> None:
    tools: list[dict[str, Any]] = [
        {"name": "read_file", "description": "Reads a file.", "server_name": "fs"},
        {"name": "run_tests", "description": "Runs the test suite.", "server_name": "fs"},
    ]
    result = _format_mcp_tool_catalog(tools)
    assert result.startswith("## Available MCP Tools")
    assert "`read_file`" in result
    assert "Reads a file." in result
    assert "`run_tests`" in result
    assert "Runs the test suite." in result


def test_format_mcp_tool_catalog_includes_input_schema_properties() -> None:
    tools: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": "Reads a file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "encoding": {"type": "string"},
                },
            },
        }
    ]
    result = _format_mcp_tool_catalog(tools)
    assert "Input schema:" in result
    assert "path" in result
    assert "string" in result


def test_format_mcp_tool_catalog_omits_schema_when_no_properties() -> None:
    tools: list[dict[str, Any]] = [
        {
            "name": "ping",
            "description": "Pings the server.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    result = _format_mcp_tool_catalog(tools)
    assert "## Available MCP Tools" in result
    assert "Input schema:" not in result


def test_format_mcp_tool_catalog_ignores_non_dict_entries() -> None:
    tools: list[dict[str, Any]] = [
        {"name": "valid", "description": "Valid tool."},
    ]
    # Cast to appease mypy — runtime handles non-dict gracefully
    mixed: list[dict[str, Any]] = tools  # type: ignore[assignment]
    result = _format_mcp_tool_catalog(mixed)
    assert "`valid`" in result


# --- Gap 4: planner injects ## Available MCP Tools from state["mcp_tools"] ---


def test_planner_injects_mcp_tool_catalog_from_mcp_tools_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When state['mcp_tools'] is non-empty, the planner injects the
    ## Available MCP Tools block into the user prompt sent to the model."""
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
                            "id": "mcp-step-1",
                            "description": "Use discovered MCP tools.",
                            "tools": [],
                            "expected_outcome": "MCP tools referenced in plan.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
    out = planner(
        _base_state(
            request="apply a patch using available tools",
            _repo_root=".",
            mcp_tools=[
                {
                    "name": "read_file",
                    "description": "Reads a file from the workspace.",
                    "server_name": "fs",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
                {
                    "name": "run_tests",
                    "description": "Runs the test suite.",
                    "server_name": "fs",
                    "inputSchema": None,
                },
            ],
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

    assert out["plan"]["steps"][0]["id"] == "mcp-step-1"
    user_prompt = captured["user_prompt"]
    assert "## Available MCP Tools" in user_prompt
    assert "`read_file`" in user_prompt
    assert "Reads a file from the workspace." in user_prompt
    assert "`run_tests`" in user_prompt
    # Schema properties are emitted for tools that have them
    assert "path" in user_prompt


def test_planner_omits_mcp_tool_catalog_block_when_mcp_tools_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When state['mcp_tools'] is [] the ## Available MCP Tools block must NOT appear."""
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
                            "id": "empty-mcp-step-1",
                            "description": "No MCP tools in context.",
                            "tools": [],
                            "expected_outcome": "Plan generated without MCP catalog.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
    planner(
        _base_state(
            request="inspect repository",
            _repo_root=".",
            mcp_tools=[],
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

    assert "## Available MCP Tools" not in captured["user_prompt"]


def test_planner_mcp_tool_catalog_uses_runtime_tools_not_hardcoded_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool names in the catalog are derived from state['mcp_tools'], never hard-coded."""
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
                            "id": "dynamic-step-1",
                            "description": "Dynamic tools referenced.",
                            "tools": [],
                            "expected_outcome": "Catalog block contains only runtime tools.",
                            "files_touched": [],
                        }
                    ],
                    "verification": [],
                    "rollback": "No rollback needed.",
                    "acceptance_criteria": ["Plan executed successfully."],
                    "max_iterations": 1,
                }
            )

    monkeypatch.setattr("lg_orch.tools.InferenceClient", _FakeInferenceClient)
    # Provide unusual tool names that could not be hard-coded
    custom_tool_name = "xYz_custom_runtime_tool_99"
    planner(
        _base_state(
            request="use custom tool",
            _repo_root=".",
            mcp_tools=[
                {
                    "name": custom_tool_name,
                    "description": "A custom runtime tool.",
                    "server_name": "custom",
                },
            ],
            _models={
                "planner": {
                    "provider": "remote_digitalocean",
                    "model": "anthropic-claude-3.5-haiku",
                    "temperature": 0.0,
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

    assert f"`{custom_tool_name}`" in captured["user_prompt"]
    assert "## Available MCP Tools" in captured["user_prompt"]


# --- Fix 10.1: _default_plan with failed verification ---


def test_default_plan_with_failed_verification_includes_recovery_step() -> None:
    """_default_plan with a failed verification dict produces a recovery step."""
    verification = {
        "ok": False,
        "failure_class": "test_assertion",
        "recovery": {"action": "widen_context"},
    }
    plan = _default_plan("fix the bug", verification=verification)
    plan_dict = plan.model_dump()
    steps = plan_dict["steps"]
    assert len(steps) == 2
    recovery_step = steps[0]
    assert recovery_step["id"] == "step-0-recovery"
    assert "test_assertion" in recovery_step["description"]
    assert "widen_context" in recovery_step["description"]
    assert any(
        "Recovery from prior verification failure" in c
        for c in plan_dict["acceptance_criteria"]
    )


def test_default_plan_without_verification_unchanged() -> None:
    """_default_plan without verification produces the original single-step plan."""
    plan = _default_plan("fix the bug")
    plan_dict = plan.model_dump()
    assert len(plan_dict["steps"]) == 1
    assert plan_dict["steps"][0]["id"] == "step-1"


def test_default_plan_with_ok_verification_unchanged() -> None:
    """_default_plan with ok=True verification produces the original single-step plan."""
    plan = _default_plan("fix the bug", verification={"ok": True})
    plan_dict = plan.model_dump()
    assert len(plan_dict["steps"]) == 1
    assert plan_dict["steps"][0]["id"] == "step-1"


def test_planner_local_path_passes_verification_to_default_plan() -> None:
    """When the local model path is taken, verification state is forwarded to _default_plan."""
    verification = {
        "ok": False,
        "failure_class": "lint_error",
        "recovery": {"action": "retry"},
    }
    out = planner(_base_state(request="fix the bug", verification=verification))
    steps = out["plan"]["steps"]
    assert any("lint_error" in step["description"] for step in steps)
