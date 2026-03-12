from __future__ import annotations

from typing import Any

from lg_orch.nodes.verifier import verifier


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"request": "test", "tool_results": []}
    s.update(overrides)
    return s


def test_verifier_creates_ok_report() -> None:
    out = verifier(_base_state())
    assert out["verification"]["ok"] is True
    assert out["verification"]["checks"] == []
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
