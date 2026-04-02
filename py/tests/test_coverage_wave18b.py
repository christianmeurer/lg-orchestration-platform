# Wave 18 coverage tests — additional targets for router, trace helpers,
# reporter, executor edge cases, and verifier.
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lg_orch.commands.trace import (
    _trace_payload_from_path,
    _trace_run_id,
    trace_view_command,
)
from lg_orch.nodes.executor import executor
from lg_orch.nodes.reporter import _summarize_tool_results, reporter
from lg_orch.nodes.router import _classify_intent, router
from lg_orch.nodes.verifier import (
    _acceptance_failure,
    _build_checks,
    _classify_retry,
    _diagnostics_telemetry_entries,
    _evaluate_acceptance_checks,
    _is_architecture_mismatch,
    _is_test_failure_post_change,
    _loop_summary_entry,
    _next_handoff_payload,
    _recovery_action_payload,
    _recovery_packet_payload,
    verifier,
)
from lg_orch.tools.runner_client import RunnerClient

# ---------------------------------------------------------------------------
# trace helpers
# ---------------------------------------------------------------------------


def test_trace_payload_from_path_valid(tmp_path: Path) -> None:
    p = tmp_path / "run-test.json"
    p.write_text('{"run_id": "r1", "events": []}')
    result = _trace_payload_from_path(p, warn_context="test")
    assert result is not None
    assert result["run_id"] == "r1"


