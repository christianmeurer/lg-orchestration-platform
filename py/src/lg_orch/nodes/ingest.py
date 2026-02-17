from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.state import OrchState
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
}


def ingest(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    req = str(state.get("request", "")).strip()
    internal = {k: v for k, v in state.items() if str(k).startswith("_")}
    internal = ensure_run_id(internal)
    try:
        validated = OrchState(request=req).model_dump()
    except Exception as exc:
        log.error("ingest_validation_failed", error=str(exc))
        validated = {"request": req, **_ORCH_DEFAULTS}
    out = {**validated, **internal}
    return append_event(out, kind="node", data={"name": "ingest", "phase": "end"})
