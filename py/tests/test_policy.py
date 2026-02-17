from __future__ import annotations

from lg_orch.policy import PolicyDecision, decide_policy


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
