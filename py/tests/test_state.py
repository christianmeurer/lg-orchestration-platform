from __future__ import annotations

import pytest
from pydantic import ValidationError

from lg_orch.state import (
    AgentHandoff,
    HandoffEvidence,
    OrchState,
    PlannerOutput,
    PlanStep,
    RecoveryPacket,
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


def _handoff() -> AgentHandoff:
    return AgentHandoff(
        producer="planner",
        consumer="coder",
        objective="Prepare a minimal patch.",
        file_scope=["py/src/lg_orch/state.py"],
        evidence=[
            HandoffEvidence(kind="request", detail="implement the change", ref="user_request")
        ],
        constraints=["Prefer the smallest correct diff."],
        acceptance_checks=["The patch addresses the request."],
        retry_budget=1,
        provenance=["plan:step-1"],
    )


def test_agent_handoff_valid() -> None:
    handoff = _handoff()
    assert handoff.consumer == "coder"
    assert handoff.evidence[0].kind == "request"


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


def test_plan_step_accepts_handoff() -> None:
    ps = PlanStep(id="s1", description="d", expected_outcome="ok", handoff=_handoff())
    assert ps.handoff is not None
    assert ps.handoff.consumer == "coder"


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


def test_planner_output_accepts_recovery_packet() -> None:
    po = PlannerOutput(
        steps=[PlanStep(id="s1", description="d", expected_outcome="ok")],
        verification=[],
        rollback="none",
        recovery_packet=RecoveryPacket(
            failure_class="verification_failed",
            failure_fingerprint="fp-1",
            rationale="retry planning with same context",
            retry_target="planner",
            context_scope="working_set",
            plan_action="keep",
            loop=1,
            summary="verification_failed: test assertion failed",
            last_check="test assertion failed",
            discard_reason="",
        ),
    )
    assert po.recovery_packet is not None
    assert po.recovery_packet.failure_fingerprint == "fp-1"


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
    assert vr.retry_target is None
    assert vr.plan_action == "keep"


def test_verifier_report_with_checks() -> None:
    vr = VerifierReport(
        ok=False,
        checks=[VerificationCheck(name="test", ok=False, tool="pytest", exit_code=1)],
        retry_target="planner",
        plan_action="keep",
    )
    assert len(vr.checks) == 1
    assert vr.ok is False


def test_verifier_report_accepts_recovery_packet() -> None:
    vr = VerifierReport(
        ok=False,
        checks=[],
        retry_target="planner",
        recovery_packet=RecoveryPacket(
            failure_class="verification_failed",
            failure_fingerprint="fp-2",
            rationale="retry planning",
            retry_target="planner",
            context_scope="working_set",
            plan_action="keep",
            loop=2,
            summary="verification_failed: lint failed",
            last_check="lint failed",
            discard_reason="",
        ),
    )
    assert vr.recovery_packet is not None
    assert vr.recovery_packet.loop == 2


def test_verifier_report_accepts_next_handoff() -> None:
    vr = VerifierReport(ok=False, checks=[], retry_target="planner", next_handoff=_handoff())
    assert vr.next_handoff is not None
    assert vr.next_handoff.producer == "planner"


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


def test_orch_state_allows_extra() -> None:
    # OrchState uses extra="allow" so that LangGraph can attach internal
    # underscore-prefixed fields (_run_id, _lane, etc.) without failing
    # Pydantic validation.  Extra fields are stored in model_extra.
    state = OrchState(request="test", unknown="tolerated")  # type: ignore[call-arg]
    assert state.model_extra.get("unknown") == "tolerated"


def test_orch_state_model_dump_roundtrip() -> None:
    os_ = OrchState(request="test", intent="debug")
    dumped = os_.model_dump()
    restored = OrchState(**dumped)
    assert restored.request == "test"
    assert restored.intent == "debug"


def test_orch_state_new_phase1_fields_defaults() -> None:
    os_ = OrchState(request="hi")
    assert os_.active_handoff is None
    assert os_.retry_target is None
    assert os_.recovery_packet is None
    assert os_.context_reset_requested is False
    assert os_.plan_discarded is False
    assert os_.plan_discard_reason == ""
    assert os_.halt_reason == ""
    assert os_.history_policy == {}
    assert os_.provenance == []
    assert os_.checkpoint == {}
    assert os_.snapshots == []
    assert os_.undo == {}
    assert os_.resume == {}


def test_orch_state_allows_runtime_private_mcp_fields() -> None:
    # With aliases, underscore-prefixed runtime fields are parsed into the model
    state = OrchState(
        request="run mcp",
        _mcp_enabled=True,  # type: ignore[call-arg]
        _mcp_servers={"mock": {"command": "python"}},  # type: ignore[call-arg]
    )
    assert state.mcp_enabled_internal is True
    assert isinstance(state.mcp_servers_internal, dict)
