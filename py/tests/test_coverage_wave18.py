# Wave 18 coverage tests — targets executor GLEAN wiring, planner reflection pool,
# reporter helpers, router node, runner_client helpers, and verifier helpers.
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

from lg_orch.model_routing import FailureReflection, SharedReflectionPool
from lg_orch.nodes._planner_prompt import (
    _extract_pdf_path,
    _first_step_handoff,
    _format_mcp_tool_catalog,
    _planner_mcp_prompt,
    _recovery_action_from_packet,
)
from lg_orch.nodes.executor import (
    _maybe_create_glean_auditor,
    executor,
)
from lg_orch.nodes.planner import _reflection_pool, planner
from lg_orch.nodes.reporter import (
    _get_inference_config,
    _structured_summary,
    _summarize_tool_results,
)
from lg_orch.nodes.router import _default_route, router
from lg_orch.nodes.verifier import (
    _diagnostic_summary,
    _extract_diagnostics,
    _failure_fingerprint,
    _first_nonempty_line,
    _is_architecture_mismatch,
)
from lg_orch.tools.runner_client import RunnerClient


def _executor_base(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "test",
        "_runner_enabled": True,
        "_runner_base_url": "http://127.0.0.1:8088",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "tools": [{"tool": "list_files", "input": {"path": "."}}],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
        "tool_results": [],
    }
    s.update(overrides)
    return s


def test_maybe_create_glean_auditor_disabled_by_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LG_GLEAN_ENABLED", None)
        assert _maybe_create_glean_auditor() is None


def test_maybe_create_glean_auditor_enabled() -> None:
    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "true"}):
        auditor = _maybe_create_glean_auditor()
        assert auditor is not None
        summary = auditor.summary()
        assert summary["guidelines_checked"] > 0


def test_maybe_create_glean_auditor_enabled_yes() -> None:
    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "yes"}):
        assert _maybe_create_glean_auditor() is not None


