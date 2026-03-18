from __future__ import annotations

from typing import Any

from lg_orch.graph import export_mermaid, route_after_policy_gate, route_after_verifier


def _state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"halt_reason": "", "context_reset_requested": False, "retry_target": None}
    s.update(overrides)
    return s


def test_route_after_policy_gate_stops_on_budget_exhaustion() -> None:
    assert route_after_policy_gate(_state(halt_reason="max_loops_exhausted")) == "reporter"


def test_route_after_policy_gate_stops_on_plan_iteration_exhaustion() -> None:
    assert route_after_policy_gate(_state(halt_reason="plan_max_iterations_exhausted")) == "reporter"


def test_route_after_policy_gate_prefers_context_reset() -> None:
    out = route_after_policy_gate(
        _state(context_reset_requested=True, retry_target="planner")
    )
    assert out == "context_builder"


def test_route_after_policy_gate_routes_to_planner_when_requested() -> None:
    assert route_after_policy_gate(_state(retry_target="planner")) == "planner"


def test_route_after_policy_gate_routes_to_coder_when_requested() -> None:
    assert route_after_policy_gate(_state(retry_target="coder")) == "coder"


def test_route_after_policy_gate_routes_to_context_builder_when_requested() -> None:
    assert route_after_policy_gate(_state(retry_target="context_builder")) == "context_builder"


def test_route_after_verifier_success_goes_to_reporter() -> None:
    assert route_after_verifier({"verification": {"ok": True}}) == "reporter"


def test_route_after_verifier_failure_reenters_budget_gate() -> None:
    assert route_after_verifier({"verification": {"ok": False}}) == "policy_gate"


def test_export_mermaid_includes_coder_node_and_edges() -> None:
    mermaid = export_mermaid()
    assert 'coder["coder"]' in mermaid
    assert "planner --> coder" in mermaid
    assert "coder --> executor" in mermaid
