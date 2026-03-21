# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def _state_get(state: Any, key: str, default: Any = None) -> Any:
    """Get a value from *state* whether it is a Pydantic BaseModel or a plain dict.

    For Pydantic v2 models with ``extra="allow"`` (e.g. :class:`~lg_orch.state.OrchState`),
    extra/underscore-prefixed fields live in ``model_extra`` but are also
    accessible via ``getattr``, so ``getattr(state, key, default)`` covers
    both declared fields and extra fields uniformly.
    """
    try:
        from pydantic import BaseModel  # local import — avoids circular deps

        if isinstance(state, BaseModel):
            return getattr(state, key, default)
    except ImportError:
        pass
    if isinstance(state, dict):
        return state.get(key, default)
    return default


def _state_as_dict(state: Any) -> dict[str, Any]:
    """Convert *state* to a plain dict, preserving extra/model_extra fields.

    Uses ``model_dump()`` plus ``model_extra`` so that Pydantic v2 models
    with ``extra="allow"`` (e.g. :class:`~lg_orch.state.OrchState`) do not
    silently drop the underscore-prefixed internal keys stored in
    ``model_extra``.
    """
    try:
        from pydantic import BaseModel  # local import — avoids circular deps

        if isinstance(state, BaseModel):
            base = state.model_dump()
            extra = getattr(state, "model_extra", None)
            if extra:
                base.update(extra)
            return base
    except ImportError:
        pass
    if isinstance(state, dict):
        return dict(state)
    return dict(state)


def ensure_run_id(state: Any) -> dict[str, Any]:
    """Return a partial state update that guarantees ``_run_id`` is set.

    Always returns ``{"_run_id": <value>}`` so callers can merge the result
    unconditionally.  When ``_run_id`` is already present the existing value
    is returned unchanged; otherwise a fresh UUID hex is generated.
    """
    existing = _state_get(state, "_run_id")
    if existing:
        return {"_run_id": existing}
    return {"_run_id": uuid.uuid4().hex}


def append_event(state: Any, *, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append a trace event and return the **full** state dict with the event
    appended to ``_trace_events``.

    Previously this returned only ``{"_trace_events": events}`` (a partial
    update).  LangGraph *does* merge partial dicts returned by nodes, but node
    implementations that do::

        state = append_event(state, ...)

    and then continue to mutate ``state`` would lose all other fields if this
    function returned a partial dict.  Returning the complete state is safe for
    both patterns: LangGraph-level merges work on the full dict, and
    call-site reassignments keep the full state intact.
    """
    existing = _state_get(state, "_trace_events", []) or []
    events = list(existing)
    events.append({"ts_ms": now_ms(), "kind": kind, "data": data})
    full = _state_as_dict(state)
    full["_trace_events"] = events
    return full


def write_run_trace(*, repo_root: Path, out_dir: Path, state: Any) -> Path:
    """Serialise the current run trace to a JSON file under *out_dir*.

    Accepts both :class:`~lg_orch.state.OrchState` Pydantic models and plain
    dicts so it can be called from any context in the graph.
    """
    run_id = str(_state_get(state, "_run_id") or uuid.uuid4().hex)

    checkpoint_raw = _state_get(state, "_checkpoint", {})
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, dict) else {}

    verification_raw = _state_get(state, "verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}

    undo_raw = _state_get(state, "undo", {})
    undo = dict(undo_raw) if isinstance(undo_raw, dict) else {}

    approval_raw = _state_get(state, "_approval_context", {})
    approval = dict(approval_raw) if isinstance(approval_raw, dict) else {}

    recovery_packet_raw = _state_get(state, "recovery_packet", {})
    recovery_packet = dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else None

    correlation: dict[str, Any] = {}
    request_id_raw = _state_get(state, "_request_id")
    if isinstance(request_id_raw, str) and request_id_raw.strip():
        correlation["request_id"] = request_id_raw.strip()

    remote_api_context_raw = _state_get(state, "_remote_api_context", {})
    remote_api_context = (
        dict(remote_api_context_raw) if isinstance(remote_api_context_raw, dict) else {}
    )
    for key in ("auth_subject", "client_ip"):
        value = remote_api_context.get(key)
        if isinstance(value, str) and value.strip():
            correlation[key] = value.strip()

    out_dir_abs = (repo_root / out_dir).resolve()
    try:
        out_dir_abs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"failed to create trace dir: {out_dir_abs}") from exc

    out_path = out_dir_abs / f"run-{run_id}.json"

    trace_events_raw = _state_get(state, "_trace_events", []) or []
    tool_results_raw = _state_get(state, "tool_results", []) or []
    loop_summaries_raw = _state_get(state, "loop_summaries", []) or []
    snapshots_raw = _state_get(state, "snapshots", []) or []
    provenance_raw = _state_get(state, "provenance", []) or []
    telemetry_raw = _state_get(state, "telemetry", {}) or {}

    payload = {
        "run_id": run_id,
        "request": _state_get(state, "request"),
        "intent": _state_get(state, "intent"),
        "route": _state_get(state, "route"),
        "final": _state_get(state, "final"),
        "events": list(trace_events_raw),
        "tool_results": list(tool_results_raw),
        "verification": verification,
        "recovery_packet": recovery_packet,
        "approval": approval,
        "loop_summaries": list(loop_summaries_raw),
        "checkpoint": checkpoint,
        "snapshots": list(snapshots_raw),
        "undo": undo,
        "correlation": correlation,
        "telemetry": dict(telemetry_raw),
        "provenance": list(provenance_raw),
    }
    try:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"failed to write trace: {out_path}") from exc
    return out_path
