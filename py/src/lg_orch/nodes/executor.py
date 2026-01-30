from __future__ import annotations

from typing import Any

from lg_orch.tools import RunnerClient
from lg_orch.trace import append_event


def executor(state: dict[str, Any]) -> dict[str, Any]:
    if bool(state.get("_runner_enabled", True)) is False:
        return state

    state = append_event(state, kind="node", data={"name": "executor", "phase": "start"})

    plan = state.get("plan")
    if not isinstance(plan, dict):
        return state

    runner_base_url = str(state.get("_runner_base_url", "http://127.0.0.1:8088"))
    client = RunnerClient(base_url=runner_base_url)

    tool_results: list[dict[str, Any]] = list(state.get("tool_results", []))
    for step in plan.get("steps", []):
        calls: list[dict[str, Any]] = []
        for tool_call in step.get("tools", []):
            calls.append(
                {
                    "tool": str(tool_call.get("tool")),
                    "input": dict(tool_call.get("input", {})),
                }
            )
        if calls:
            tool_results.extend(client.batch_execute_tools(calls=calls))
            state = append_event(
                state,
                kind="tools",
                data={"count": len(calls), "tools": [str(c.get("tool")) for c in calls]},
            )
    out = {**state, "tool_results": tool_results}
    return append_event(out, kind="node", data={"name": "executor", "phase": "end"})