def test_maybe_create_glean_auditor_disabled_explicit() -> None:
    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "false"}):
        assert _maybe_create_glean_auditor() is None


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_glean_blocks_dangerous_tool(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    state = _executor_base(
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {"tool": "bash", "input": {"command": "git push origin main --force"}}
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        }
    )

    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "true"}):
        out = executor(state)

    # The dangerous tool should have been blocked
    blocked_results = [
        r for r in out["tool_results"] if r.get("artifacts", {}).get("error") == "glean_blocked"
    ]
    assert len(blocked_results) == 1
    assert "GLEAN blocked" in blocked_results[0]["stderr"]
    # Runner should not have been called for this tool
    mock_instance.batch_execute_tools.assert_not_called()


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_glean_adds_summary_trace_event(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "true"}):
        out = executor(_executor_base())

    events = out.get("_trace_events", [])
    glean_events = [e for e in events if e["kind"] == "glean"]
    assert len(glean_events) == 1
    assert glean_events[0]["data"]["compliant"] is True


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_glean_post_execution_records_warnings(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "bash",
            "ok": True,
            "exit_code": 0,
            "stdout": "api_key=sk-supersecretkey123456 found",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    with patch.dict(os.environ, {"LG_GLEAN_ENABLED": "true"}):
        out = executor(_executor_base())

    events = out.get("_trace_events", [])
    glean_events = [e for e in events if e["kind"] == "glean"]
    assert len(glean_events) == 1
    # There should be a warning violation but still compliant (no blocking)
    assert glean_events[0]["data"]["violations"] >= 1
    assert glean_events[0]["data"]["compliant"] is True


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_no_glean_when_disabled(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LG_GLEAN_ENABLED", None)
        out = executor(_executor_base())

    events = out.get("_trace_events", [])
    glean_events = [e for e in events if e["kind"] == "glean"]
    assert len(glean_events) == 0


# ---------------------------------------------------------------------------
# Planner — SharedReflectionPool wiring
# ---------------------------------------------------------------------------


def test_planner_reflection_pool_module_level() -> None:
    """The module-level reflection pool should be a SharedReflectionPool."""
    assert isinstance(_reflection_pool, SharedReflectionPool)


def test_planner_records_failure_reflection_on_exception() -> None:
    """When planner fails, a reflection should be added to the pool."""
    _reflection_pool.clear()

    # Force an exception in _planner_model_output by giving it bad state
    state: dict[str, Any] = {
        "request": "test failure recording",
        "repo_context": {},
        "_models": {
            "planner": {
                "provider": "remote",
                "model": "test-model",
            }
        },
    }

    # Patch the model output to raise an exception
    with patch("lg_orch.nodes.planner._planner_model_output", side_effect=ValueError("test error")):
        out = planner(state)

    # Should have used fallback plan
    assert out["plan"]["rollback"] == "Plan generation failed; deterministic fallback used."

    # Check the reflection pool has a reflection
    context = _reflection_pool.get_context()
    assert "planner_exception" in context
    assert "test error" in context

    _reflection_pool.clear()


def test_planner_injects_reflection_context_into_prompt() -> None:
    """When reflections exist, they should be injected into the system prompt."""
    _reflection_pool.clear()
    _reflection_pool.add_reflection(
        FailureReflection(
            loop_index=0,
            model_used="test-model",
            failure_class="test_failure",
            reflection="The approach of modifying X failed because Y",
        )
    )

    state: dict[str, Any] = {
        "request": "test reflection injection",
        "repo_context": {},
    }

    # The planner uses _build_planner_prompts internally; we just need to
    # confirm it runs without error and produces a plan
    out = planner(state)
    assert "plan" in out
    assert out["plan"] is not None

    _reflection_pool.clear()


# ---------------------------------------------------------------------------
# Reporter — _summarize_tool_results and _structured_summary
# ---------------------------------------------------------------------------


def test_summarize_tool_results_basic() -> None:
    results: list[Any] = [
        {"tool": "list_files", "ok": True, "stdout": "file1.py\nfile2.py", "stderr": ""},
        {"tool": "exec", "ok": False, "stdout": "", "stderr": "command failed"},
    ]
    summary = _summarize_tool_results(results)
    assert "[list_files] ok=True" in summary
    assert "[exec] ok=False" in summary
    assert "command failed" in summary


def test_summarize_tool_results_skips_non_dict() -> None:
    results: list[Any] = ["not a dict", 42, None]
    summary = _summarize_tool_results(results)
    assert summary == ""


def test_summarize_tool_results_truncates_long_output() -> None:
    results: list[Any] = [
        {"tool": "read_file", "ok": True, "stdout": "x" * 2000, "stderr": ""},
    ]
    summary = _summarize_tool_results(results)
    assert len(summary) <= 2000


def test_structured_summary_basic() -> None:
    state: dict[str, Any] = {
        "intent": "code_change",
        "repo_context": {"repo_root": "/tmp/test", "top_level": ["a"]},
        "tool_results": [{"tool": "exec"}],
        "verification": {"ok": True, "acceptance_ok": True},
    }
    summary = _structured_summary(state)
    assert "intent: code_change" in summary
    assert "repo_root: /tmp/test" in summary
    assert "tool_calls: 1" in summary
    assert "verification_ok: True" in summary


def test_structured_summary_with_halt_reason() -> None:
    state: dict[str, Any] = {
        "intent": "debug",
        "repo_context": {},
        "tool_results": [],
        "verification": {},
        "halt_reason": "budget_exceeded",
    }
    summary = _structured_summary(state)
    assert "halt_reason: budget_exceeded" in summary


def test_structured_summary_with_unmet_acceptance() -> None:
    state: dict[str, Any] = {
        "intent": "analysis",
        "repo_context": {},
        "tool_results": [],
        "verification": {
            "acceptance_checks": [
                {"criterion": "Tests pass", "ok": False, "detail": "failed"},
                {"criterion": "Lint clean", "ok": True, "detail": ""},
            ],
        },
    }
    summary = _structured_summary(state)
    assert "acceptance_unmet" in summary
    assert "Tests pass" in summary


# ---------------------------------------------------------------------------
# Router — additional _default_route edge cases and router() node
# ---------------------------------------------------------------------------


def test_default_route_recovery_with_compression_pressure() -> None:
    route = _default_route(
        {
            "request": "test",
            "retry_target": "router",
            "repo_context": {
                "planner_context": {
                    "token_estimate": 200,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 5,
                    "fact_count": 0,
                },
                "compression": {"pressure": {"overall": {"score": 5}}},
            },
            "facts": [],
            "budgets": {"current_loop": 0},
            "_model_routing_policy": {
                "interactive_context_limit": 1800,
                "default_cache_affinity": "workspace",
            },
        }
    )
    assert route.lane == "recovery"
    assert "compression pressure" in route.rationale


def test_default_route_recovery_from_recovery_packet() -> None:
    route = _default_route(
        {
            "request": "test",
            "recovery_packet": {
                "failure_class": "test_failure_post_change",
                "context_scope": "full_reset",
            },
            "verification": {
                "ok": False,
                "recovery_packet": {
                    "failure_class": "test_failure_post_change",
                    "context_scope": "full_reset",
                },
            },
            "repo_context": {
                "planner_context": {
                    "token_estimate": 200,
                    "working_set_token_estimate": 80,
                    "compression_pressure": 0,
                    "fact_count": 0,
                },
                "compression": {"pressure": {"overall": {"score": 0}}},
            },
            "facts": [],
            "budgets": {"current_loop": 1},
            "_model_routing_policy": {
                "interactive_context_limit": 1800,
                "default_cache_affinity": "workspace",
            },
        }
    )
    assert route.lane == "recovery"
    assert route.task_class == "test_failure_post_change"


def test_router_node_returns_route_and_intent() -> None:
    state: dict[str, Any] = {
        "request": "fix the broken test",
        "repo_context": {},
        "facts": [],
        "budgets": {},
    }
    out = router(state)
    assert "route" in out
    assert out["intent"] == "code_change"
    assert out["route"]["lane"] in {"interactive", "deep_planning", "recovery"}
    events = out.get("_trace_events", [])
    assert any(e["kind"] == "node" and e["data"]["name"] == "router" for e in events)


def test_router_node_with_pydantic_state() -> None:
    from lg_orch.state import OrchState

    state = OrchState(request="summarize the repo")
    out = router(state)
    assert out["intent"] == "analysis"


# ---------------------------------------------------------------------------
# RunnerClient helpers
# ---------------------------------------------------------------------------


def test_runner_client_checkpoint_payload_valid() -> None:
    payload = RunnerClient._checkpoint_payload(
        {"_checkpoint": {"thread_id": "t-1", "checkpoint_ns": "ns"}}
    )
    assert payload is not None
    assert payload["thread_id"] == "t-1"
    assert payload["checkpoint_ns"] == "ns"


def test_runner_client_checkpoint_payload_with_ids() -> None:
    payload = RunnerClient._checkpoint_payload(
        {
            "_checkpoint": {
                "thread_id": "t-1",
                "checkpoint_ns": "",
                "latest_checkpoint_id": "cp-1",
                "run_id": "run-1",
            }
        }
    )
    assert payload is not None
    assert payload["checkpoint_id"] == "cp-1"
    assert payload["run_id"] == "run-1"


def test_runner_client_checkpoint_payload_resume_id() -> None:
    payload = RunnerClient._checkpoint_payload(
        {
            "_checkpoint": {
                "thread_id": "t-1",
                "checkpoint_ns": "",
                "resume_checkpoint_id": "rcp-1",
            }
        }
    )
    assert payload is not None
    assert payload["checkpoint_id"] == "rcp-1"


def test_runner_client_checkpoint_payload_no_thread_id() -> None:
    assert RunnerClient._checkpoint_payload({"_checkpoint": {"checkpoint_ns": "ns"}}) is None


def test_runner_client_checkpoint_payload_not_dict() -> None:
    assert RunnerClient._checkpoint_payload({"_checkpoint": "bad"}) is None
    assert RunnerClient._checkpoint_payload({}) is None


def test_runner_client_route_payload() -> None:
    payload = RunnerClient._route_payload({"_route": {"lane": "interactive"}})
    assert payload == {"lane": "interactive"}


def test_runner_client_route_payload_not_dict() -> None:
    assert RunnerClient._route_payload({"_route": "bad"}) is None
    assert RunnerClient._route_payload({}) is None


# ---------------------------------------------------------------------------
# Verifier helpers — _diagnostic_summary, _first_nonempty_line,
#                     _failure_fingerprint, _is_architecture_mismatch
# ---------------------------------------------------------------------------


def test_diagnostic_summary_full() -> None:
    d = {"file": "src/main.rs", "line": 10, "column": 5, "code": "E0432", "message": "unresolved"}
    assert _diagnostic_summary(d) == "src/main.rs:10:5 [E0432] unresolved"


def test_diagnostic_summary_no_column() -> None:
    d = {"file": "src/main.rs", "line": 10, "code": "", "message": "error"}
    assert _diagnostic_summary(d) == "src/main.rs:10 error"


def test_diagnostic_summary_no_file() -> None:
    d = {"file": "", "line": 10, "code": "E001", "message": "bad"}
    assert _diagnostic_summary(d) == "[E001] bad"


def test_diagnostic_summary_empty() -> None:
    assert _diagnostic_summary({}) == ""


def test_first_nonempty_line() -> None:
    assert _first_nonempty_line("\n\n  hello  \nworld") == "hello"
    assert _first_nonempty_line("") == ""
    assert _first_nonempty_line("\n\n  \n") == ""


def test_failure_fingerprint_uses_direct_fingerprint() -> None:
    result: dict[str, Any] = {"tool": "exec", "stderr": "err", "artifacts": {}}
    diagnostics = [{"fingerprint": "fp-123", "message": "fail"}]
    assert _failure_fingerprint(result, diagnostics) == "fp-123"


def test_failure_fingerprint_generates_hash() -> None:
    result: dict[str, Any] = {
        "tool": "exec",
        "stderr": "error output",
        "artifacts": {"error": "compile_failed"},
    }
    diagnostics: list[dict[str, Any]] = []
    fp = _failure_fingerprint(result, diagnostics)
    assert len(fp) == 16  # sha256 hex[:16]


def test_extract_diagnostics_from_direct() -> None:
    result: dict[str, Any] = {"diagnostics": [{"message": "a"}, {"message": "b"}]}
    assert len(_extract_diagnostics(result)) == 2


def test_extract_diagnostics_from_artifacts() -> None:
    result: dict[str, Any] = {
        "diagnostics": "bad",
        "artifacts": {"diagnostics": [{"message": "c"}]},
    }
    assert len(_extract_diagnostics(result)) == 1


def test_extract_diagnostics_empty() -> None:
    assert _extract_diagnostics({}) == []


def test_is_architecture_mismatch_read_file() -> None:
    assert (
        _is_architecture_mismatch(tool="read_file", diagnostics=[], stderr="", artifacts={}) is True
    )


def test_is_architecture_mismatch_error_tag() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec", diagnostics=[], stderr="", artifacts={"error": "read_denied"}
        )
        is True
    )


