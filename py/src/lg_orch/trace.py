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
    out_dir_abs = (repo_root / out_dir).resolve()
    out_dir_abs.mkdir(parents=True, exist_ok=True)
    out_path = out_dir_abs / f"run-{run_id}.json"

    payload = {
        "run_id": run_id,
        "request": state.get("request"),
        "intent": state.get("intent"),
        "final": state.get("final"),
        "events": list(state.get("_trace_events", [])),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
