# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""SSE streaming logic: push_run_event() and the /runs/{id}/stream endpoint."""
from __future__ import annotations

import json
import queue
import threading
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
        except OSError:
            return
    try:
        wfile.flush()
    except OSError:
        return

    # Completed run — send done sentinel and return
    if run.get("finished_at") is not None:
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
    try:
        for _ in range(1200):  # up to ~20 minutes at 1 s each
            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                # Fallback: poll run status for completion
                current = service.get_run(run_id)
                if current is None or current.get("finished_at") is not None:
                    try:
                        wfile.write(b'data: {"type":"done"}\n\n')
                        wfile.flush()
                    except OSError:
                        pass
                    return
                continue
            if event is None:
                # None is the sentinel meaning "stream is done"
                try:
                    wfile.write(b'data: {"type":"done"}\n\n')
                    wfile.flush()
                except OSError:
                    pass
                return
            try:
                data = json.dumps(event, ensure_ascii=False)
                wfile.write(f"data: {data}\n\n".encode())
                wfile.flush()
            except OSError:
                return
    except OSError:
        pass
    finally:
        with _run_streams_lock:
            _run_streams.pop(run_id, None)
        log.debug("spa_sse_stream_closed", run_id=run_id)