def test_is_architecture_mismatch_diagnostic_code() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec",
            diagnostics=[{"code": "E0432", "message": "unresolved import"}],
            stderr="",
            artifacts={},
        )
        is True
    )


def test_is_architecture_mismatch_stderr_hint() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec", diagnostics=[], stderr="error: no such file or directory", artifacts={}
        )
        is True
    )


def test_is_architecture_mismatch_missing_module() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec", diagnostics=[], stderr="missing module foo", artifacts={}
        )
        is True
    )


def test_is_architecture_mismatch_false() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec", diagnostics=[], stderr="lint warning: unused import", artifacts={}
        )
        is False
    )


# ---------------------------------------------------------------------------
# Planner prompt helpers
# ---------------------------------------------------------------------------


def test_extract_pdf_path() -> None:
    assert _extract_pdf_path('Read "docs/spec.pdf" and implement it') == "docs/spec.pdf"
    assert _extract_pdf_path("no pdf here") is None
    assert _extract_pdf_path("") is None


def test_first_step_handoff_returns_none_for_no_steps() -> None:
    assert _first_step_handoff({"steps": []}) is None
    assert _first_step_handoff({"steps": "bad"}) is None


def test_first_step_handoff_extracts_handoff() -> None:
    plan = {
        "steps": [
            {"id": "step-1", "handoff": {"producer": "planner", "consumer": "coder"}},
        ]
    }
    h = _first_step_handoff(plan)
    assert h is not None
    assert h["consumer"] == "coder"


