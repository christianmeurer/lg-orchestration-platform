from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from lg_orch.trace import append_event, ensure_run_id, now_ms, write_run_trace


def test_now_ms_returns_positive_int() -> None:
    ts = now_ms()
    assert isinstance(ts, int)
    assert ts > 0


def test_ensure_run_id_generates_id() -> None:
    state: dict[str, Any] = {}
    out = ensure_run_id(state)
    assert "_run_id" in out
    assert isinstance(out["_run_id"], str)
    assert len(out["_run_id"]) == 32


def test_ensure_run_id_preserves_existing() -> None:
    state: dict[str, Any] = {"_run_id": "abc123"}
    out = ensure_run_id(state)
    assert out["_run_id"] == "abc123"


def test_ensure_run_id_replaces_falsy() -> None:
    state: dict[str, Any] = {"_run_id": ""}
    out = ensure_run_id(state)
    assert out["_run_id"] != ""
    assert len(out["_run_id"]) == 32


def test_ensure_run_id_does_not_mutate_original() -> None:
    state: dict[str, Any] = {"key": "val"}
    out = ensure_run_id(state)
    assert "_run_id" not in state
    assert "_run_id" in out


def test_append_event_adds_event() -> None:
    state: dict[str, Any] = {}
    out = append_event(state, kind="test", data={"x": 1})
    events = out["_trace_events"]
    assert len(events) == 1
    assert events[0]["kind"] == "test"
    assert events[0]["data"]["x"] == 1
    assert "ts_ms" in events[0]


def test_append_event_accumulates() -> None:
    state: dict[str, Any] = {}
    s1 = append_event(state, kind="a", data={})
    s2 = append_event(s1, kind="b", data={})
    assert len(s2["_trace_events"]) == 2


def test_append_event_does_not_mutate_original() -> None:
    state: dict[str, Any] = {"_trace_events": [{"ts_ms": 0, "kind": "old", "data": {}}]}
    out = append_event(state, kind="new", data={})
    assert len(state["_trace_events"]) == 1
    assert len(out["_trace_events"]) == 2


def test_write_run_trace_creates_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        state: dict[str, Any] = {
            "_run_id": "test123",
            "request": "hello",
            "intent": "analysis",
            "final": "done",
            "_trace_events": [{"ts_ms": 1000, "kind": "node", "data": {"name": "ingest"}}],
        }
        path = write_run_trace(repo_root=Path(td), out_dir=Path("traces"), state=state)
        assert path.exists()
        assert path.name == "run-test123.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["run_id"] == "test123"
        assert data["request"] == "hello"
        assert data["intent"] == "analysis"
        assert data["final"] == "done"
        assert len(data["events"]) == 1


def test_write_run_trace_creates_dirs() -> None:
    with tempfile.TemporaryDirectory() as td:
        state: dict[str, Any] = {"_run_id": "abc"}
        path = write_run_trace(repo_root=Path(td), out_dir=Path("deep/nested/dir"), state=state)
        assert path.exists()
        assert (Path(td) / "deep" / "nested" / "dir").is_dir()


def test_write_run_trace_auto_generates_run_id() -> None:
    with tempfile.TemporaryDirectory() as td:
        state: dict[str, Any] = {}
        path = write_run_trace(repo_root=Path(td), out_dir=Path("out"), state=state)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["run_id"]) == 32
