from __future__ import annotations

import pytest
from pydantic import ValidationError

from lg_orch.state import (
    OrchState,
    PlannerOutput,
    PlanStep,
    ToolCall,
    VerificationCheck,
    VerifierReport,
)

# --- ToolCall ---


def test_tool_call_valid() -> None:
    tc = ToolCall(tool="read_file", input={"path": "README.md"})
    assert tc.tool == "read_file"
    assert tc.input == {"path": "README.md"}


def test_tool_call_default_input() -> None:
    tc = ToolCall(tool="health")
    assert tc.input == {}


def test_tool_call_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ToolCall(tool="read_file", input={}, extra_field="bad")  # type: ignore[call-arg]


# --- PlanStep ---


def test_plan_step_valid() -> None:
    ps = PlanStep(id="s1", description="do thing", expected_outcome="done", files_touched=["a.py"])
    assert ps.id == "s1"
    assert ps.tools == []


def test_plan_step_with_tools() -> None:
    ps = PlanStep(
        id="s1",
        description="d",
        tools=[ToolCall(tool="exec", input={"cmd": "python"})],
        expected_outcome="ok",
    )
    assert len(ps.tools) == 1


def test_plan_step_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            id="s1",
            description="d",
            expected_outcome="ok",
            unknown="bad",  # type: ignore[call-arg]
        )


# --- PlannerOutput ---


def test_planner_output_valid() -> None:
    po = PlannerOutput(
        steps=[
            PlanStep(id="s1", description="d", expected_outcome="ok"),
        ],
        verification=[],
        rollback="none",
    )
    assert len(po.steps) == 1


def test_planner_output_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(steps=[], verification=[], rollback="none", extra="bad")  # type: ignore[call-arg]


# --- VerificationCheck ---


def test_verification_check_valid() -> None:
    vc = VerificationCheck(name="lint", ok=True, tool="ruff", exit_code=0)
    assert vc.summary == ""


def test_verification_check_with_summary() -> None:
    vc = VerificationCheck(name="lint", ok=False, tool="ruff", exit_code=1, summary="failed")
    assert vc.summary == "failed"


# --- VerifierReport ---


def test_verifier_report_valid() -> None:
    vr = VerifierReport(ok=True, checks=[])
    assert vr.ok is True
    assert vr.checks == []


def test_verifier_report_with_checks() -> None:
    vr = VerifierReport(
        ok=False,
        checks=[VerificationCheck(name="test", ok=False, tool="pytest", exit_code=1)],
    )
    assert len(vr.checks) == 1
    assert vr.ok is False


# --- OrchState ---


def test_orch_state_minimal() -> None:
    os_ = OrchState(request="hello")
    assert os_.request == "hello"
    assert os_.intent == "analysis"
    assert os_.plan is None
    assert os_.final == ""


def test_orch_state_with_intent() -> None:
    os_ = OrchState(request="fix bug", intent="code_change")
    assert os_.intent == "code_change"


def test_orch_state_invalid_intent() -> None:
    with pytest.raises(ValidationError):
        OrchState(request="test", intent="invalid_intent")  # type: ignore[arg-type]


def test_orch_state_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        OrchState(request="test", unknown="bad")  # type: ignore[call-arg]


def test_orch_state_model_dump_roundtrip() -> None:
    os_ = OrchState(request="test", intent="debug")
    dumped = os_.model_dump()
    restored = OrchState(**dumped)
    assert restored.request == "test"
    assert restored.intent == "debug"
