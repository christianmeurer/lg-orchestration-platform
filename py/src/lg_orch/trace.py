from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_run_id(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("_run_id"):
        return state
    return {**state, "_run_id": uuid.uuid4().hex}


def append_event(state: dict[str, Any], *, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    events = list(state.get("_trace_events", []))
    events.append({"ts_ms": now_ms(), "kind": kind, "data": data})
    return {**state, "_trace_events": events}


def write_run_trace(*, repo_root: Path, out_dir: Path, state: dict[str, Any]) -> Path:
    run_id = str(state.get("_run_id") or uuid.uuid4().hex)
    checkpoint_raw = state.get("_checkpoint", {})
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, dict) else {}
    verification_raw = state.get("verification", {})
    verification = dict(verification_raw) if isinstance(verification_raw, dict) else {}
    undo_raw = state.get("undo", {})
    undo = dict(undo_raw) if isinstance(undo_raw, dict) else {}
    approval_raw = state.get("_approval_context", {})
    approval = dict(approval_raw) if isinstance(approval_raw, dict) else {}
    recovery_packet_raw = state.get("recovery_packet", {})
    recovery_packet = (
        dict(recovery_packet_raw) if isinstance(recovery_packet_raw, dict) else None
    )
    correlation: dict[str, Any] = {}
    request_id_raw = state.get("_request_id")
    if isinstance(request_id_raw, str) and request_id_raw.strip():
        correlation["request_id"] = request_id_raw.strip()
    remote_api_context_raw = state.get("_remote_api_context", {})
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

    payload = {
        "run_id": run_id,
        "request": state.get("request"),
        "intent": state.get("intent"),
        "route": state.get("route"),
        "final": state.get("final"),
        "events": list(state.get("_trace_events", [])),
        "tool_results": list(state.get("tool_results", [])),
        "verification": verification,
        "recovery_packet": recovery_packet,
        "approval": approval,
        "loop_summaries": list(state.get("loop_summaries", [])),
        "checkpoint": checkpoint,
        "snapshots": list(state.get("snapshots", [])),
        "undo": undo,
        "correlation": correlation,
        "telemetry": dict(state.get("telemetry", {})),
        "provenance": list(state.get("provenance", [])),
    }
    try:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"failed to write trace: {out_path}") from exc
    return out_path
