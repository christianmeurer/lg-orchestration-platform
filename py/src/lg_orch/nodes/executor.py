from __future__ import annotations

from typing import Any

from lg_orch.logging import get_logger
from lg_orch.tools import RunnerClient
from lg_orch.trace import append_event


def _validate_base_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def executor(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    if bool(state.get("_runner_enabled", True)) is False:
        return state

    state = append_event(state, kind="node", data={"name": "executor", "phase": "start"})

    plan = state.get("plan")
    if not isinstance(plan, dict):
        return state

    runner_base_url = str(state.get("_runner_base_url", "http://127.0.0.1:8088"))
    if not _validate_base_url(runner_base_url):
        log.error("executor_invalid_base_url", url=runner_base_url)
        return append_event(
            state,
            kind="node",
            data={
                "name": "executor",
                "phase": "end",
                "error": "invalid_base_url",
            },
        )

    api_key = state.get("_runner_api_key")
    api_key_s = str(api_key).strip() if api_key is not None else None
    try:
        client = RunnerClient(base_url=runner_base_url, api_key=api_key_s)
    except Exception as exc:
        log.error("executor_client_init_failed", error=str(exc))
        return append_event(
            state,
            kind="node",
            data={
                "name": "executor",
                "phase": "end",
                "error": "client_init_failed",
            },
        )

    tool_results: list[dict[str, Any]] = list(state.get("tool_results", []))
    for step in plan.get("steps", []):
        try:
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
                    data={
                        "count": len(calls),
                        "tools": [str(c.get("tool")) for c in calls],
                    },
                )
        except Exception as exc:
            log.error(
                "executor_step_failed",
                error=str(exc),
                step_id=step.get("id"),
            )
            tool_results.append(
                {
                    "tool": "batch_execute",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": str(exc),
                    "timing_ms": 0,
                    "artifacts": {"error": "executor_failed"},
                }
            )
    out = {**state, "tool_results": tool_results}
    return append_event(out, kind="node", data={"name": "executor", "phase": "end"})
