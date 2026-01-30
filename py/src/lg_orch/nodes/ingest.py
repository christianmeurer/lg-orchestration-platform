from __future__ import annotations

from typing import Any

from lg_orch.state import OrchState
from lg_orch.trace import append_event, ensure_run_id


def ingest(state: dict[str, Any]) -> dict[str, Any]:
    req = str(state.get("request", "")).strip()
    internal = {k: v for k, v in state.items() if str(k).startswith("_")}
    internal = ensure_run_id(internal)
    validated = OrchState(request=req).model_dump()
    out = {**validated, **internal}
    return append_event(out, kind="node", data={"name": "ingest", "phase": "end"})