def test_trace_payload_from_path_not_found(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    assert _trace_payload_from_path(p, warn_context="test") is None


def test_trace_payload_from_path_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert _trace_payload_from_path(p, warn_context="test") is None


def test_trace_payload_from_path_not_dict(tmp_path: Path) -> None:
    p = tmp_path / "array.json"
    p.write_text("[1, 2, 3]")
    assert _trace_payload_from_path(p, warn_context="test") is None


def test_trace_run_id_from_payload(tmp_path: Path) -> None:
    p = tmp_path / "run-abc.json"
    assert _trace_run_id(p, {"run_id": "custom-id"}) == "custom-id"


def test_trace_run_id_from_filename(tmp_path: Path) -> None:
    p = tmp_path / "run-xyz123.json"
    assert _trace_run_id(p, {}) == "xyz123"


def test_trace_view_command_missing_file(tmp_path: Path) -> None:
    args = Namespace(trace_path=str(tmp_path / "missing.json"), format="console", width=80)
    assert trace_view_command(args) == 2


def test_trace_view_command_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{bad")
    args = Namespace(trace_path=str(p), format="console", width=80)
    assert trace_view_command(args) == 2


def test_trace_view_command_not_dict(tmp_path: Path) -> None:
    p = tmp_path / "arr.json"
    p.write_text("[1,2]")
    args = Namespace(trace_path=str(p), format="console", width=80)
    assert trace_view_command(args) == 2


def test_trace_view_command_console(tmp_path: Path) -> None:
    p = tmp_path / "run-test.json"
    p.write_text(json.dumps({"run_id": "r1", "_trace_events": []}))
    args = Namespace(trace_path=str(p), format="console", width=80)
    assert trace_view_command(args) == 0


def test_trace_view_command_html_to_file(tmp_path: Path) -> None:
    p = tmp_path / "run-test.json"
    p.write_text(json.dumps({"run_id": "r1", "_trace_events": []}))
    out = tmp_path / "out.html"
    args = Namespace(trace_path=str(p), format="html", output=str(out))
    assert trace_view_command(args) == 0
    assert out.exists()


def test_trace_view_command_html_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "run-test.json"
    p.write_text(json.dumps({"run_id": "r1", "_trace_events": []}))
    args = Namespace(trace_path=str(p), format="html", output=None)
    assert trace_view_command(args) == 0
    captured = capsys.readouterr()
    assert "<html" in captured.out.lower() or "<!doctype" in captured.out.lower()


# ---------------------------------------------------------------------------
# Router — router() node exercise deeper
# ---------------------------------------------------------------------------


def test_router_recovery_lane() -> None:
    state: dict[str, Any] = {
        "request": "test recovery",
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
        "verification": {
            "ok": False,
            "recovery": {
                "failure_class": "test_failure",
                "context_scope": "working_set",
            },
        },
    }
    out = router(state)
    assert out["route"]["lane"] == "recovery"


def test_router_creates_telemetry() -> None:
    state: dict[str, Any] = {
        "request": "summarize",
        "repo_context": {},
        "facts": [],
        "budgets": {},
    }
    out = router(state)
    assert "telemetry" in out
    routing = out["telemetry"].get("routing", [])
    assert len(routing) >= 1


def test_classify_intent_build_and_create() -> None:
    assert _classify_intent("build the Docker image") == "code_change"
    assert _classify_intent("create a new module") == "code_change"
    assert _classify_intent("generate test cases") == "code_change"
    assert _classify_intent("write a function") == "code_change"
    assert _classify_intent("make the button blue") == "code_change"


# ---------------------------------------------------------------------------
# Executor — apply_patch with auto-generated approval (no policy)
# ---------------------------------------------------------------------------


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_auto_approval_for_apply_patch(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "apply_patch",
            "ok": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    state: dict[str, Any] = {
        "request": "test",
        "_runner_enabled": True,
        "_runner_base_url": "http://127.0.0.1:8088",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {
                                "changes": [{"path": "py/x.py", "op": "add", "content": "x"}]
                            },
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
        "tool_results": [],
        "guards": {"require_approval_for_mutations": False},
    }
    executor(state)
    # Should have called the runner with an auto-generated approval
    calls = mock_instance.batch_execute_tools.call_args.kwargs["calls"]
    assert "approval" in calls[0]["input"]
    assert calls[0]["input"]["approval"]["challenge_id"] == "approval:apply_patch"


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_scip_stale_on_apply_patch_success(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "apply_patch",
            "ok": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    state: dict[str, Any] = {
        "request": "test",
        "_runner_enabled": True,
        "_runner_base_url": "http://127.0.0.1:8088",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {
                                "changes": [{"path": "py/x.py", "op": "add", "content": "x"}]
                            },
                        }
                    ],
                }
            ],
        },
        "tool_results": [],
    }
    out = executor(state)
    assert out.get("_scip_index_stale") is True


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_checkpoint_extraction(mock_cls: MagicMock) -> None:
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
            "snapshot": {
                "checkpoint": {
                    "thread_id": "t-1",
                    "checkpoint_ns": "ns",
                    "checkpoint_id": "cp-1",
                    "run_id": "run-1",
                },
                "snapshot_id": "snap-1",
            },
        }
    ]
    mock_cls.return_value = mock_instance

    state: dict[str, Any] = {
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
        },
        "tool_results": [],
    }
    out = executor(state)
    cp = out["_checkpoint"]
    assert cp["thread_id"] == "t-1"
    assert cp["latest_checkpoint_id"] == "cp-1"
    assert cp["latest_snapshot_id"] == "snap-1"
    assert cp["run_id"] == "run-1"


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_undo_snapshot_extraction(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "undo",
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
            "undo": {
                "restored_snapshot_id": "snap-restored",
                "checkpoint": {
                    "checkpoint_id": "cp-undo",
                },
            },
        }
    ]
    mock_cls.return_value = mock_instance

    state: dict[str, Any] = {
        "request": "test",
        "_runner_enabled": True,
        "_runner_base_url": "http://127.0.0.1:8088",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "tools": [{"tool": "undo", "input": {}}],
                }
            ],
        },
        "tool_results": [],
    }
    out = executor(state)
    cp = out["_checkpoint"]
    assert cp["latest_snapshot_id"] == "snap-restored"
    assert cp["latest_checkpoint_id"] == "cp-undo"


# ---------------------------------------------------------------------------
# Verifier — additional helpers
# ---------------------------------------------------------------------------


def test_is_test_failure_post_change_with_write_file() -> None:
    """write_file should also count as a patch for test_failure_post_change."""
    result = _is_test_failure_post_change(
        tool="run_tests",
        diagnostics=[],
        stderr="",
        stdout="FAILED tests/test_foo.py::test_bar",
        artifacts={},
        tool_results=[
            {
                "tool": "write_file",
                "ok": True,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "diagnostics": [],
                "timing_ms": 1,
                "artifacts": {},
            }
        ],
    )
    assert result is True


def test_is_architecture_mismatch_path_escapes_root() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec",
            diagnostics=[],
            stderr="",
            artifacts={"error": "path escapes root"},
        )
        is True
    )


def test_is_architecture_mismatch_e0583() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec",
            diagnostics=[{"code": "E0583", "message": "file not found"}],
            stderr="",
            artifacts={},
        )
        is True
    )


