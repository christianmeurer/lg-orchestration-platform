from __future__ import annotations

from typing import Any

import pytest

from lg_orch.nodes.planner import _classify_intent, planner


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"request": "test request", "repo_context": {}}
    s.update(overrides)
    return s


# --- _classify_intent tests ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix the login bug", "code_change"),
        ("implement dark mode", "code_change"),
        ("add a new button", "code_change"),
        ("change the color", "code_change"),
        ("refactor the module", "code_change"),
        ("why does this fail", "question"),
        ("how does auth work", "question"),
        ("what is this function", "question"),
        ("explain the architecture", "question"),
        ("research best practices", "research"),
        ("latest updates on React", "research"),
        ("compare frameworks", "research"),
        ("survey existing solutions", "research"),
        ("debug the crash", "debug"),
        ("stack trace analysis", "debug"),
        ("error in production", "debug"),
        ("panic in the handler", "debug"),
        ("exception thrown here", "debug"),
        ("summarize the repo", "analysis"),
        ("show me the stats", "analysis"),
        ("list all files", "analysis"),
    ],
)
def test_classify_intent(text: str, expected: str) -> None:
    assert _classify_intent(text) == expected


def test_classify_intent_case_insensitive() -> None:
    assert _classify_intent("FIX THIS BUG") == "code_change"
    assert _classify_intent("EXPLAIN why") == "question"


def test_classify_intent_empty_string() -> None:
    assert _classify_intent("") == "analysis"


def test_classify_intent_priority_code_change_over_debug() -> None:
    # "fix" matches code_change first, even though "error" could match debug
    assert _classify_intent("fix the error") == "code_change"


# --- planner node tests ---


def test_planner_sets_intent() -> None:
    out = planner(_base_state(request="fix the bug"))
    assert out["intent"] == "code_change"


def test_planner_creates_plan() -> None:
    out = planner(_base_state())
    plan = out["plan"]
    assert isinstance(plan, dict)
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["id"] == "step-1"
    assert plan["rollback"] == "No changes were made."


def test_planner_plan_has_tool_calls() -> None:
    out = planner(_base_state())
    tools = out["plan"]["steps"][0]["tools"]
    assert len(tools) == 2
    assert tools[0]["tool"] == "list_files"
    assert tools[1]["tool"] == "search_files"


def test_planner_creates_trace_events() -> None:
    out = planner(_base_state())
    events = out.get("_trace_events", [])
    names = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "planner" in names


def test_planner_preserves_state() -> None:
    out = planner(_base_state(repo_context={"test": True}))
    assert out["repo_context"]["test"] is True
