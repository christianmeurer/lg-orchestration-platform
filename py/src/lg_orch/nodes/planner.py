from __future__ import annotations

from typing import Any

from lg_orch.state import PlannerOutput, PlanStep, ToolCall
from lg_orch.trace import append_event


def _classify_intent(request: str) -> str:
    r = request.lower()
    if any(k in r for k in ("fix", "implement", "add", "change", "refactor")):
        return "code_change"
    if any(k in r for k in ("why", "how", "what is", "explain")):
        return "question"
    if any(k in r for k in ("research", "latest", "compare", "survey")):
        return "research"
    if any(k in r for k in ("debug", "stack trace", "error", "panic", "exception")):
        return "debug"
    return "analysis"


def planner(state: dict[str, Any]) -> dict[str, Any]:
    state = append_event(state, kind="node", data={"name": "planner", "phase": "start"})
    request = str(state.get("request", "")).strip()
    intent = _classify_intent(request)
    plan = PlannerOutput(
        steps=[
            PlanStep(
                id="step-1",
                description="Collect repository context.",
                tools=[ToolCall(tool="list_files", input={"path": ".", "recursive": False})],
                expected_outcome="Top-level repository structure captured.",
                files_touched=[],
            )
        ],
        verification=[],
        rollback="No changes were made.",
    )

    out = {**state, "intent": intent, "plan": plan.model_dump()}
    return append_event(out, kind="node", data={"name": "planner", "phase": "end", "steps": 1})
