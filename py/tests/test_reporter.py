from __future__ import annotations

from typing import Any

from lg_orch.nodes.reporter import reporter


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "test",
        "intent": "analysis",
        "repo_context": {"repo_root": "/tmp/test", "top_level": ["a", "b"]},
        "tool_results": [],
    }
    s.update(overrides)
    return s


def test_reporter_includes_intent() -> None:
    out = reporter(_base_state(intent="debug"))
    assert "intent: debug" in out["final"]


def test_reporter_includes_repo_root() -> None:
    out = reporter(_base_state())
    assert "repo_root: /tmp/test" in out["final"]


def test_reporter_includes_top_level() -> None:
    out = reporter(_base_state())
    assert "top_level:" in out["final"]


def test_reporter_includes_tool_count_when_results_exist() -> None:
    results = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "timing_ms": 0,
            "artifacts": {},
        },
        {
            "tool": "read_file",
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "timing_ms": 0,
            "artifacts": {},
        },
    ]
    out = reporter(_base_state(tool_results=results))
    assert "tool_calls: 2" in out["final"]


def test_reporter_omits_tool_count_when_no_results() -> None:
    out = reporter(_base_state(tool_results=[]))
    assert "tool_calls" not in out["final"]


def test_reporter_creates_trace_events() -> None:
    out = reporter(_base_state())
    events = out.get("_trace_events", [])
    names = [e["data"]["name"] for e in events if e["kind"] == "node"]
    assert "reporter" in names


def test_reporter_handles_missing_context() -> None:
    state: dict[str, Any] = {
        "request": "test",
        "intent": "analysis",
        "repo_context": {},
        "tool_results": [],
    }
    out = reporter(state)
    assert "intent: analysis" in out["final"]
    assert "repo_root: None" in out["final"]
