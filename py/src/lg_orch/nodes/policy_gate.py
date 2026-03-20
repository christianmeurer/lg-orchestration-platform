# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.policy import decide_policy, enforce_loop_budget
from lg_orch.trace import append_event


def policy_gate(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "policy_gate", "phase": "start"})
    budgets = dict(state.get("budgets", {}))
    guards = dict(state.get("guards", {}))
    cfg_policy = dict(state.get("_config_policy", {}))
    try:
        network_default = str(cfg_policy.get("network_default", "deny"))
        require_approval = bool(cfg_policy.get("require_approval_for_mutations", True))
        allowed_write_paths_raw = cfg_policy.get("allowed_write_paths", ())
        allowed_write_paths = (
            tuple(entry.strip() for entry in allowed_write_paths_raw if isinstance(entry, str))
            if isinstance(allowed_write_paths_raw, (list, tuple))
            else ()
        )
        if network_default not in {"allow", "deny"}:
            log.warning("policy_gate_invalid_network_default", value=network_default)
            network_default = "deny"
        decision = decide_policy(
            network_default=network_default,
            require_approval_for_mutations=require_approval,
            allowed_write_paths=allowed_write_paths,
        )
        guards["allow_network"] = decision.allow_network
        guards["require_approval_for_mutations"] = decision.require_approval_for_mutations
        guards["allowed_write_paths"] = list(decision.allowed_write_paths)
    except Exception as exc:
        log.error("policy_gate_failed", error=str(exc))
        guards["allow_network"] = False
        guards["require_approval_for_mutations"] = True
        guards["allowed_write_paths"] = []

    raw_max_loops = state.get("_budget_max_loops", 3)
    raw_max_tool_calls = state.get("_budget_max_tool_calls_per_loop", 0)
    raw_max_patch_bytes = state.get("_budget_max_patch_bytes", 0)
    raw_context_budget = state.get("_budget_context", {})
    plan_raw = state.get("plan", {})
    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
    try:
        configured_max_loops = int(raw_max_loops)
    except (TypeError, ValueError):
        configured_max_loops = 3
    try:
        configured_max_tool_calls = int(raw_max_tool_calls)
    except (TypeError, ValueError):
        configured_max_tool_calls = 0
    try:
        configured_max_patch_bytes = int(raw_max_patch_bytes)
    except (TypeError, ValueError):
        configured_max_patch_bytes = 0
    plan_max_iterations_raw = plan.get("max_iterations")
    try:
        plan_max_iterations = (
            int(plan_max_iterations_raw) if plan_max_iterations_raw is not None else 0
        )
    except (TypeError, ValueError):
        plan_max_iterations = 0
    loop_decision = enforce_loop_budget(
        budgets=budgets,
        configured_max_loops=configured_max_loops,
        plan_max_iterations=plan_max_iterations if plan_max_iterations >= 1 else None,
    )
    budgets["max_loops"] = loop_decision.max_loops
    budgets["current_loop"] = loop_decision.current_loop
    budgets["loop"] = {"remaining": max(loop_decision.max_loops - loop_decision.current_loop, 0)}
    if plan_max_iterations >= 1:
        budgets["plan_max_iterations"] = plan_max_iterations
    budgets["tool_calls_limit"] = max(configured_max_tool_calls, 0)
    budgets["tool_calls_used"] = 0
    budgets["patch_bytes_limit"] = max(configured_max_patch_bytes, 0)
    budgets["context"] = dict(raw_context_budget) if isinstance(raw_context_budget, dict) else {}

    out = {
        **state,
        "guards": guards,
        "budgets": budgets,
        "halt_reason": loop_decision.halt_reason,
    }

    if not loop_decision.allow:
        out["verification"] = {
            "ok": False,
            "checks": [
                {
                    "name": "loop_budget",
                    "ok": False,
                    "tool": "policy_gate",
                    "exit_code": 1,
                    "summary": "Loop budget exhausted",
                }
            ],
            "acceptance_ok": False,
            "acceptance_checks": [],
            "retry_target": "planner",
            "plan_action": "keep",
            "failure_class": loop_decision.halt_reason,
            "failure_fingerprint": loop_decision.halt_reason,
            "recovery": None,
            "recovery_packet": None,
            "loop_summary": loop_decision.halt_reason,
        }

    return append_event(out, kind="node", data={"name": "policy_gate", "phase": "end"})
