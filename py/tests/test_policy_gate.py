from __future__ import annotations

from typing import Any

from lg_orch.nodes.policy_gate import policy_gate


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "test",
        "guards": {},
        "budgets": {},
        "_config_policy": {"network_default": "deny", "require_approval_for_mutations": True},
        "_budget_max_loops": 3,
    }
    s.update(overrides)
    return s


def test_policy_gate_sets_deny_network() -> None:
    out = policy_gate(_base_state())
    assert out["guards"]["allow_network"] is False


def test_policy_gate_sets_allow_network() -> None:
    out = policy_gate(
        _base_state(
            _config_policy={"network_default": "allow", "require_approval_for_mutations": False}
        )
    )
    assert out["guards"]["allow_network"] is True


def test_policy_gate_sets_require_approval() -> None:
    out = policy_gate(_base_state())
    assert out["guards"]["require_approval_for_mutations"] is True


def test_policy_gate_initializes_budget_loop() -> None:
    out = policy_gate(_base_state())
    assert out["budgets"]["loop"]["remaining"] == 3


def test_policy_gate_custom_max_loops() -> None:
    out = policy_gate(_base_state(_budget_max_loops=10))
    assert out["budgets"]["loop"]["remaining"] == 10


def test_policy_gate_preserves_existing_budget() -> None:
    out = policy_gate(_base_state(budgets={"loop": {"remaining": 1}}))
    assert out["budgets"]["loop"]["remaining"] == 1


def test_policy_gate_creates_trace_events() -> None:
    out = policy_gate(_base_state())
    events = out.get("_trace_events", [])
    kinds = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "policy_gate" in kinds


def test_policy_gate_defaults_when_config_missing() -> None:
    state: dict[str, Any] = {"request": "test", "guards": {}, "budgets": {}}
    out = policy_gate(state)
    assert out["guards"]["allow_network"] is False
    assert out["guards"]["require_approval_for_mutations"] is True