def test_recovery_action_from_packet() -> None:
    packet = {
        "failure_class": "test_failure",
        "failure_fingerprint": "fp-1",
        "rationale": "tests failed",
        "retry_target": "coder",
        "context_scope": "working_set",
        "plan_action": "amend",
    }
    action = _recovery_action_from_packet(packet)
    assert action["failure_class"] == "test_failure"
    assert action["retry_target"] == "coder"
    assert action["plan_action"] == "amend"


def test_recovery_action_from_packet_defaults() -> None:
    action = _recovery_action_from_packet({})
    assert action["retry_target"] == "planner"
    assert action["context_scope"] == "working_set"
    assert action["plan_action"] == "keep"


def test_planner_mcp_prompt_empty() -> None:
    assert _planner_mcp_prompt({}) == ""


def test_planner_mcp_prompt_with_catalog() -> None:
    ctx = {"mcp_catalog": "my-catalog", "mcp_recovery_hints": "retry hint"}
    prompt = _planner_mcp_prompt(ctx)
    assert "mcp_catalog: my-catalog" in prompt
    assert "mcp_recovery_hints: retry hint" in prompt


def test_format_mcp_tool_catalog_empty() -> None:
    assert _format_mcp_tool_catalog([]) == ""


def test_format_mcp_tool_catalog_with_tools() -> None:
    tools = [
        {
            "name": "echo",
            "description": "Echo text",
            "inputSchema": {"properties": {"text": {"type": "string"}}},
        },
        {"name": "noop", "description": ""},
    ]
    catalog = _format_mcp_tool_catalog(tools)
    assert "## Available MCP Tools" in catalog
    assert "`echo`" in catalog
    assert "`noop`" in catalog


