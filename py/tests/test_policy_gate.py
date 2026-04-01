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


def test_policy_gate_sets_allowed_write_paths() -> None:
    out = policy_gate(
        _base_state(
            _config_policy={
                "network_default": "deny",
                "require_approval_for_mutations": True,
                "allowed_write_paths": ["py/**", "docs/**"],
            }
        )
    )
    assert out["guards"]["allowed_write_paths"] == ["py/**", "docs/**"]


def test_policy_gate_initializes_budget_loop() -> None:
    out = policy_gate(_base_state())
    assert out["budgets"]["loop"]["remaining"] == 2
    assert out["budgets"]["current_loop"] == 1


def test_policy_gate_custom_max_loops() -> None:
    out = policy_gate(_base_state(_budget_max_loops=10))
    assert out["budgets"]["loop"]["remaining"] == 9
    assert out["budgets"]["current_loop"] == 1


def test_policy_gate_preserves_existing_budget() -> None:
    out = policy_gate(_base_state(budgets={"current_loop": 1, "max_loops": 3}))
    assert out["budgets"]["loop"]["remaining"] == 1
    assert out["budgets"]["current_loop"] == 2


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


def test_policy_gate_sets_halt_reason_when_budget_exhausted() -> None:
    out = policy_gate(_base_state(budgets={"current_loop": 3, "max_loops": 3}))
    assert out["halt_reason"] == "max_loops_exhausted"
    assert out["verification"]["ok"] is False
    assert out["verification"]["checks"][0]["name"] == "loop_budget"


def test_policy_gate_halts_when_plan_iterations_exhausted() -> None:
    out = policy_gate(
        _base_state(
            budgets={"current_loop": 1, "max_loops": 3},
            plan={"max_iterations": 1},
        )
    )
    assert out["halt_reason"] == "plan_max_iterations_exhausted"
    assert out["verification"]["failure_class"] == "plan_max_iterations_exhausted"


def test_policy_gate_handles_invalid_network_default() -> None:
    out = policy_gate(
        _base_state(
            _config_policy={
                "network_default": "invalid_value",
                "require_approval_for_mutations": False,
            }
        )
    )
    # Invalid values should default to "deny"
    assert out["guards"]["allow_network"] is False


def test_policy_gate_handles_non_int_max_loops() -> None:
    out = policy_gate(_base_state(_budget_max_loops="not_a_number"))
    # Should fall back to default of 3
    assert out["budgets"]["max_loops"] == 3


def test_policy_gate_handles_non_int_max_tool_calls() -> None:
    out = policy_gate(_base_state(_budget_max_tool_calls_per_loop="bad"))
    assert out["budgets"]["tool_calls_limit"] == 0


def test_policy_gate_handles_non_int_max_patch_bytes() -> None:
    out = policy_gate(_base_state(_budget_max_patch_bytes="bad"))
    assert out["budgets"]["patch_bytes_limit"] == 0


def test_policy_gate_handles_non_int_plan_max_iterations() -> None:
    out = policy_gate(
        _base_state(
            plan={"max_iterations": "not_a_number"},
        )
    )
    # Should fall back to 0 (not capping loops)
    assert "plan_max_iterations" not in out["budgets"]


def test_policy_gate_sets_tool_calls_and_patch_budgets() -> None:
    out = policy_gate(
        _base_state(
            _budget_max_tool_calls_per_loop=10,
            _budget_max_patch_bytes=50000,
        )
    )
    assert out["budgets"]["tool_calls_limit"] == 10
    assert out["budgets"]["tool_calls_used"] == 0
    assert out["budgets"]["patch_bytes_limit"] == 50000


def test_policy_gate_sets_context_budget() -> None:
    out = policy_gate(_base_state(_budget_context={"max_tokens": 4096}))
    assert out["budgets"]["context"] == {"max_tokens": 4096}
