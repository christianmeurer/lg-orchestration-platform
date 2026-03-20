# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.state import OrchState, validate_state
from lg_orch.trace import append_event, ensure_run_id

_ORCH_DEFAULTS: dict[str, Any] = {
    "intent": "analysis",
    "repo_context": {},
    "facts": [],
    "plan": None,
    "tool_results": [],
    "patches": [],
    "verification": None,
    "final": "",
    "guards": {},
    "budgets": {},
    "approvals": {},
    "security": {},
    "telemetry": {},
    "route": None,
    "active_handoff": None,
    "retry_target": None,
    "recovery_packet": None,
    "context_reset_requested": False,
    "plan_discarded": False,
    "plan_discard_reason": "",
    "halt_reason": "",
    "loop_summaries": [],
    "history_policy": {},
    "provenance": [],
    "checkpoint": {},
    "snapshots": [],
    "undo": {},
    "resume": {},
}


def ingest(state: OrchState | dict[str, Any]) -> dict[str, Any]:
    """Entry-point node for the orchestration graph.

    Accepts either an :class:`OrchState` instance (from LangGraph when the graph
    is typed) or a raw ``dict`` (from unit tests and legacy callers).  In both
    cases the output is a plain ``dict`` containing validated state fields merged
    with any underscore-prefixed internal keys.
    """
    log = get_logger()

    # --- Normalise input to a raw dict so the rest of the node is uniform ----
    if isinstance(state, OrchState):
        raw: dict[str, Any] = state.model_dump()
        raw.update(state.model_extra)          # re-attach _run_id, _lane, etc.
    else:
        raw = dict(state)

    # Preserve any LangGraph/caller-supplied underscore-prefixed internal keys.
    internal: dict[str, Any] = {k: v for k, v in raw.items() if str(k).startswith("_")}
    internal = ensure_run_id(internal)
    req = str(raw.get("request", "")).strip()

    try:
        validated_state = validate_state({"request": req})
        validated = validated_state.model_dump()
    except Exception as exc:
        log.error("ingest_validation_failed", error=str(exc))
        validated = {"request": req, **_ORCH_DEFAULTS}

    out = {**validated, **internal}
    return append_event(out, kind="node", data={"name": "ingest", "phase": "end"})
