from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.state import VerifierReport
from lg_orch.trace import append_event


def verifier(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "verifier", "phase": "start"})
    try:
        report = VerifierReport(ok=True, checks=[]).model_dump()
    except Exception as exc:
        log.error("verifier_failed", error=str(exc))
        report = {"ok": False, "checks": []}
    ok = bool(report.get("ok", False))
    out = {**state, "verification": report}
    return append_event(out, kind="node", data={"name": "verifier", "phase": "end", "ok": ok})
