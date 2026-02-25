from __future__ import annotations

import re
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.state import PlannerOutput, PlanStep, ToolCall
from lg_orch.trace import append_event

_WORD_RE = re.compile(r"[a-z0-9']+")


def _classify_intent(request: str) -> str:
    r = request.lower()
    words = set(_WORD_RE.findall(r))
    if ("fix" in words) or ("fix" in r):
        return "code_change"
    if words.intersection({"implement", "add", "change", "refactor"}):
        return "code_change"
    if (
        "why" in words
        or "how" in words
        or "explain" in words
        or re.search(r"\bwhat\s+is\b", r) is not None
    ):
        return "question"
    if words.intersection({"research", "latest", "compare", "survey"}):
        return "research"
    if words.intersection({"debug", "error", "panic", "exception"}) or "stack trace" in r:
        return "debug"
    return "analysis"


def planner(state: dict[str, Any]) -> dict[str, Any]:
    log = get_logger()
    state = append_event(state, kind="node", data={"name": "planner", "phase": "start"})

    # Increment current_loop
    budgets = state.get("budgets", {})
    if not isinstance(budgets, dict):
        budgets = {}
    current_loop = budgets.get("current_loop", 0) + 1
    budgets["current_loop"] = current_loop
    state["budgets"] = budgets

    request = str(state.get("request", "")).strip()
    try:
        intent = _classify_intent(request)
        plan = PlannerOutput(
            steps=[
                PlanStep(
                    id="step-1",
                    description="Collect repository context.",
                    tools=[
                        ToolCall(tool="list_files", input={"path": ".", "recursive": False}),
                        ToolCall(
                            tool="search_files",
                            input={"path": ".", "regex": "TODO", "file_pattern": "*.py"},
                        ),
                    ],
                    expected_outcome="Top-level repository structure and TODOs captured.",
                    files_touched=[],
                )
            ],
            verification=[],
            rollback="No changes were made.",
        )
        out = {**state, "intent": intent, "plan": plan.model_dump()}
    except Exception as exc:
        log.error("planner_failed", error=str(exc))
        fallback_plan = {
            "steps": [],
            "verification": [],
            "rollback": "Plan generation failed.",
        }
        out = {**state, "intent": "analysis", "plan": fallback_plan}
    step_count = len(out.get("plan", {}).get("steps", []))
    return append_event(
        out,
        kind="node",
        data={"name": "planner", "phase": "end", "steps": step_count},
    )
