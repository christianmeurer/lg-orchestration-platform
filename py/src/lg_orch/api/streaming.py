# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""SSE streaming logic: push_run_event() and the /runs/{id}/stream endpoint."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

from lg_orch.logging import get_logger

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# SSE stream registry — one Queue per active /runs/{run_id}/stream client.
# Push None to signal stream end.
# ---------------------------------------------------------------------------
_run_streams: dict[str, queue.Queue[dict[str, Any] | None]] = {}
_run_streams_lock = threading.Lock()


def push_run_event(run_id: str, event: dict[str, Any]) -> None:
    """Push a trace event into the live SSE queue for *run_id*.

    Call from any thread; safe with ThreadingHTTPServer.  No-op when no
    browser is currently streaming *run_id*.  To signal stream end, put
    ``None`` directly into ``_run_streams[run_id]``.
    """
    with _run_streams_lock:
        q = _run_streams.get(run_id)
    if q is not None:
        q.put_nowait(event)


def _emit_tool_stdout_lines(event: dict[str, Any], wfile: Any) -> None:
    """Extract stdout from tool_result events and emit as tool_stdout SSE frames.

    Each non-empty line of stdout is sent as a separate SSE event with type
    ``tool_stdout``, enabling real-time terminal-style display in the SPA.
    """
    kind = event.get("kind", "")
    if kind not in ("tool_result", "tool_call"):
        return
    data = event.get("data", {})
    if not isinstance(data, dict):
        return
    stdout = data.get("stdout", "")
    if not stdout:
        return
    tool_name = str(data.get("tool", data.get("name", "")))
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.dumps(
                {"type": "tool_stdout", "tool": tool_name, "line": line},
                ensure_ascii=False,
            )
            wfile.write(f"data: {payload}\n\n".encode())
        except OSError:
            return


def _send_final_output(run: dict[str, Any] | None, wfile: Any) -> None:
    """Send a ``final_output`` SSE event if the run has a ``trace.final`` value."""
    if run is None:
        return
    trace_data = run.get("trace")
    if not isinstance(trace_data, dict):
        return
    final_text = str(trace_data.get("final", "")).strip()
    if not final_text:
        return
    try:
        payload = json.dumps({"type": "final_output", "text": final_text}, ensure_ascii=False)
        wfile.write(f"data: {payload}\n\n".encode())
        wfile.flush()
    except OSError:
        pass


def stream_new_sse(service: Any, run_id: str, wfile: Any) -> None:
    """Write new-format SSE events for a run to wfile.

    Protocol (each frame is ``data: <json>\\n\\n``):

    * Replays existing trace events from the trace file first.
    * For completed runs: sends one *done* sentinel and returns.
    * For active runs: drains ``_run_streams[run_id]`` with 1-second timeout;
      falls back to polling ``service.get_run()`` to detect completion.
    * Sends ``data: {"type":"done"}\\n\\n`` as the terminal frame.
    * If *run_id* is unknown, sends ``data: {"error":"not_found"}\\n\\n`` and returns.
    * On client disconnect (``OSError`` on ``wfile.write``), cleans up and returns.
    """
    from pathlib import Path

    log = get_logger()
    run = service.get_run(run_id)
    if run is None:
        try:
            data = json.dumps({"error": "not_found", "run_id": run_id}, ensure_ascii=False)
            wfile.write(f"data: {data}\n\n".encode())
            wfile.flush()
        except OSError:
            pass
        return

    # Replay existing trace events from the trace file
    trace_path = Path(str(run.get("trace_path", "")))
    trace_payload: dict[str, Any] | None = None
    if trace_path.is_file():
        try:
            raw = json.loads(trace_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                trace_payload = raw
        except (OSError, json.JSONDecodeError):
            pass

    existing_events: list[dict[str, Any]] = []
    if trace_payload is not None:
        events_raw = trace_payload.get("events", [])
        existing_events = [e for e in events_raw if isinstance(e, dict)]

    for ev in existing_events:
        try:
            data = json.dumps(ev, ensure_ascii=False)
            wfile.write(f"data: {data}\n\n".encode())
            # Emit tool_stdout lines from tool_result events
            _emit_tool_stdout_lines(ev, wfile)
        except OSError:
            return
    try:
        wfile.flush()
    except OSError:
        return

    # Completed run — send final output (if available) then done sentinel
    if run.get("finished_at") is not None:
        _send_final_output(run, wfile)
        try:
            wfile.write(b'data: {"type":"done"}\n\n')
            wfile.flush()
        except OSError:
            pass
        return

    # Active run — register a queue and forward live events
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    with _run_streams_lock:
        _run_streams[run_id] = q
    KEEPALIVE_INTERVAL = 30  # seconds between SSE keepalive comments
    last_event_time = time.monotonic()
    try:
        for _ in range(3000):  # up to ~50 minutes at 1 s each
            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                # Send keepalive comment to prevent proxy/CDN timeout
                now = time.monotonic()
                if now - last_event_time > KEEPALIVE_INTERVAL:
                    try:
                        wfile.write(b": keepalive\n\n")
                        wfile.flush()
                        last_event_time = now
                    except OSError:
                        return
                # Fallback: poll run status for completion
                current = service.get_run(run_id)
                if current is None or current.get("finished_at") is not None:
                    _send_final_output(current, wfile)
                    try:
                        wfile.write(b'data: {"type":"done"}\n\n')
                        wfile.flush()
                    except OSError:
                        pass
                    return
                continue
            if event is None:
                # None is the sentinel meaning "stream is done"
                current = service.get_run(run_id)
                _send_final_output(current, wfile)
                try:
                    wfile.write(b'data: {"type":"done"}\n\n')
                    wfile.flush()
                except OSError:
                    pass
                return
            try:
                data = json.dumps(event, ensure_ascii=False)
                wfile.write(f"data: {data}\n\n".encode())
                # Emit tool_stdout lines from live tool_result events
                _emit_tool_stdout_lines(event, wfile)
                wfile.flush()
                last_event_time = time.monotonic()
            except OSError:
                return
    except OSError:
        pass
    finally:
        with _run_streams_lock:
            _run_streams.pop(run_id, None)
        log.debug("spa_sse_stream_closed", run_id=run_id)
