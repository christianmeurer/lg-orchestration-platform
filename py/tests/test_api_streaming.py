"""Tests for lg_orch.api.streaming — SSE push and helper functions."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from lg_orch.api.streaming import (
    _emit_tool_stdout_lines,
    _run_streams,
    _run_streams_lock,
    _send_final_output,
    push_run_event,
)


# ---------------------------------------------------------------------------
# push_run_event
# ---------------------------------------------------------------------------


def test_push_run_event_noop_when_no_subscriber() -> None:
    """push_run_event must not raise when no queue exists for run_id."""
    push_run_event("nonexistent-run", {"kind": "test"})


def test_push_run_event_delivers_to_queue() -> None:
    import queue

    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    with _run_streams_lock:
        _run_streams["test-run"] = q
    try:
        push_run_event("test-run", {"kind": "node", "data": {"name": "planner"}})
        event = q.get_nowait()
        assert event == {"kind": "node", "data": {"name": "planner"}}
    finally:
        with _run_streams_lock:
            _run_streams.pop("test-run", None)


# ---------------------------------------------------------------------------
# _emit_tool_stdout_lines
# ---------------------------------------------------------------------------


class _FakeWfile:
    """Minimal wfile mock that collects written bytes."""

    def __init__(self) -> None:
        self.buf = BytesIO()

    def write(self, data: bytes) -> int:
        return self.buf.write(data)

    def flush(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return self.buf.getvalue()


def test_emit_tool_stdout_lines_ignores_non_tool_event() -> None:
    wfile = _FakeWfile()
    _emit_tool_stdout_lines({"kind": "node", "data": {}}, wfile)
    assert wfile.getvalue() == b""


def test_emit_tool_stdout_lines_emits_lines() -> None:
    wfile = _FakeWfile()
    event: dict[str, Any] = {
        "kind": "tool_result",
        "data": {
            "tool": "exec",
            "stdout": "line one\nline two\n",
        },
    }
    _emit_tool_stdout_lines(event, wfile)
    output = wfile.getvalue().decode()
    assert "tool_stdout" in output
    assert "line one" in output
    assert "line two" in output


def test_emit_tool_stdout_lines_skips_blank_lines() -> None:
    wfile = _FakeWfile()
    event: dict[str, Any] = {
        "kind": "tool_result",
        "data": {
            "tool": "exec",
            "stdout": "line one\n\n   \nline two",
        },
    }
    _emit_tool_stdout_lines(event, wfile)
    output = wfile.getvalue().decode()
    # Count the number of SSE frames (each ends with \n\n)
    frames = [f for f in output.split("\n\n") if f.strip()]
    assert len(frames) == 2


def test_emit_tool_stdout_lines_handles_no_stdout() -> None:
    wfile = _FakeWfile()
    _emit_tool_stdout_lines({"kind": "tool_result", "data": {"tool": "exec"}}, wfile)
    assert wfile.getvalue() == b""


def test_emit_tool_stdout_lines_handles_non_dict_data() -> None:
    wfile = _FakeWfile()
    _emit_tool_stdout_lines({"kind": "tool_result", "data": "not_a_dict"}, wfile)
    assert wfile.getvalue() == b""


# ---------------------------------------------------------------------------
# _send_final_output
# ---------------------------------------------------------------------------


def test_send_final_output_sends_json_frame() -> None:
    wfile = _FakeWfile()
    run: dict[str, Any] = {"trace": {"final": "All done."}}
    _send_final_output(run, wfile)
    output = wfile.getvalue().decode()
    assert "final_output" in output
    parsed = json.loads(output.split("data: ")[1].split("\n")[0])
    assert parsed["type"] == "final_output"
    assert parsed["text"] == "All done."


def test_send_final_output_noop_when_run_is_none() -> None:
    wfile = _FakeWfile()
    _send_final_output(None, wfile)
    assert wfile.getvalue() == b""


def test_send_final_output_noop_when_no_trace() -> None:
    wfile = _FakeWfile()
    _send_final_output({"status": "done"}, wfile)
    assert wfile.getvalue() == b""


def test_send_final_output_noop_when_final_is_empty() -> None:
    wfile = _FakeWfile()
    _send_final_output({"trace": {"final": ""}}, wfile)
    assert wfile.getvalue() == b""


def test_send_final_output_noop_when_trace_is_not_dict() -> None:
    wfile = _FakeWfile()
    _send_final_output({"trace": "not_a_dict"}, wfile)
    assert wfile.getvalue() == b""
