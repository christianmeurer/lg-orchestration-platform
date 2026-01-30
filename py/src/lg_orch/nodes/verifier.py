from __future__ import annotations

from typing import Any

from lg_orch.state import VerifierReport
from lg_orch.trace import append_event


def verifier(state: dict[str, Any]) -> dict[str, Any]:
    state = append_event(state, kind="node", data={"name": "verifier", "phase": "start"})
    report = VerifierReport(ok=True, checks=[]).model_dump()
    out = {**state, "verification": report}
    return append_event(out, kind="node", data={"name": "verifier", "phase": "end", "ok": True})