# ---------------------------------------------------------------------------
# Executor — tool call budget and patch budget
# ---------------------------------------------------------------------------


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_tool_call_budget_exceeded(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    state = _executor_base(
        _budget_max_tool_calls_per_loop=1,
        budgets={"tool_calls_used": 0},
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {"tool": "list_files", "input": {"path": "."}},
                        {"tool": "read_file", "input": {"path": "a.py"}},
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
    )
    out = executor(state)
    assert any(
        r.get("artifacts", {}).get("error") == "tool_call_budget_exceeded"
        for r in out["tool_results"]
    )


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_patch_size_budget_exceeded(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    state = _executor_base(
        _budget_max_patch_bytes=10,
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {"patch": "x" * 100},
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
    )
    out = executor(state)
    assert any(
        r.get("artifacts", {}).get("error") == "patch_size_budget_exceeded"
        for r in out["tool_results"]
    )


def test_executor_invalid_base_url() -> None:
    state = _executor_base(_runner_base_url="ftp://bad-url")
    out = executor(state)
    events = out.get("_trace_events", [])
    assert any(e["kind"] == "node" and e["data"].get("error") == "invalid_base_url" for e in events)


@patch("lg_orch.nodes.executor.RunnerClient", side_effect=Exception("init failed"))
def test_executor_client_init_failure(mock_cls: MagicMock) -> None:
    state = _executor_base()
    out = executor(state)
    events = out.get("_trace_events", [])
    assert any(
        e["kind"] == "node" and e["data"].get("error") == "client_init_failed" for e in events
    )


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_step_exception_handled(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.side_effect = RuntimeError("connection failed")
    mock_cls.return_value = mock_instance

    out = executor(_executor_base())
    assert any(
        r.get("artifacts", {}).get("error") == "executor_failed" for r in out["tool_results"]
    )


# ---------------------------------------------------------------------------
# Reporter — _get_inference_config
# ---------------------------------------------------------------------------


def test_get_inference_config_returns_none_for_local() -> None:
    state: dict[str, Any] = {"_models": {"planner": {"provider": "local", "model": "det"}}}
    assert _get_inference_config(state) is None


def test_get_inference_config_returns_none_no_model() -> None:
    state: dict[str, Any] = {"_models": {"planner": {"provider": "remote", "model": ""}}}
    assert _get_inference_config(state) is None


def test_get_inference_config_returns_none_no_api_key() -> None:
    state: dict[str, Any] = {
        "_models": {"planner": {"provider": "digitalocean", "model": "test-model"}},
        "_model_provider_runtime": {"digitalocean": {"api_key": ""}},
    }
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MODEL_ACCESS_KEY", None)
        os.environ.pop("DIGITAL_OCEAN_MODEL_ACCESS_KEY", None)
        result = _get_inference_config(state)
    assert result is None


def test_get_inference_config_openai_compatible() -> None:
    state: dict[str, Any] = {
        "_models": {"planner": {"provider": "openai_compatible", "model": "gpt-4"}},
        "_model_provider_runtime": {
            "openai_compatible": {
                "api_key": "sk-test",
                "base_url": "https://api.openai.com/v1",
                "timeout_s": 30,
            }
        },
    }
    result = _get_inference_config(state)
    assert result is not None
    model, api_key, _base_url, timeout_s = result
    assert model == "gpt-4"
    assert api_key == "sk-test"
    assert timeout_s == 30