def test_is_architecture_mismatch_f821() -> None:
    assert (
        _is_architecture_mismatch(
            tool="exec",
            diagnostics=[{"code": "F821", "message": "undefined name"}],
            stderr="",
            artifacts={},
        )
        is True
    )


def test_verifier_handles_pydantic_state() -> None:
    from lg_orch.state import OrchState

    state = OrchState(request="test verifier with pydantic")
    out = verifier(state)
    assert out["verification"]["ok"] is True


def test_verifier_acceptance_check_with_repo_context() -> None:
    out = verifier(
        {
            "request": "test",
            "plan": {
                "steps": [{"id": "step-1"}],
                "acceptance_criteria": ["Necessary repository context was gathered."],
                "max_iterations": 1,
            },
            "repo_context": {"repo_root": "/tmp", "top_level": ["a.py"]},
            "tool_results": [],
        }
    )
    # With repo_context present, the acceptance check should pass
    assert out["verification"]["acceptance_ok"] is True


# ---------------------------------------------------------------------------
# Reporter — edge cases
# ---------------------------------------------------------------------------


def test_reporter_with_pydantic_state() -> None:
    from lg_orch.state import OrchState

    state = OrchState(request="test reporter with pydantic")
    out = reporter(state)
    assert "final" in out


def test_summarize_tool_results_large_set() -> None:
    """Summary should be capped even with many results."""
    results: list[Any] = [
        {"tool": f"tool_{i}", "ok": True, "stdout": "x" * 200, "stderr": ""} for i in range(50)
    ]
    summary = _summarize_tool_results(results)
    assert len(summary) <= 2000


# ---------------------------------------------------------------------------
# RunnerClient — close
# ---------------------------------------------------------------------------


def test_runner_client_close() -> None:
    with patch("lg_orch.tools.runner_client.httpx.Client") as mock_client:
        mock_instance = MagicMock()
        mock_client.return_value = mock_instance
        client = RunnerClient(base_url="http://localhost:8088")
        client.close()
        mock_instance.close.assert_called_once()


def test_runner_client_close_no_client() -> None:
    # Should not raise when _client is None
    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "_client", None)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    client.close()  # should not raise


# ---------------------------------------------------------------------------
# Verifier — deeper coverage of helpers
# ---------------------------------------------------------------------------


def test_build_checks_returns_empty_for_all_ok() -> None:
    results = [
        {"tool": "exec", "ok": True, "exit_code": 0},
        {"tool": "test", "ok": True, "exit_code": 0},
    ]
    assert _build_checks(results) == []


def test_build_checks_multiple_failures() -> None:
    results = [
        {
            "tool": "exec",
            "ok": False,
            "exit_code": 1,
            "stderr": "compile error",
            "diagnostics": [],
        },
        {
            "tool": "test",
            "ok": False,
            "exit_code": 2,
            "stderr": "",
            "diagnostics": [{"file": "a.py", "line": 5, "message": "assertion"}],
        },
    ]
    checks = _build_checks(results)
    assert len(checks) == 2
    assert checks[0].tool == "exec"
    assert checks[0].exit_code == 1
    assert checks[1].tool == "test"


def test_build_checks_bad_exit_code_type() -> None:
    results = [{"tool": "x", "ok": False, "exit_code": "bad", "stderr": "err"}]
    checks = _build_checks(results)
    assert checks[0].exit_code == 1


def test_classify_retry_repeated_failure_at_loop_2() -> None:
    tool_results = [
        {
            "tool": "exec",
            "ok": False,
            "exit_code": 1,
            "stderr": "generic failure",
            "diagnostics": [],
            "artifacts": {},
        }
    ]
    recovery, label = _classify_retry(tool_results, current_loop=2)
    assert label == "repeated_verification_failure"
    assert recovery["retry_target"] == "router"


def test_classify_retry_all_ok_returns_default() -> None:
    tool_results = [{"tool": "exec", "ok": True}]
    recovery, label = _classify_retry(tool_results, current_loop=0)
    assert label == "verification_failed"
    assert recovery["retry_target"] == "planner"


def test_classify_retry_empty_results() -> None:
    _recovery, label = _classify_retry([], current_loop=0)
    assert label == "verification_failed"


def test_recovery_action_payload() -> None:
    recovery = {
        "failure_class": "test_failure",
        "failure_fingerprint": "fp-1",
        "rationale": "tests failed",
        "retry_target": "coder",
        "context_scope": "working_set",
        "plan_action": "amend",
    }
    payload = _recovery_action_payload(recovery)
    assert payload["failure_class"] == "test_failure"
    assert payload["retry_target"] == "coder"


