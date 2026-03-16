from __future__ import annotations

from typing import Any

from lg_orch.nodes.verifier import (
    _classify_retry,
    _is_test_failure_post_change,
    _requires_formal_verification,
    _run_formal_verification,
    verifier,
)


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"request": "test", "tool_results": []}
    s.update(overrides)
    return s


def test_verifier_creates_ok_report() -> None:
    out = verifier(_base_state())
    assert out["verification"]["ok"] is True
    assert out["verification"]["checks"] == []
    assert out["verification"]["acceptance_ok"] is True
    assert out["verification"]["retry_target"] is None
    assert out["verification"]["plan_action"] == "keep"


def test_verifier_creates_trace_events() -> None:
    out = verifier(_base_state())
    events = out.get("_trace_events", [])
    names = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "verifier" in names


def test_verifier_trace_has_start_and_end() -> None:
    out = verifier(_base_state())
    events = out.get("_trace_events", [])
    verifier_events = [e for e in events if e["kind"] == "node" and e["data"]["name"] == "verifier"]
    phases = [e["data"]["phase"] for e in verifier_events]
    assert "start" in phases
    assert "end" in phases


def test_verifier_preserves_state() -> None:
    out = verifier(_base_state(request="hello", intent="debug"))
    assert out["request"] == "hello"
    assert out["intent"] == "debug"


def test_verifier_uses_structured_diagnostics_first_for_summary() -> None:
    out = verifier(
        _base_state(
            tool_results=[
                {
                    "tool": "exec",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "plain stderr fallback",
                    "diagnostics": [
                        {
                            "file": "src/main.rs",
                            "line": 10,
                            "column": 5,
                            "code": "E0432",
                            "message": "unresolved import",
                        }
                    ],
                    "timing_ms": 1,
                    "artifacts": {},
                }
            ]
        )
    )
    checks = out["verification"]["checks"]
    assert len(checks) == 1
    assert "src/main.rs:10:5" in checks[0]["summary"]
    assert "[E0432] unresolved import" in checks[0]["summary"]


def test_verifier_failure_routes_to_planner_when_not_arch_mismatch() -> None:
    out = verifier(
        _base_state(
            tool_results=[
                {
                    "tool": "exec",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "test assertion failed",
                    "diagnostics": [],
                    "timing_ms": 1,
                    "artifacts": {},
                }
            ]
        )
    )
    assert out["verification"]["ok"] is False
    assert out["verification"]["retry_target"] == "planner"
    assert out["verification"]["plan_action"] == "keep"
    assert out["verification"]["recovery_packet"]["retry_target"] == "planner"
    assert out["recovery_packet"]["failure_class"] == "verification_failed"
    assert out["loop_summaries"][0]["recovery_packet"]["last_check"] == "test assertion failed"
    assert out["facts"][0]["kind"] == "recovery_fact"
    assert out["facts"][0]["failure_class"] == "verification_failed"
    assert out.get("context_reset_requested") in {None, False}


def test_verifier_failure_routes_to_context_builder_and_discards_plan() -> None:
    out = verifier(
        _base_state(
            plan={"steps": []},
            tool_results=[
                {
                    "tool": "exec",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "error[E0432]: unresolved import `crate::missing`",
                    "diagnostics": [
                        {
                            "file": "src/main.rs",
                            "line": 3,
                            "column": 1,
                            "code": "E0432",
                            "message": "unresolved import `crate::missing`",
                        }
                    ],
                    "timing_ms": 1,
                    "artifacts": {},
                }
            ],
        )
    )
    assert out["verification"]["retry_target"] == "context_builder"
    assert out["verification"]["plan_action"] == "discard_reset"
    assert out["verification"]["recovery_packet"]["context_scope"] == "full_reset"
    assert out["context_reset_requested"] is True
    assert out["plan_discarded"] is True
    assert out["plan_discard_reason"] == "architecture_mismatch_detected"
    assert out["plan"] is None


def test_verifier_prunes_large_read_payload_after_successful_verification() -> None:
    large = "x" * 6000
    out = verifier(
        _base_state(
            verification={"ok": True},
            history_policy={"read_file_prune_threshold_chars": 5000},
            tool_results=[
                {
                    "tool": "read_file",
                    "ok": True,
                    "exit_code": 0,
                    "stdout": large,
                    "stderr": "",
                    "diagnostics": [],
                    "timing_ms": 1,
                    "artifacts": {"path": "py/x.py"},
                },
                {
                    "tool": "apply_patch",
                    "ok": True,
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "diagnostics": [],
                    "timing_ms": 1,
                    "artifacts": {},
                },
            ],
            provenance=[],
        )
    )
    read_result = out["tool_results"][0]
    assert read_result["stdout"].startswith("[pruned_read_file_payload]")
    assert read_result["artifacts"]["pruned"]["stdout_chars"] == 6000
    assert out["provenance"][-1]["event"] == "read_file_payload_evicted"


def test_verifier_records_model_routing_telemetry() -> None:
    out = verifier(
        _base_state(
            telemetry={},
            _models={
                "router": {
                    "provider": "remote_openai",
                    "model": "gpt-4.1",
                    "temperature": 0.0,
                }
            },
            _model_routing_policy={
                "local_provider": "local",
                "fallback_task_classes": ["lint_reflection"],
            },
        )
    )
    routes = out.get("telemetry", {}).get("model_routing", [])
    assert len(routes) >= 1
    assert routes[-1]["node"] == "verifier"
    assert routes[-1]["task_class"] == "lint_reflection"


