from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyDecision:
    allow_network: bool
    require_approval_for_mutations: bool


def decide_policy(*, network_default: str, require_approval_for_mutations: bool) -> PolicyDecision:
    allow_network = network_default.strip().lower() == "allow"
    return PolicyDecision(
        allow_network=allow_network,
        require_approval_for_mutations=require_approval_for_mutations,
    )