def test_recovery_action_payload_defaults() -> None:
    payload = _recovery_action_payload({})
    assert payload["retry_target"] == "planner"
    assert payload["context_scope"] == "working_set"
    assert payload["plan_action"] == "keep"


def test_recovery_packet_payload() -> None:
    recovery = {
        "failure_class": "test_failure",
        "failure_fingerprint": "fp-1",
        "rationale": "tests failed",
        "retry_target": "planner",
        "context_scope": "working_set",
        "plan_action": "keep",
    }
    packet = _recovery_packet_payload(
        recovery,
        current_loop=1,
        loop_summary="loop 1 summary",
        last_check="last check text",
        discard_reason="",
    )
    assert packet["loop"] == 1
    assert packet["origin"] == "verifier"
    assert packet["summary"] == "loop 1 summary"
    assert packet["last_check"] == "last check text"


def test_loop_summary_entry() -> None:
    report: dict[str, Any] = {
        "failure_class": "test_failure",
        "failure_fingerprint": "fp-1",
        "retry_target": "coder",
        "plan_action": "amend",
        "loop_summary": "something failed",
        "recovery_packet": {"context_scope": "working_set", "last_check": "check"},
    }
    entry = _loop_summary_entry(
        report,
        current_loop=2,
        acceptance_criteria=["Tests pass"],
        acceptance_checks=[{"criterion": "Tests pass", "ok": False}],
    )
    assert entry["loop"] == 2
    assert entry["failure_class"] == "test_failure"
    assert "Tests pass" in entry["acceptance_unmet"]


def test_evaluate_acceptance_checks_no_criteria() -> None:
    state: dict[str, Any] = {"plan": {"acceptance_criteria": []}}
    assert _evaluate_acceptance_checks(state, tool_results=[], checks=[]) == []


def test_evaluate_acceptance_checks_context_criterion() -> None:
    state: dict[str, Any] = {
        "plan": {"acceptance_criteria": ["Necessary repository context was gathered."]},
        "repo_context": {"top_level": ["a.py"]},
    }
    checks = _evaluate_acceptance_checks(state, tool_results=[], checks=[])
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["detail"] == "repo_context_available"


def test_evaluate_acceptance_checks_bounded_criterion() -> None:
    state: dict[str, Any] = {
        "plan": {
            "acceptance_criteria": ["bounded next steps available"],
            "steps": [{"id": "step-1"}],
        },
        "repo_context": {},
    }
    checks = _evaluate_acceptance_checks(state, tool_results=[], checks=[])
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert checks[0]["detail"] == "bounded_plan_available"


def test_evaluate_acceptance_checks_request_criterion() -> None:
    state: dict[str, Any] = {
        "plan": {
            "acceptance_criteria": ["The request can be answered or executed"],
            "steps": [{"id": "step-1"}],
        },
        "repo_context": {},
    }
    checks = _evaluate_acceptance_checks(
        state,
        tool_results=[{"ok": True}],
        checks=[],
    )
    assert checks[0]["ok"] is True
    assert checks[0]["detail"] == "request_path_available"


def test_acceptance_failure_no_unmet() -> None:
    checks = [{"criterion": "Tests pass", "ok": True}]
    recovery, failure_class, _summary = _acceptance_failure(checks)
    assert recovery == {}
    assert failure_class == ""


def test_acceptance_failure_with_unmet() -> None:
    checks = [{"criterion": "Tests pass", "ok": False}]
    recovery, failure_class, summary = _acceptance_failure(checks)
    assert failure_class == "acceptance_criteria_unmet"
    assert recovery["retry_target"] == "planner"
    assert "Tests pass" in summary


def test_next_handoff_payload_for_coder() -> None:
    from lg_orch.nodes.verifier import VerificationCheck

    state: dict[str, Any] = {
        "request": "fix the bug",
        "active_handoff": {
            "file_scope": ["py/src/main.py"],
            "objective": "prior objective",
            "consumer": "executor",
        },
        "plan": {
            "steps": [{"files_touched": ["py/src/utils.py"]}],
        },
    }
    report: dict[str, Any] = {
        "retry_target": "coder",
        "failure_class": "test_failure_post_change",
        "loop_summary": "test failed after patch",
    }
    handoff = _next_handoff_payload(
        state,
        report=report,
        checks=[
            VerificationCheck(
                name="tool_failure_1",
                ok=False,
                tool="test",
                exit_code=1,
                summary="assertion failed",
            )
        ],
        current_loop=1,
    )
    assert handoff is not None
    assert handoff["consumer"] == "coder"
    assert "py/src/main.py" in handoff["file_scope"]
    assert "py/src/utils.py" in handoff["file_scope"]


