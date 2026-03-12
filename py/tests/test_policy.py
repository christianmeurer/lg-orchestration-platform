from __future__ import annotations

from lg_orch.policy import LoopBudgetDecision, PolicyDecision, decide_policy, enforce_loop_budget


def test_decide_policy_deny_network() -> None:
    d = decide_policy(network_default="deny", require_approval_for_mutations=True)
    assert d.allow_network is False
    assert d.require_approval_for_mutations is True


def test_decide_policy_allow_network() -> None:
    d = decide_policy(network_default="allow", require_approval_for_mutations=False)
    assert d.allow_network is True
    assert d.require_approval_for_mutations is False


def test_decide_policy_case_insensitive() -> None:
    assert (
        decide_policy(network_default="ALLOW", require_approval_for_mutations=False).allow_network
        is True
    )
    assert (
        decide_policy(network_default="Allow", require_approval_for_mutations=False).allow_network
        is True
    )
    assert (
        decide_policy(network_default="DENY", require_approval_for_mutations=False).allow_network
        is False
    )


def test_decide_policy_whitespace_stripping() -> None:
    d = decide_policy(network_default="  allow  ", require_approval_for_mutations=True)
    assert d.allow_network is True


def test_decide_policy_unknown_defaults_to_deny() -> None:
    d = decide_policy(network_default="something_else", require_approval_for_mutations=False)
    assert d.allow_network is False


def test_policy_decision_frozen() -> None:
    d = PolicyDecision(allow_network=True, require_approval_for_mutations=False)
    try:
        d.allow_network = False  # type: ignore[misc]
        assert False, "should raise"  # noqa: B011
    except AttributeError:
        pass


def test_enforce_loop_budget_allows_and_increments() -> None:
    d = enforce_loop_budget(budgets={"current_loop": 0}, configured_max_loops=3)
    assert d == LoopBudgetDecision(allow=True, current_loop=1, max_loops=3, halt_reason="")


def test_enforce_loop_budget_halts_at_limit() -> None:
    d = enforce_loop_budget(budgets={"current_loop": 3, "max_loops": 3}, configured_max_loops=5)
    assert d.allow is False
    assert d.current_loop == 3
    assert d.max_loops == 3
    assert d.halt_reason == "max_loops_exhausted"


def test_enforce_loop_budget_uses_fallback_for_invalid_budget_values() -> None:
    d = enforce_loop_budget(
        budgets={"current_loop": "bad", "max_loops": "bad"},
        configured_max_loops=2,
    )
    assert d.allow is True
    assert d.current_loop == 1
    assert d.max_loops == 2


def test_enforce_loop_budget_clamps_invalid_configured_max() -> None:
    d = enforce_loop_budget(budgets={}, configured_max_loops=0)
    assert d.max_loops == 1
