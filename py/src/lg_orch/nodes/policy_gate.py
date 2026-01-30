from __future__ import annotations

from typing import Any

from lg_orch.policy import decide_policy
from lg_orch.trace import append_event


def policy_gate(state: dict[str, Any]) -> dict[str, Any]:
    state = append_event(state, kind="node", data={"name": "policy_gate", "phase": "start"})
    budgets = dict(state.get("budgets", {}))
    guards = dict(state.get("guards", {}))
    cfg_policy = dict(state.get("_config_policy", {}))
    decision = decide_policy(
        network_default=str(cfg_policy.get("network_default", "deny")),
        require_approval_for_mutations=bool(cfg_policy.get("require_approval_for_mutations", True)),
    )
    guards["allow_network"] = decision.allow_network
    guards["require_approval_for_mutations"] = decision.require_approval_for_mutations
    budgets.setdefault("loop", {"remaining": int(state.get("_budget_max_loops", 3))})
    out = {**state, "guards": guards, "budgets": budgets}
    return append_event(out, kind="node", data={"name": "policy_gate", "phase": "end"})