def test_next_handoff_payload_for_context_builder() -> None:
    state: dict[str, Any] = {"request": "test", "plan": {}}
    report: dict[str, Any] = {
        "retry_target": "context_builder",
        "failure_class": "architecture_mismatch",
    }
    handoff = _next_handoff_payload(state, report=report, checks=[], current_loop=0)
    assert handoff is not None
    assert handoff["consumer"] == "context_builder"
    assert "Rebuild" in handoff["objective"]


def test_next_handoff_payload_for_router() -> None:
    state: dict[str, Any] = {"request": "test", "plan": {}}
    report: dict[str, Any] = {
        "retry_target": "router",
        "failure_class": "repeated_failure",
    }
    handoff = _next_handoff_payload(state, report=report, checks=[], current_loop=3)
    assert handoff is not None
    assert handoff["consumer"] == "router"


def test_next_handoff_payload_invalid_consumer() -> None:
    state: dict[str, Any] = {"request": "test", "plan": {}}
    report: dict[str, Any] = {"retry_target": "unknown"}
    assert _next_handoff_payload(state, report=report, checks=[], current_loop=0) is None


def test_verifier_run_verification_calls_exception() -> None:
    """When _run_verification_calls raises, verifier should catch and record."""
    with patch(
        "lg_orch.nodes.verifier._run_verification_calls",
        side_effect=RuntimeError("runner crashed"),
    ):
        out = verifier(
            {
                "request": "test",
                "tool_results": [],
                "_runner_enabled": True,
            }
        )
    # Should still produce a report (the exception is caught)
    assert "verification" in out
    # The error should be recorded in tool_results
    assert any(
        r.get("artifacts", {}).get("error") == "verifier_execution_failed"
        for r in out.get("tool_results", [])
    )


@patch("lg_orch.nodes.verifier.RunnerClient")
def test_verifier_run_verification_calls_with_plan(mock_cls: MagicMock) -> None:
    """Verifier should dispatch plan verification calls to the runner."""
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "exec",
            "ok": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance

    out = verifier(
        {
            "request": "test",
            "_runner_enabled": True,
            "_runner_base_url": "http://127.0.0.1:8088",
            "plan": {
                "steps": [],
                "verification": [{"tool": "exec", "input": {"cmd": "pytest", "args": ["-x"]}}],
            },
            "tool_results": [],
        }
    )
    assert out["verification"]["ok"] is True
    mock_instance.batch_execute_tools.assert_called_once()


def test_verifier_formal_verification_path() -> None:
    """Verifier should call formal verification when vericoding is enabled."""
    with patch(
        "lg_orch.nodes.verifier._run_formal_verification",
        return_value={
            "tool": "formal_verification",
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "verification failed",
            "diagnostics": [],
            "artifacts": {"error": "formal_verification_failed"},
        },
    ):
        out = verifier(
            {
                "request": "test",
                "_runner_enabled": True,
                "_vericoding_enabled": True,
                "_vericoding_extensions": [".rs"],
                "tool_results": [
                    {
                        "tool": "apply_patch",
                        "ok": True,
                        "input": {"changes": [{"path": "src/main.rs"}]},
                    }
                ],
            }
        )
    assert out["verification"]["ok"] is False
    assert out["verification"]["failure_class"] == "formal_verification_failed"


def test_diagnostics_telemetry_entries() -> None:
    tool_results = [
        {
            "tool": "exec",
            "ok": False,
            "exit_code": 1,
            "stderr": "compile error",
            "diagnostics": [{"file": "a.py", "line": 5, "message": "syntax error"}],
            "artifacts": {"error": "compile_failed"},
        },
        {"tool": "test", "ok": True},
    ]
    report: dict[str, Any] = {"failure_class": "verification_failed"}
    entries = _diagnostics_telemetry_entries(tool_results, current_loop=1, report=report)
    assert len(entries) == 1
    assert entries[0]["tool"] == "exec"
    assert entries[0]["failure_class"] == "verification_failed"
    assert entries[0]["diagnostic_count"] == 1