def test_verifier_records_normalized_diagnostics_telemetry() -> None:
    out = verifier(
        _base_state(
            telemetry={},
            tool_results=[
                {
                    "tool": "exec",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "plain stderr fallback",
                    "diagnostics": [
                        {
                            "file": "src/main.rs",
                            "line": 10,
                            "column": 5,
                            "code": "E0432",
                            "message": "unresolved import",
                        }
                    ],
                    "timing_ms": 1,
                    "artifacts": {},
                }
            ],
        )
    )
    diagnostics = out.get("telemetry", {}).get("diagnostics", [])
    assert diagnostics[-1]["tool"] == "exec"
    assert diagnostics[-1]["failure_fingerprint"]
    assert "src/main.rs:10:5" in diagnostics[-1]["summary"]


def test_verifier_fails_when_acceptance_criteria_are_unmet() -> None:
    out = verifier(
        _base_state(
            plan={
                "steps": [{"id": "step-1"}],
                "acceptance_criteria": ["Necessary repository context was gathered."],
                "max_iterations": 1,
            },
            repo_context={},
            tool_results=[],
        )
    )
    assert out["verification"]["ok"] is False
    assert out["verification"]["acceptance_ok"] is False
    assert out["verification"]["failure_class"] == "acceptance_criteria_unmet"
    assert out["verification"]["acceptance_checks"][0]["detail"] == "repo_context_missing"


# --- PDF #4: Reflect-phase test repair routing ---

def _patch_ok() -> dict[str, Any]:
    return {
        "tool": "apply_patch",
        "ok": True,
        "exit_code": 0,
        "stdout": "patch applied",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 1,
        "artifacts": {},
    }


def test_is_test_failure_post_change_true() -> None:
    result = _is_test_failure_post_change(
        tool="run_tests",
        diagnostics=[],
        stderr="",
        stdout="FAILED tests/test_foo.py::test_bar",
        artifacts={},
        tool_results=[_patch_ok()],
    )
    assert result is True


def test_is_test_failure_post_change_false_no_patch() -> None:
    result = _is_test_failure_post_change(
        tool="run_tests",
        diagnostics=[],
        stderr="",
        stdout="FAILED tests/test_foo.py::test_bar",
        artifacts={},
        tool_results=[],
    )
    assert result is False


def test_is_test_failure_post_change_false_non_test_tool() -> None:
    result = _is_test_failure_post_change(
        tool="compile",
        diagnostics=[],
        stderr="build failed",
        stdout="",
        artifacts={},
        tool_results=[_patch_ok()],
    )
    assert result is False


def test_classify_retry_returns_test_failure_post_change() -> None:
    tool_results: list[dict[str, Any]] = [
        _patch_ok(),
        {
            "tool": "run_tests",
            "ok": False,
            "exit_code": 1,
            "stdout": "FAILED tests/test_foo.py::test_bar",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {},
        },
    ]
    recovery, label = _classify_retry(tool_results, current_loop=0)
    assert label == "test_failure_post_change"
    assert recovery["failure_class"] == "test_failure_post_change"
    assert recovery["plan_action"] == "amend"
    assert recovery["retry_target"] == "planner"


def test_classify_retry_budget_exceeded_takes_priority() -> None:
    tool_results: list[dict[str, Any]] = [
        _patch_ok(),
        {
            "tool": "run_tests",
            "ok": False,
            "exit_code": 1,
            "stdout": "FAILED tests/test_foo.py::test_bar",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {"error": "tool_call_budget_exceeded"},
        },
    ]
    recovery, label = _classify_retry(tool_results, current_loop=0)
    assert label == "tool_call_budget_exceeded"
    assert recovery["failure_class"] == "budget_exceeded"

def test_requires_formal_verification() -> None:
    state = {
        "_vericoding_enabled": True,
        "_vericoding_extensions": [".rs"]
    }
    tool_results = [
        {
            "tool": "apply_patch",
            "ok": True,
            "input": {
                "changes": [
                    {"path": "src/main.rs"}
                ]
            }
        }
    ]
    files = _requires_formal_verification(state, tool_results)
    assert files == ["src/main.rs"]

def test_requires_formal_verification_disabled() -> None:
    state = {
        "_vericoding_enabled": False,
        "_vericoding_extensions": [".rs"]
    }
    tool_results = [
        {
            "tool": "apply_patch",
            "ok": True,
            "input": {
                "changes": [
                    {"path": "src/main.rs"}
                ]
            }
        }
    ]
    files = _requires_formal_verification(state, tool_results)
    assert files == []

def test_classify_retry_formal_verification_failed() -> None:
    tool_results = [
        {
            "tool": "formal_verification",
            "ok": False,
            "artifacts": {"error": "formal_verification_failed"}
        }
    ]
    recovery, label = _classify_retry(tool_results, current_loop=0)
    assert label == "formal_verification_failed"
    assert recovery["failure_class"] == "formal_verification_failed"
    assert recovery["plan_action"] == "amend"
    assert recovery["retry_target"] == "planner"


def test_formal_verification_skipped_for_invalid_url() -> None:
    state: dict[str, Any] = {
        "_runner_base_url": "ftp://bad",
        "_vericoding_enabled": True,
    }
    route_metadata: dict[str, Any] = {}
    result = _run_formal_verification(state, ["src/main.rs"], route_metadata)
    assert result is not None
    assert result["ok"] is False
    assert result["artifacts"]["error"] == "invalid_base_url"
