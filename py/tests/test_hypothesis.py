from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from lg_orch.nodes.planner import _classify_intent
from lg_orch.policy import decide_policy
from lg_orch.state import OrchState, ToolCall
from lg_orch.trace import append_event, ensure_run_id

# --- Intent classification property tests ---

VALID_INTENTS = {"code_change", "question", "research", "debug", "analysis"}


@given(text=st.text(min_size=0, max_size=500))
@settings(max_examples=200)
def test_classify_intent_always_returns_valid_intent(text: str) -> None:
    result = _classify_intent(text)
    assert result in VALID_INTENTS


@given(text=st.text(min_size=0, max_size=500))
@settings(max_examples=200)
def test_classify_intent_is_deterministic(text: str) -> None:
    assert _classify_intent(text) == _classify_intent(text)


@given(
    prefix=st.text(max_size=50),
    suffix=st.text(max_size=50),
)
@settings(max_examples=100)
def test_classify_intent_fix_always_code_change(prefix: str, suffix: str) -> None:
    # If "fix" is present, it should always be code_change
    text = prefix + "fix" + suffix
    assert _classify_intent(text) == "code_change"


# --- Policy property tests ---


@given(
    network=st.sampled_from(["allow", "deny", "ALLOW", "DENY", "Allow", " allow ", " deny "]),
    approval=st.booleans(),
)
def test_decide_policy_is_consistent(network: str, approval: bool) -> None:
    d = decide_policy(network_default=network, require_approval_for_mutations=approval)
    assert d.allow_network == (network.strip().lower() == "allow")
    assert d.require_approval_for_mutations == approval


@given(network=st.text(min_size=0, max_size=100))
@settings(max_examples=200)
def test_decide_policy_never_crashes(network: str) -> None:
    d = decide_policy(network_default=network, require_approval_for_mutations=True)
    assert isinstance(d.allow_network, bool)


# --- Trace property tests ---


@given(kind=st.text(min_size=1, max_size=50), data_val=st.integers())
@settings(max_examples=100)
def test_append_event_always_increases_count(kind: str, data_val: int) -> None:
    state: dict[str, Any] = {}
    out = append_event(state, kind=kind, data={"val": data_val})
    assert len(out["_trace_events"]) == 1


@given(run_id=st.text(min_size=1, max_size=64))
@settings(max_examples=100)
def test_ensure_run_id_preserves_nonempty(run_id: str) -> None:
    state: dict[str, Any] = {"_run_id": run_id}
    out = ensure_run_id(state)
    assert out["_run_id"] == run_id


# --- State model property tests ---


@given(request=st.text(min_size=1, max_size=200))
@settings(max_examples=100)
def test_orch_state_accepts_any_string_request(request: str) -> None:
    os_ = OrchState(request=request)
    assert os_.request == request


@given(tool_name=st.text(min_size=1, max_size=50))
@settings(max_examples=100)
def test_tool_call_accepts_any_tool_name(tool_name: str) -> None:
    tc = ToolCall(tool=tool_name)
    assert tc.tool == tool_name
