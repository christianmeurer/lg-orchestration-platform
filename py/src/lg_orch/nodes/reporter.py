from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.trace import append_event


def reporter(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "reporter", "phase": "start"})
    try:
        repo_context = state.get("repo_context", {})
        tool_results = state.get("tool_results", [])
        lines: list[str] = []
        lines.append(f"intent: {state.get('intent')}")
        lines.append(f"repo_root: {repo_context.get('repo_root')}")
        lines.append(f"top_level: {repo_context.get('top_level')}")
        if tool_results:
            lines.append(f"tool_calls: {len(tool_results)}")
        final = "\n".join(lines)
    except Exception as exc:
        log.error("reporter_failed", error=str(exc))
        final = f"error: reporter failed: {exc}"
    out = {**state, "final": final}
    return append_event(out, kind="node", data={"name": "reporter", "phase": "end"})
