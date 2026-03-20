from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Prometheus metrics — defined at module level (single-process, no multiprocess)
# ---------------------------------------------------------------------------
_LULA_RUNS_TOTAL: Counter = Counter(
    "lula_runs_total",
    "Total number of completed runs",
    ["lane", "status"],
)
_LULA_RUN_DURATION_SECONDS: Histogram = Histogram(
    "lula_run_duration_seconds",
    "Wall-clock duration of runs in seconds",
    ["lane"],
)
_LULA_ACTIVE_RUNS: Gauge = Gauge(
    "lula_active_runs",
    "Number of currently active runs",
)
# Placeholder — wired to LLM calls in Wave 13
_LULA_LLM_REQUESTS_TOTAL: Counter = Counter(
    "lula_llm_requests_total",
    "Total number of LLM requests",
    ["provider", "model", "status"],
)
# Placeholder — wired to tool calls in Wave 13
_LULA_TOOL_CALLS_TOTAL: Counter = Counter(
    "lula_tool_calls_total",
    "Total number of tool calls",
    ["tool_name", "status"],
)

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

from lg_orch.approval_policy import (
    ApprovalDecision,
    ApprovalEngine,
    ApprovalPolicy,
    ApprovalVote,
    QuorumApprovalPolicy,
    RoleApprovalPolicy,
    TimedApprovalPolicy,
)
from lg_orch.audit import AuditEvent, AuditLogger, build_sink, utc_now_iso
from lg_orch.auth import (
    AuthError,
    JWTSettings,
    _route_policy,
    authorize_stdlib,
    jwt_settings_from_config,
)
from lg_orch.config import load_config
from lg_orch.logging import get_logger, init_telemetry
from lg_orch.procedure_cache import ProcedureCache, _canonical_procedure_name
from lg_orch.run_store import RunStore

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_TRACE_OUT_DIR = Path("artifacts/remote-api")
_ALLOWED_VIEWS = {"classic", "console"}
_REQUEST_ID_HEADER = "X-Request-ID"

# ---------------------------------------------------------------------------
# SSE stream registry — one Queue per active /runs/{run_id}/stream client
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _non_empty_str(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    return value


def _normalized_run_id(raw: object) -> str | None:
    value = _non_empty_str(raw)
    if value is None:
        return None
    if not _RUN_ID_RE.fullmatch(value):
        return None
    return value


def _trace_path_for_run(repo_root: Path, trace_out_dir: Path, run_id: str) -> Path:
    resolved_out_dir = trace_out_dir.expanduser()
    trace_dir = resolved_out_dir if resolved_out_dir.is_absolute() else (repo_root / resolved_out_dir)
    return trace_dir.resolve() / f"run-{run_id}.json"


def _approval_token_for_challenge(challenge_id: str) -> str:
    """Generate a cryptographically signed approval token.

    Format: ``{challenge_id}|{iat}|{nonce}|{signature}``

    Matches the HMAC-SHA256 protocol expected by the Rust runner in
    ``rs/runner/src/approval.rs``.  When ``LG_RUNNER_APPROVAL_SECRET`` is
    unset or empty, falls back to the legacy plain-text format and logs a
    warning.
    """
    secret = os.environ.get("LG_RUNNER_APPROVAL_SECRET", "")
    if not secret:
        _log = get_logger()
        _log.warning(
            "approval_token_insecure",
            detail="LG_RUNNER_APPROVAL_SECRET not set; using deprecated plain-text token",
        )
        return f"approve:{challenge_id}"
    nonce = secrets.token_hex(16)
    iat = int(time.time())
    message = f"{challenge_id}|{iat}|{nonce}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}|{signature}"


def _tool_name_for_approval(*, operation_class: str, challenge_id: str) -> str:
    joined = f"{operation_class}:{challenge_id}".lower()
    if "apply_patch" in joined:
        return "apply_patch"
    if "exec" in joined:
        return "exec"
    return "apply_patch"


def _approval_summary(details: dict[str, Any]) -> str:
    operation_class = _non_empty_str(details.get("operation_class")) or "mutation"
    challenge_id = _non_empty_str(details.get("challenge_id"))
    reason = _non_empty_str(details.get("reason")) or "approval_required"
    summary = f"{operation_class} requires approval"
    if challenge_id is not None:
        summary = f"{summary} ({challenge_id})"
    if reason not in {"approval_required", "challenge_required", "missing_approval_token"}:
        summary = f"{summary}: {reason}"
    return summary


def _approval_state_from_trace(trace_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(trace_payload, dict):
        return {
            "pending": False,
            "summary": "",
            "details": {},
            "history": [],
            "thread_id": "",
            "checkpoint_id": "",
        }

    checkpoint_raw = trace_payload.get("checkpoint", {})
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, dict) else {}
    thread_id = str(checkpoint.get("thread_id", "")).strip()
    checkpoint_id = str(
        checkpoint.get("latest_checkpoint_id") or checkpoint.get("resume_checkpoint_id") or ""
    ).strip()

    approval_raw = trace_payload.get("approval", {})
    approval = dict(approval_raw) if isinstance(approval_raw, dict) else {}
    has_explicit_pending = "pending" in approval
    pending_details_raw = approval.get("pending_details", {})
    pending_details = dict(pending_details_raw) if isinstance(pending_details_raw, dict) else {}
    history_raw = approval.get("history", [])
    history = [dict(entry) for entry in history_raw if isinstance(entry, dict)] if isinstance(history_raw, list) else []
    pending = bool(approval.get("pending", False))
    summary = str(approval.get("summary", "")).strip()

    if not has_explicit_pending and not pending_details:
        tool_results_raw = trace_payload.get("tool_results", [])
        tool_results = [entry for entry in tool_results_raw if isinstance(entry, dict)] if isinstance(tool_results_raw, list) else []
        for result in reversed(tool_results):
            artifacts_raw = result.get("artifacts", {})
            artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
            if str(artifacts.get("error", "")).strip().lower() != "approval_required":
                continue
            approval_details_raw = artifacts.get("approval", {})
            if isinstance(approval_details_raw, dict):
                pending_details = dict(approval_details_raw)
                pending = True
                break

    if pending_details and not summary:
        summary = _approval_summary(pending_details)

    return {
        "pending": pending,
        "summary": summary,
        "details": pending_details,
        "history": history,
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id,
    }


def _apply_approval_state_to_record(record: RunRecord, approval_state: dict[str, Any]) -> None:
    thread_id = str(approval_state.get("thread_id", "")).strip()
    checkpoint_id = str(approval_state.get("checkpoint_id", "")).strip()
    if thread_id:
        record.thread_id = thread_id
    if checkpoint_id:
        record.checkpoint_id = checkpoint_id

    history_raw = approval_state.get("history", [])
    if isinstance(history_raw, list) and history_raw:
        record.approval_history = [dict(entry) for entry in history_raw if isinstance(entry, dict)]

    pending_details_raw = approval_state.get("details", {})
    pending_details = dict(pending_details_raw) if isinstance(pending_details_raw, dict) else {}
    if bool(approval_state.get("pending", False)) and not record.cancel_requested:
        record.status = "suspended"
        record.pending_approval = True
        record.pending_approval_summary = str(approval_state.get("summary", "")).strip()
        record.pending_approval_details = pending_details
    else:
        record.pending_approval = False
        record.pending_approval_summary = ""
        record.pending_approval_details = {}


def _apply_trace_state_to_payload(
    payload: dict[str, Any],
    trace_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(payload)
    out["trace_ready"] = trace_payload is not None
    out["trace"] = trace_payload
    approval_state = _approval_state_from_trace(trace_payload)
    out["thread_id"] = str(approval_state.get("thread_id", "")).strip() or str(out.get("thread_id", "")).strip()
    out["checkpoint_id"] = str(approval_state.get("checkpoint_id", "")).strip() or str(out.get("checkpoint_id", "")).strip()
    out["pending_approval"] = bool(approval_state.get("pending", out.get("pending_approval", False)))
    out["pending_approval_summary"] = str(
        approval_state.get("summary", out.get("pending_approval_summary", ""))
    ).strip()
    details_raw = approval_state.get("details", {})
    out["pending_approval_details"] = dict(details_raw) if isinstance(details_raw, dict) else {}
    history_raw = approval_state.get("history", [])
    if isinstance(history_raw, list) and history_raw:
        out["approval_history"] = [dict(entry) for entry in history_raw if isinstance(entry, dict)]
    else:
        out.setdefault("approval_history", [])
    if out["pending_approval"] and str(out.get("status", "")).strip() not in {"cancelled", "rejected"}:
        out["status"] = "suspended"
    return out


def _write_trace_approval_state(
    trace_path: Path,
    *,
    pending: bool,
    pending_details: dict[str, Any] | None,
    history: list[dict[str, Any]],
    last_decision: dict[str, Any] | None,
) -> None:
    if not trace_path.is_file():
        return
    try:
        payload_raw = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload_raw, dict):
        return

    approval_raw = payload_raw.get("approval", {})
    approval = dict(approval_raw) if isinstance(approval_raw, dict) else {}
    approval["pending"] = pending
    approval["history"] = history
    if pending_details:
        approval["pending_details"] = pending_details
        approval["summary"] = _approval_summary(pending_details)
    else:
        approval.pop("pending_details", None)
        approval["summary"] = ""
    if last_decision is not None:
        approval["last_decision"] = last_decision
    payload_raw["approval"] = approval
    try:
        trace_path.write_text(json.dumps(payload_raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def _resume_argv(record: RunRecord) -> list[str]:
    argv: list[str] = []
    idx = 0
    while idx < len(record.argv):
        token = record.argv[idx]
        if token == "--resume":
            idx += 1
            continue
        if token in {"--thread-id", "--checkpoint-id"}:
            idx += 2
            continue
        argv.append(token)
        idx += 1
    argv.append("--resume")
    if record.thread_id:
        argv.extend(["--thread-id", record.thread_id])
    if record.checkpoint_id:
        argv.extend(["--checkpoint-id", record.checkpoint_id])
    return argv


def _semantic_memories_from_trace(
    trace_payload: dict[str, Any] | None,
    *,
    request: str,
) -> list[dict[str, Any]]:
    if not isinstance(trace_payload, dict):
        return []

    memories: list[dict[str, Any]] = []
    request_text = str(trace_payload.get("request", request)).strip()
    if request_text:
        memories.append(
            {
                "kind": "request",
                "source": "user_request",
                "summary": request_text,
            }
        )

    final_text = str(trace_payload.get("final", "")).strip()
    if final_text:
        memories.append(
            {
                "kind": "final_output",
                "source": "reporter",
                "summary": final_text[:600],
            }
        )

    loop_summaries_raw = trace_payload.get("loop_summaries", [])
    loop_summaries = [entry for entry in loop_summaries_raw if isinstance(entry, dict)] if isinstance(loop_summaries_raw, list) else []
    for entry in loop_summaries[-5:]:
        summary = str(entry.get("summary", "")).strip()
        if not summary:
            continue
        memories.append(
            {
                "kind": "loop_summary",
                "source": str(entry.get("failure_class", "")).strip() or "loop_summary",
                "summary": summary,
            }
        )

    approval_state = _approval_state_from_trace(trace_payload)
    approval_summary = str(approval_state.get("summary", "")).strip()
    if approval_summary:
        memories.append(
            {
                "kind": "approval_summary",
                "source": str(approval_state.get("details", {}).get("operation_class", "approval")).strip() or "approval",
                "summary": approval_summary,
            }
        )
    history_raw = approval_state.get("history", [])
    history = [entry for entry in history_raw if isinstance(entry, dict)] if isinstance(history_raw, list) else []
    for entry in history[-5:]:
        decision = str(entry.get("decision", "")).strip() or "approval"
        actor = str(entry.get("actor", "")).strip() or "operator"
        rationale = str(entry.get("rationale", "")).strip()
        challenge = str(entry.get("challenge_id", "")).strip()
        summary = f"{decision} by {actor}"
        if challenge:
            summary = f"{summary} for {challenge}"
        if rationale:
            summary = f"{summary}: {rationale}"
        memories.append(
            {
                "kind": "approval_history",
                "source": decision,
                "summary": summary,
            }
        )

    return memories


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status, _JSON_CONTENT_TYPE, body


def _audit_action_and_resource(
    *,
    method: str,
    route: str,
    path_parts: list[str],
    status: int,
) -> tuple[str, str | None]:
    """Derive (action, resource_id) for an audit event from request metadata."""
    # run.create
    if method == "POST" and route in {"/v1/runs", "/runs", "/runs/"}:
        return "run.create", None

    # run.list
    if method == "GET" and route in {"/v1/runs", "/runs", "/runs/"}:
        return "run.list", None

    # run.search
    if route == "/runs/search" and method == "GET":
        return "run.search", None

    # GET /v1/runs/{run_id} or /runs/{run_id}
    if method == "GET" and len(path_parts) == 3 and path_parts[:2] == ["v1", "runs"]:
        return "run.read", path_parts[2]
    if method == "GET" and len(path_parts) == 2 and path_parts[0] == "runs":
        return "run.read", path_parts[1]

    # cancel
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "cancel":
        return "run.cancel", path_parts[2]
    if method == "POST" and len(path_parts) == 3 and path_parts[0] == "runs" and path_parts[2] == "cancel":
        return "run.cancel", path_parts[1]

    # approve / reject
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "approve":
        return "run.approve", path_parts[2]
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "reject":
        return "run.approve", path_parts[2]
    if method == "POST" and len(path_parts) == 3 and path_parts[0] == "runs" and path_parts[2] in {"approve", "reject"}:
        return "run.approve", path_parts[1]

    # logs / stream
    if len(path_parts) >= 3 and path_parts[-1] in {"logs", "stream"}:
        rid = path_parts[-2] if len(path_parts) >= 3 else None
        return "run.read", rid

    return "api.request", None


def _spawn_run_subprocess(
    *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def _start_daemon_thread(*, target: Callable[[], None], name: str) -> None:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()


def _request_id_from_value(raw: object) -> str:
    value = _non_empty_str(raw)
    return value or uuid.uuid4().hex


def _request_client_ip(
    *,
    client_address: tuple[str, int] | None,
    forwarded_for: str | None,
    trust_forwarded_headers: bool,
) -> str:
    if trust_forwarded_headers and forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first
    if client_address is not None:
        return str(client_address[0])
    return ""


def _request_scheme(*, forwarded_proto: str | None, trust_forwarded_headers: bool) -> str:
    if trust_forwarded_headers and forwarded_proto:
        first = forwarded_proto.split(",", 1)[0].strip().lower()
        if first:
            return first
    return "http"


def _authorize_request(
    *,
    route: str,
    auth_mode: str,
    expected_bearer_token: str | None,
    authorization_header: str | None,
    allow_unauthenticated_healthz: bool,
) -> tuple[str, tuple[int, str, bytes] | None]:
    if route == "/healthz" and allow_unauthenticated_healthz:
        return "", None
    # /metrics is always public — required for K8s Prometheus scraping
    if route == "/metrics":
        return "", None
    if auth_mode == "off":
        return "", None
    if auth_mode != "bearer":
        return "", _json_response(500, {"error": "unsupported_auth_mode"})
    if expected_bearer_token is None:
        return "", _json_response(503, {"error": "remote_api_auth_not_configured"})
    auth = _non_empty_str(authorization_header)
    if auth is None or auth[:7].lower() != "bearer ":
        return "", _json_response(401, {"error": "missing_bearer_token"})
    given = auth[7:].strip()
    if not hmac.compare_digest(given, expected_bearer_token):
        return "", _json_response(403, {"error": "invalid_bearer_token"})
    return "bearer", None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Token-bucket rate limiter (stdlib only, thread-safe)."""

    def __init__(self, *, capacity: int, rate: float) -> None:
        self._capacity = float(capacity)
        self._rate = float(rate)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


@dataclass(slots=True)
class RunRecord:
    run_id: str
    request: str
    argv: list[str]
    trace_out_dir: Path
    trace_path: Path
    process: subprocess.Popen[str]
    created_at: str
    started_at: str
    status: str = "running"
    finished_at: str | None = None
    exit_code: int | None = None
    logs: list[str] = field(default_factory=list)
    request_id: str = ""
    auth_subject: str = ""
    client_ip: str = ""
    cancel_requested: bool = False
    thread_id: str = ""
    checkpoint_id: str = ""
    pending_approval: bool = False
    pending_approval_summary: str = ""
    pending_approval_details: dict[str, Any] = field(default_factory=dict)
    approval_history: list[dict[str, Any]] = field(default_factory=list)


def _stream_new_sse(service: RemoteAPIService, run_id: str, wfile: Any) -> None:
    """New-format SSE stream for the standalone SPA at ``/runs/{run_id}/stream``.

    Protocol (each frame is ``data: <json>\\n\\n``):

    * Replays existing trace events from the trace file first.
    * For completed runs: sends one *done* sentinel and returns.
    * For active runs: drains ``_run_streams[run_id]`` with 1-second timeout;
      falls back to polling ``service.get_run()`` to detect completion.
    * Sends ``data: {"type":"done"}\\n\\n`` as the terminal frame.
    * If *run_id* is unknown, sends ``data: {"error":"not_found"}\\n\\n`` and returns.
    * On client disconnect (``OSError`` on ``wfile.write``), cleans up and returns.
    """
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


class RemoteAPIService:
    def __init__(
        self,
        *,
        repo_root: Path,
        run_store: RunStore | None = None,
        rate_limiter: _RateLimiter | None = None,
        procedure_cache: ProcedureCache | None = None,
        namespace: str = "",
    ) -> None:
        self._repo_root = repo_root.resolve()
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}
        self._approval_decisions: dict[str, ApprovalDecision] = {}
        self._log = get_logger()
        self._run_store = run_store
        self._rate_limiter = rate_limiter
        self._procedure_cache = procedure_cache
        self._namespace = namespace.strip()
        # healing loop background tasks: loop_id -> asyncio.Task
        self._healing_tasks: dict[str, asyncio.Task[None]] = {}
        self._healing_loops: dict[str, Any] = {}  # loop_id -> HealingLoop instance
        # Monotonic start times for run duration tracking
        self._run_start_times: dict[str, float] = {}

    def create_run(
        self,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        auth_subject: str = "",
        client_ip: str = "",
    ) -> dict[str, Any]:
        request = _non_empty_str(payload.get("request"))
        if request is None:
            raise ValueError("invalid_request")

        provided_run_id = payload.get("run_id")
        run_id = _normalized_run_id(provided_run_id)
        if provided_run_id is not None and run_id is None:
            raise ValueError("invalid_run_id")
        if run_id is None:
            run_id = uuid.uuid4().hex

        view = _non_empty_str(payload.get("view")) or "console"
        if view not in _ALLOWED_VIEWS:
            raise ValueError("invalid_view")

        trace_out_dir_value = _non_empty_str(payload.get("trace_out_dir"))
        trace_out_dir = (
            Path(trace_out_dir_value).expanduser()
            if trace_out_dir_value is not None
            else _DEFAULT_TRACE_OUT_DIR
        )
        trace_path = _trace_path_for_run(self._repo_root, trace_out_dir, run_id)

        argv = [
            sys.executable,
            "-m",
            "lg_orch.main",
            "run",
            request,
            "--repo-root",
            str(self._repo_root),
            "--trace",
            "--run-id",
            run_id,
            "--trace-out-dir",
            str(trace_out_dir),
            "--view",
            view,
        ]

        for key, flag in (
            ("profile", "--profile"),
            ("runner_base_url", "--runner-base-url"),
            ("thread_id", "--thread-id"),
            ("checkpoint_id", "--checkpoint-id"),
        ):
            value = _non_empty_str(payload.get(key))
            if value is not None:
                argv.extend([flag, value])

        if bool(payload.get("resume")):
            argv.append("--resume")

        created_at = _utc_now()
        run_env: dict[str, str] | None = None
        if request_id or auth_subject or client_ip:
            run_env = dict(os.environ)
            if request_id:
                run_env["LG_REQUEST_ID"] = request_id
            if auth_subject:
                run_env["LG_REMOTE_API_AUTH_SUBJECT"] = auth_subject
            if client_ip:
                run_env["LG_REMOTE_API_CLIENT_IP"] = client_ip
        with self._lock:
            if run_id in self._runs:
                raise ValueError("duplicate_run_id")
            process = _spawn_run_subprocess(argv=argv, cwd=self._repo_root, env=run_env)
            self._runs[run_id] = RunRecord(
                run_id=run_id,
                request=request,
                argv=argv,
                trace_out_dir=trace_out_dir,
                trace_path=trace_path,
                process=process,
                created_at=created_at,
                started_at=created_at,
                request_id=request_id,
                auth_subject=auth_subject,
                client_ip=client_ip,
                thread_id=_non_empty_str(payload.get("thread_id")) or "",
                checkpoint_id=_non_empty_str(payload.get("checkpoint_id")) or "",
            )

        if self._run_store is not None:
            with self._lock:
                _record = self._runs.get(run_id)
                if _record is not None:
                    self._run_store.upsert(self._summary_payload_locked(_record))
        _LULA_ACTIVE_RUNS.inc()
        with self._lock:
            self._run_start_times[run_id] = time.monotonic()
        _start_daemon_thread(
            target=lambda: self._capture_process_output(run_id),
            name=f"lg-orch-run-{run_id}",
        )
        self._log.info(
            "remote_api_run_started",
            run_id=run_id,
            trace_path=str(trace_path),
            repo_root=str(self._repo_root),
            request_id=request_id,
            auth_subject=auth_subject,
            client_ip=client_ip,
        )
        detail = self.get_run(run_id)
        if detail is None:
            raise RuntimeError("run_not_found")
        return detail

    def search_runs(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        if self._run_store is None:
            return []
        return self._run_store.search_runs(query, limit=limit)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            in_memory = {r.run_id: self._summary_payload_locked(r) for r in self._runs.values()}
        if self._run_store is not None:
            persisted = {r["run_id"]: r for r in self._run_store.list_runs()}
            # merge: in-memory wins for running/cancelling; persisted fills completed gaps
            merged: dict[str, dict[str, Any]] = {}
            for run_id, p in persisted.items():
                merged[run_id] = p
            for run_id, m in in_memory.items():
                merged[run_id] = m
            return sorted(merged.values(), key=lambda x: x.get("created_at", ""), reverse=True)
        return sorted(in_memory.values(), key=lambda x: x.get("created_at", ""), reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None
        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is not None:
                payload = self._summary_payload_locked(record)
            else:
                payload = None
        
        if payload is None and self._run_store is not None:
            persisted = self._run_store.get_run(normalized_run_id)
            if persisted is not None:
                payload = dict(persisted)
        
        if payload is None:
            return None

        trace_payload = self._load_trace(Path(payload["trace_path"]))
        return _apply_trace_state_to_payload(payload, trace_payload)

    def get_logs(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None
        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is None:
                return None
            self._refresh_record_locked(record)
            return {
                "run_id": record.run_id,
                "status": record.status,
                "exit_code": record.exit_code,
                "cancel_requested": record.cancel_requested,
                "logs": list(record.logs),
            }

    def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None

        process: subprocess.Popen[str] | None = None
        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is None:
                return None
            self._refresh_record_locked(record)
            if record.finished_at is not None:
                return self._summary_payload_locked(record)
            if not record.cancel_requested:
                record.cancel_requested = True
                record.status = "cancelling"
                process = record.process

        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError as exc:
                self._log.warning(
                    "remote_api_run_cancel_terminate_failed",
                    run_id=normalized_run_id,
                    error=str(exc),
                )

        payload = self.get_run(normalized_run_id)
        if payload is None:
            return None
        self._log.info(
            "remote_api_run_cancel_requested",
            run_id=normalized_run_id,
            request_id=str(payload.get("request_id", "")),
        )
        return payload

    def approve_run(self, run_id: str, payload: dict[str, Any], *, auth_subject: str = "") -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None

        actor = _non_empty_str(payload.get("actor")) or auth_subject or "operator"
        rationale = _non_empty_str(payload.get("rationale")) or ""
        provided_challenge_id = _non_empty_str(payload.get("challenge_id"))

        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is None:
                return None
            self._refresh_record_locked(record)
            if not record.pending_approval:
                raise ValueError("approval_not_pending")
            challenge_id = _non_empty_str(record.pending_approval_details.get("challenge_id"))
            if challenge_id is None:
                raise ValueError("approval_challenge_missing")
            if provided_challenge_id is not None and provided_challenge_id != challenge_id:
                raise ValueError("challenge_id_mismatch")
            if not record.thread_id or not record.checkpoint_id:
                raise ValueError("resume_checkpoint_missing")

            approval_entry = {
                "decision": "approved",
                "actor": actor,
                "rationale": rationale,
                "challenge_id": challenge_id,
                "ts": _utc_now(),
            }
            operation_class = _non_empty_str(record.pending_approval_details.get("operation_class")) or "apply_patch"
            tool_name = _tool_name_for_approval(operation_class=operation_class, challenge_id=challenge_id)
            approval_token = {
                "challenge_id": challenge_id,
                "token": _approval_token_for_challenge(challenge_id),
            }
            approvals_payload = {tool_name: approval_token, "mutations": {tool_name: approval_token}}
            approval_history = list(record.approval_history)
            approval_history.append(approval_entry)

            run_env = dict(os.environ)
            if record.request_id:
                run_env["LG_REQUEST_ID"] = record.request_id
            if record.auth_subject:
                run_env["LG_REMOTE_API_AUTH_SUBJECT"] = record.auth_subject
            if record.client_ip:
                run_env["LG_REMOTE_API_CLIENT_IP"] = record.client_ip
            run_env["LG_RESUME_APPROVALS_JSON"] = json.dumps(approvals_payload, ensure_ascii=False)
            run_env["LG_APPROVAL_CONTEXT_JSON"] = json.dumps(
                {
                    "pending": False,
                    "history": approval_history,
                    "last_decision": approval_entry,
                },
                ensure_ascii=False,
            )
            argv = _resume_argv(record)
            process = _spawn_run_subprocess(argv=argv, cwd=self._repo_root, env=run_env)

            record.argv = argv
            record.process = process
            record.started_at = _utc_now()
            record.finished_at = None
            record.exit_code = None
            record.status = "running"
            record.pending_approval = False
            record.pending_approval_summary = ""
            record.pending_approval_details = {}
            record.approval_history = approval_history
            record.logs.append(f"[approval] approved by {actor}")
            payload_out = self._summary_payload_locked(record)
            trace_path = record.trace_path

        _write_trace_approval_state(
            trace_path,
            pending=False,
            pending_details=None,
            history=approval_history,
            last_decision=approval_entry,
        )
        if self._run_store is not None:
            self._run_store.upsert(payload_out)
        _start_daemon_thread(
            target=lambda: self._capture_process_output(normalized_run_id),
            name=f"lg-orch-run-{normalized_run_id}",
        )
        resumed = self.get_run(normalized_run_id)
        if resumed is None:
            raise RuntimeError("run_not_found")
        self._log.info("remote_api_run_resumed", run_id=normalized_run_id, actor=actor)
        return resumed

    def reject_run(self, run_id: str, payload: dict[str, Any], *, auth_subject: str = "") -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None

        actor = _non_empty_str(payload.get("actor")) or auth_subject or "operator"
        rationale = _non_empty_str(payload.get("rationale")) or ""

        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is None:
                return None
            self._refresh_record_locked(record)
            if not record.pending_approval:
                raise ValueError("approval_not_pending")

            challenge_id = _non_empty_str(record.pending_approval_details.get("challenge_id")) or ""
            approval_entry = {
                "decision": "rejected",
                "actor": actor,
                "rationale": rationale,
                "challenge_id": challenge_id,
                "ts": _utc_now(),
            }
            approval_history = list(record.approval_history)
            approval_history.append(approval_entry)
            record.pending_approval = False
            record.pending_approval_summary = ""
            record.pending_approval_details = {}
            record.approval_history = approval_history
            record.finished_at = _utc_now()
            record.exit_code = 1
            record.status = "rejected"
            record.logs.append(f"[approval] rejected by {actor}")
            payload_out = self._summary_payload_locked(record)
            trace_path = record.trace_path

        _write_trace_approval_state(
            trace_path,
            pending=False,
            pending_details=None,
            history=approval_history,
            last_decision=approval_entry,
        )
        if self._run_store is not None:
            self._run_store.upsert(payload_out)
        self._log.info("remote_api_run_rejected", run_id=normalized_run_id, actor=actor)
        return self.get_run(normalized_run_id)

    def set_approval_policy(self, run_id: str, policy: ApprovalPolicy) -> dict[str, Any]:
        with self._lock:
            self._approval_decisions[run_id] = ApprovalDecision(
                run_id=run_id,
                status="pending",
                policy=policy,
                votes=[],
                created_at=time.time(),
                resolved_at=None,
            )
        return {"status": "policy_set", "run_id": run_id}

    def cast_vote(
        self,
        run_id: str,
        reviewer_id: str,
        role: str | None,
        action: str,
        comment: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            decision = self._approval_decisions.get(run_id)
            if decision is None:
                raise KeyError(run_id)
            vote = ApprovalVote(
                reviewer_id=reviewer_id,
                role=role,
                action=action,  # type: ignore[arg-type]
                timestamp=time.time(),
                comment=comment,
            )
            decision.votes.append(vote)
            elapsed = time.time() - decision.created_at
            new_status = ApprovalEngine().evaluate(decision.policy, decision.votes, elapsed)
            decision.status = new_status
            if new_status != "pending":
                decision.resolved_at = time.time()
            votes_cast = len(decision.votes)

        if new_status == "approved":
            record = self._runs.get(run_id)
            if record is not None and record.pending_approval:
                with contextlib.suppress(ValueError, RuntimeError):
                    self.approve_run(
                        run_id,
                        {"actor": reviewer_id, "rationale": comment},
                    )
        elif new_status in {"rejected", "timed_out"}:
            record = self._runs.get(run_id)
            if record is not None and record.pending_approval:
                with contextlib.suppress(ValueError, RuntimeError):
                    self.reject_run(
                        run_id,
                        {"actor": reviewer_id, "rationale": comment},
                    )

        return {"status": new_status, "votes_cast": votes_cast}

    def _capture_process_output(self, run_id: str) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            process = record.process

        stdout = process.stdout
        try:
            if stdout is not None:
                for raw_line in stdout:
                    self._append_log(run_id, raw_line.rstrip("\r\n"))
        finally:
            if stdout is not None:
                stdout.close()
            exit_code = process.wait()
            self._mark_finished(run_id, exit_code)
            with self._lock:
                record = self._runs.get(run_id)
                request_id = record.request_id if record is not None else ""
            self._log.info(
                "remote_api_run_finished",
                run_id=run_id,
                request_id=request_id,
                exit_code=exit_code,
            )

    def _append_log(self, run_id: str, line: str) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record.logs.append(line)

    def _mark_finished(self, run_id: str, exit_code: int) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.finished_at is not None:
                return
            record.exit_code = exit_code
            record.finished_at = _utc_now()
            if record.cancel_requested:
                record.status = "cancelled"
            else:
                record.status = "succeeded" if exit_code == 0 else "failed"
            _final_status = record.status
            _start_time = self._run_start_times.pop(run_id, None)
            trace_path = record.trace_path
            approval_history = list(record.approval_history)

        _LULA_ACTIVE_RUNS.dec()
        _lane = "default"
        _LULA_RUNS_TOTAL.labels(lane=_lane, status=_final_status).inc()
        if _start_time is not None:
            _LULA_RUN_DURATION_SECONDS.labels(lane=_lane).observe(time.monotonic() - _start_time)

        trace_raw = self._load_trace(trace_path)
        approval_state = _approval_state_from_trace(trace_raw)

        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            _apply_approval_state_to_record(record, approval_state)
            payload = self._summary_payload_locked(record)
            approval_history = list(record.approval_history)
            pending_details = dict(record.pending_approval_details)
        if self._run_store is not None:
            self._run_store.upsert(payload)
            try:
                if trace_raw is not None:
                    state_dict = trace_raw.get("state", trace_raw)
                    facts_raw = state_dict.get("facts", []) if isinstance(state_dict, dict) else []
                    facts = facts_raw if isinstance(facts_raw, list) else []
                    if facts:
                        self._run_store.upsert_recovery_facts(run_id, facts)
                    self._run_store.upsert_semantic_memories(
                        run_id,
                        _semantic_memories_from_trace(trace_raw, request=record.request),
                    )
            except Exception:
                pass

        if payload.get("pending_approval"):
            _write_trace_approval_state(
                trace_path,
                pending=True,
                pending_details=pending_details,
                history=approval_history,
                last_decision=approval_history[-1] if approval_history else None,
            )
        if self._run_store is not None and trace_raw is not None:
            with contextlib.suppress(Exception):
                self._run_store.upsert_semantic_memories(
                    run_id,
                    _semantic_memories_from_trace(trace_raw, request=record.request),
                )

        # Procedural memory: cache verified tool sequences on success
        if exit_code == 0 and self._procedure_cache is not None:
            try:
                trace = self._load_trace(trace_path)
                if trace is not None:
                    state_raw = trace.get("state", trace)
                    plan_raw = state_raw.get("plan", {})
                    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
                    steps = plan.get("steps", [])
                    verification = plan.get("verification", [])
                    request = str(state_raw.get("request", record.request)).strip()
                    task_class = str(state_raw.get("route", {}).get("task_class", "")).strip() or "analysis"
                    if isinstance(steps, list) and steps:
                        canonical_name = _canonical_procedure_name(steps)
                        self._procedure_cache.store_procedure(
                            canonical_name=canonical_name,
                            request=request,
                            task_class=task_class,
                            steps=steps,
                            verification=verification if isinstance(verification, list) else [],
                            created_at=record.finished_at or _utc_now(),
                        )
            except Exception:
                pass

    def _refresh_record_locked(self, record: RunRecord) -> None:
        if record.finished_at is not None:
            return
        exit_code = record.process.poll()
        if exit_code is None:
            return
        record.exit_code = exit_code
        record.finished_at = _utc_now()
        if record.cancel_requested:
            record.status = "cancelled"
        else:
            record.status = "succeeded" if exit_code == 0 else "failed"
        trace_raw = self._load_trace(record.trace_path)
        _apply_approval_state_to_record(record, _approval_state_from_trace(trace_raw))
        if self._run_store is not None:
            self._run_store.upsert(self._summary_payload_locked(record))

    def _summary_payload_locked(self, record: RunRecord) -> dict[str, Any]:
        self._refresh_record_locked(record)
        return {
            "run_id": record.run_id,
            "request": record.request,
            "status": record.status,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "exit_code": record.exit_code,
            "trace_out_dir": str(record.trace_out_dir),
            "trace_path": str(record.trace_path),
            "log_lines": len(record.logs),
            "request_id": record.request_id,
            "auth_subject": record.auth_subject,
            "client_ip": record.client_ip,
            "thread_id": record.thread_id,
            "checkpoint_id": record.checkpoint_id,
            "pending_approval": record.pending_approval,
            "pending_approval_summary": record.pending_approval_summary,
            "approval_history": list(record.approval_history),
            "cancel_requested": record.cancel_requested,
            "cancellable": record.finished_at is None and not record.cancel_requested,
        }

    def stream_run_sse(self, run_id: str, wfile: Any) -> None:
        """Write Server-Sent Events for a run to wfile until the run finishes.

        Each SSE event carries the full current run payload as JSON.
        Polls at 600 ms. Terminates when finished_at is set or the run is
        not found. The caller is responsible for writing the HTTP headers.
        """
        POLL_INTERVAL = 0.6
        MAX_EVENTS = 600  # 6 minutes ceiling
        seen_log_lines = 0
        for _ in range(MAX_EVENTS):
            with self._lock:
                record = self._runs.get(run_id)
                if record is None:
                    payload = None
                else:
                    self._refresh_record_locked(record)
                    summary = self._summary_payload_locked(record)
                    trace = self._load_trace(record.trace_path)
                    new_logs = record.logs[seen_log_lines:]
                    seen_log_lines = len(record.logs)
                    payload = _apply_trace_state_to_payload({
                        **summary,
                        "log_lines": len(record.logs),
                        "new_log_lines": new_logs,
                    }, trace)
            if payload is None:
                data = json.dumps({"error": "not_found", "run_id": run_id})
                try:
                    wfile.write(f"data: {data}\n\n".encode())
                    wfile.flush()
                except OSError:
                    return
                return
            data = json.dumps(payload, ensure_ascii=False)
            try:
                wfile.write(f"data: {data}\n\n".encode())
                wfile.flush()
            except OSError:
                return
            if payload.get("finished_at") is not None:
                # Send a final `done` event so the client can close cleanly
                try:
                    wfile.write(b"event: done\ndata: {}\n\n")
                    wfile.flush()
                except OSError:
                    pass
                return
            time.sleep(POLL_INTERVAL)

    def _load_trace(self, trace_path: Path) -> dict[str, Any] | None:
        if not trace_path.is_file():
            return None
        try:
            payload_raw = json.loads(trace_path.read_text(encoding="utf-8"))
        except OSError as exc:
            self._log.warning("remote_api_trace_read_failed", path=str(trace_path), error=str(exc))
            return None
        except json.JSONDecodeError as exc:
            self._log.warning("remote_api_trace_parse_failed", path=str(trace_path), error=str(exc))
            return None
        if not isinstance(payload_raw, dict):
            self._log.warning("remote_api_trace_invalid", path=str(trace_path), expected="object")
            return None
        return payload_raw

    def start_healing_loop(
        self,
        repo_path: str,
        poll_interval_seconds: float = 60.0,
    ) -> dict[str, Any]:
        from lg_orch.healing_loop import HealingLoop

        loop_id = uuid.uuid4().hex
        healing = HealingLoop(
            repo_path=repo_path,
            poll_interval_seconds=poll_interval_seconds,
        )

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                task = loop.create_task(healing.run_until_cancelled())
                with self._lock:
                    self._healing_tasks[loop_id] = task
                loop.run_forever()
            finally:
                loop.close()

        with self._lock:
            self._healing_loops[loop_id] = healing

        thread = threading.Thread(target=_run_loop, name=f"lg-healing-{loop_id}", daemon=True)
        thread.start()

        self._log.info("healing_loop_started", loop_id=loop_id, repo_path=repo_path)
        return {"loop_id": loop_id, "status": "started"}

    def stop_healing_loop(self, loop_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._healing_tasks.get(loop_id)
            healing = self._healing_loops.get(loop_id)

        if healing is None:
            return None

        if task is not None:
            task.cancel()

        with self._lock:
            self._healing_tasks.pop(loop_id, None)
            self._healing_loops.pop(loop_id, None)

        self._log.info("healing_loop_stopped", loop_id=loop_id)
        return {"loop_id": loop_id, "status": "stopped"}

    def get_healing_jobs(self, loop_id: str) -> dict[str, Any] | None:
        with self._lock:
            healing = self._healing_loops.get(loop_id)

        if healing is None:
            return None

        from lg_orch.healing_loop import HealingJob

        def _job_dict(job: HealingJob) -> dict[str, Any]:
            return {
                "job_id": job.job_id,
                "repo_path": job.repo_path,
                "failing_tests": list(job.failing_tests),
                "priority": job.priority,
                "created_at": job.created_at,
                "status": job.status,
            }

        jobs = [_job_dict(j) for j in healing.get_job_history()]
        return {"jobs": jobs}


# ---------------------------------------------------------------------------
# Module-level audit logger (set by serve_remote_api; None when not running in server mode)
# ---------------------------------------------------------------------------
_audit_logger: AuditLogger | None = None


def _api_http_dispatch(
    service: RemoteAPIService,
    *,
    method: str,
    request_path: str,
    request_body: bytes | None,
    request_id: str = "",
    client_ip: str = "",
    auth_mode: str = "off",
    expected_bearer_token: str | None = None,
    authorization_header: str | None = None,
    allow_unauthenticated_healthz: bool = True,
    jwt_settings: JWTSettings | None = None,
) -> tuple[int, str, bytes]:
    route = urlsplit(request_path).path.rstrip("/") or "/"
    auth_subject, auth_error = _authorize_request(
        route=route,
        auth_mode=auth_mode,
        expected_bearer_token=expected_bearer_token,
        authorization_header=authorization_header,
        allow_unauthenticated_healthz=allow_unauthenticated_healthz,
    )
    if auth_error is not None:
        _path_parts_early = [p for p in route.split("/") if p]
        _action_early, _rid_early = _audit_action_and_resource(
            method=method,
            route=route,
            path_parts=_path_parts_early,
            status=auth_error[0],
        )
        if _audit_logger is not None:
            _audit_logger.log(
                AuditEvent(
                    ts=utc_now_iso(),
                    subject="anonymous",
                    roles=[],
                    action=_action_early,
                    resource_id=_rid_early,
                    outcome="denied",
                    detail="bearer_auth_failed",
                )
            )
        return auth_error

    if service._rate_limiter is not None and not service._rate_limiter.acquire():
        return _json_response(429, {"error": "rate_limit_exceeded"})

    # JWT/RBAC enforcement — runs after the existing static-bearer check.
    _jwt = jwt_settings or JWTSettings(jwt_secret=None, jwks_url=None)
    _early_path_parts = [part for part in route.split("/") if part]
    _required_roles = _route_policy(
        route=route,
        method=method,
        path_parts=_early_path_parts,
        jwt_enabled=_jwt.enabled,
    )
    # Only enforce when the route has required roles; open routes (_OPEN == ())
    # are never gated regardless of whether JWT is configured.
    _jwt_claims_roles: list[str] = []
    if _required_roles:
        try:
            _claims = authorize_stdlib(
                authorization=authorization_header,
                settings=_jwt,
                required_roles=_required_roles,
            )
            if _claims.sub and _claims.sub != "anonymous":
                auth_subject = _claims.sub
            _jwt_claims_roles = list(_claims.roles)
        except AuthError as _auth_exc:
            _path_parts_jwt = [p for p in route.split("/") if p]
            _action_jwt, _rid_jwt = _audit_action_and_resource(
                method=method,
                route=route,
                path_parts=_path_parts_jwt,
                status=_auth_exc.status_code,
            )
            if _audit_logger is not None:
                _audit_logger.log(
                    AuditEvent(
                        ts=utc_now_iso(),
                        subject="anonymous",
                        roles=[],
                        action=_action_jwt,
                        resource_id=_rid_jwt,
                        outcome="denied",
                        detail=_auth_exc.detail,
                    )
                )
            return _json_response(_auth_exc.status_code, {"error": _auth_exc.detail})

    if route == "/metrics":
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        body = prometheus_client.generate_latest()
        return 200, _PROMETHEUS_CONTENT_TYPE, body

    if route in {"/", "/ui"}:
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        from lg_orch.graph import export_mermaid
        from lg_orch.visualize import render_run_viewer_spa
        html = render_run_viewer_spa(api_base_url="", mermaid_graph=export_mermaid())
        body = html.encode("utf-8")
        return 200, "text/html; charset=utf-8", body

    if route == "/healthz":
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        return _json_response(200, {"ok": True})

    if route == "/v1/runs":
        if method == "GET":
            return _json_response(200, {"runs": service.list_runs()})
        if method != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        try:
            return _json_response(
                201,
                service.create_run(
                    payload_raw,
                    request_id=request_id,
                    auth_subject=auth_subject,
                    client_ip=client_ip,
                ),
            )
        except ValueError as exc:
            error = str(exc)
            status = 409 if error == "duplicate_run_id" else 400
            return _json_response(status, {"error": error})
        except OSError as exc:
            return _json_response(500, {"error": "launch_failed", "detail": str(exc)})

    path_parts = [part for part in route.split("/") if part]
    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "logs":
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        run_id = path_parts[2]
        payload = service.get_logs(run_id)
        if payload is None:
            return _json_response(404, {"error": "not_found", "run_id": run_id})
        return _json_response(200, payload)

    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "cancel":
        if method != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        run_id = path_parts[2]
        payload = service.cancel_run(run_id)
        if payload is None:
            return _json_response(404, {"error": "not_found", "run_id": run_id})
        return _json_response(202, payload)

    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] in {"approve", "reject"}:
        if method != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        run_id = path_parts[2]
        try:
            if path_parts[3] == "approve":
                payload = service.approve_run(run_id, payload_raw, auth_subject=auth_subject)
            else:
                payload = service.reject_run(run_id, payload_raw, auth_subject=auth_subject)
        except ValueError as exc:
            return _json_response(409, {"error": str(exc), "run_id": run_id})
        if payload is None:
            return _json_response(404, {"error": "not_found", "run_id": run_id})
        return _json_response(202, payload)

    if len(path_parts) == 4 and path_parts[:2] == ["v1", "runs"] and path_parts[3] == "stream":
        # SSE streaming — handled in the HTTP layer, not here.
        # Return sentinel (-1) so the HTTP handler can call stream_run_sse().
        return -1, "sse", path_parts[2].encode("utf-8")

    if len(path_parts) == 3 and path_parts[:2] == ["v1", "runs"]:
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        run_id = path_parts[2]
        payload = service.get_run(run_id)
        if payload is None:
            return _json_response(404, {"error": "not_found", "run_id": run_id})
        return _json_response(200, payload)

    # ── SPA convenience aliases (no /v1/ prefix) ────────────────────────────

    # GET /runs/search — full-text search over run history
    if route == "/runs/search":
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        qs = parse_qs(urlsplit(request_path).query, keep_blank_values=False)
        q_values = qs.get("q", [])
        if not q_values or not q_values[0].strip():
            return _json_response(422, {"error": "missing_required_param", "param": "q"})
        q = q_values[0].strip()
        limit_raw = qs.get("limit", ["50"])[0]
        try:
            limit = max(1, min(200, int(limit_raw)))
        except ValueError:
            limit = 50
        results = service.search_runs(q, limit=limit)
        return _json_response(200, {"results": results, "total": len(results)})

    # GET /runs — run history list used by the standalone SPA
    if route in {"/runs", "/runs/"}:
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        return _json_response(200, {"runs": service.list_runs()})

    # GET /runs/{run_id}/stream — new-format SSE for the standalone SPA
    if (
        method == "GET"
        and len(path_parts) == 3
        and path_parts[0] == "runs"
        and path_parts[2] == "stream"
    ):
        # Check run exists BEFORE opening the stream; return 404 per spec.
        _sse_run_id = path_parts[1]
        if service.get_run(_sse_run_id) is None:
            return _json_response(404, {"error": "not_found", "run_id": _sse_run_id})
        return -2, "sse_new", _sse_run_id.encode("utf-8")

    # POST /runs/{run_id}/approve and /runs/{run_id}/reject — SPA approval shortcuts
    if (
        method == "POST"
        and len(path_parts) == 3
        and path_parts[0] == "runs"
        and path_parts[2] in {"approve", "reject"}
    ):
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        run_id = path_parts[1]
        try:
            if path_parts[2] == "approve":
                payload = service.approve_run(run_id, payload_raw, auth_subject=auth_subject)
            else:
                payload = service.reject_run(run_id, payload_raw, auth_subject=auth_subject)
        except ValueError as exc:
            return _json_response(409, {"error": str(exc), "run_id": run_id})
        if payload is None:
            return _json_response(404, {"error": "not_found", "run_id": run_id})
        return _json_response(202, payload)

    # POST /runs/{run_id}/approval-policy — set a multi-path approval policy
    if (
        method == "POST"
        and len(path_parts) == 3
        and path_parts[0] == "runs"
        and path_parts[2] == "approval-policy"
    ):
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        policy_raw = payload_raw.get("policy")
        if not isinstance(policy_raw, dict):
            return _json_response(400, {"error": "missing_policy"})
        kind = policy_raw.get("kind")
        if kind == "timed":
            policy: ApprovalPolicy = TimedApprovalPolicy(
                timeout_seconds=float(policy_raw.get("timeout_seconds", 300.0)),
                auto_action=cast("Literal['approve', 'reject']", policy_raw.get("auto_action", "reject")),
            )
        elif kind == "quorum":
            policy = QuorumApprovalPolicy(
                required_approvals=int(policy_raw.get("required_approvals", 1)),
                required_rejections=int(policy_raw.get("required_rejections", 1)),
                allowed_reviewers=list(policy_raw.get("allowed_reviewers", [])),
            )
        elif kind == "role":
            policy = RoleApprovalPolicy(
                required_roles=list(policy_raw.get("required_roles", [])),
                require_all_roles=bool(policy_raw.get("require_all_roles", False)),
            )
        else:
            return _json_response(400, {"error": "unknown_policy_kind"})
        run_id = path_parts[1]
        result = service.set_approval_policy(run_id, policy)
        return _json_response(200, result)

    # POST /runs/{run_id}/vote — cast a vote on a run's approval policy
    if (
        method == "POST"
        and len(path_parts) == 3
        and path_parts[0] == "runs"
        and path_parts[2] == "vote"
    ):
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        run_id = path_parts[1]
        reviewer_id = _non_empty_str(payload_raw.get("reviewer_id"))
        if reviewer_id is None:
            return _json_response(400, {"error": "missing_reviewer_id"})
        action = _non_empty_str(payload_raw.get("action"))
        if action not in {"approve", "reject"}:
            return _json_response(400, {"error": "invalid_action"})
        role_raw = payload_raw.get("role")
        role = _non_empty_str(role_raw) if role_raw is not None else None
        comment = str(payload_raw.get("comment", ""))
        try:
            result = service.cast_vote(
                run_id,
                reviewer_id=reviewer_id,
                role=role,
                action=action,
                comment=comment,
            )
        except KeyError:
            return _json_response(404, {"error": "policy_not_found", "run_id": run_id})
        return _json_response(200, result)

    # GET /app or /app/<subpath> — standalone SPA static files
    if path_parts and path_parts[0] == "app":
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        spa_dir = Path(__file__).parent / "spa"
        if not spa_dir.exists():
            return _json_response(503, {"error": "spa_not_available"})
        from lg_orch.spa.router import create_spa_router
        subpath = "/".join(path_parts[1:]) if len(path_parts) > 1 else ""
        return create_spa_router(spa_dir)(subpath)

    # POST /healing/start
    if route == "/healing/start":
        if method != "POST":
            return _json_response(405, {"error": "method_not_allowed"})
        try:
            payload_raw = json.loads((request_body or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload_raw, dict):
            return _json_response(400, {"error": "invalid_json"})
        repo_path_raw = payload_raw.get("repo_path")
        if not isinstance(repo_path_raw, str) or not repo_path_raw.strip():
            return _json_response(400, {"error": "missing_repo_path"})
        poll_interval_raw = payload_raw.get("poll_interval_seconds", 60.0)
        try:
            poll_interval = float(poll_interval_raw)
        except (TypeError, ValueError):
            poll_interval = 60.0
        result = service.start_healing_loop(repo_path_raw.strip(), poll_interval_seconds=poll_interval)
        return _json_response(201, result)

    # POST /healing/{loop_id}/stop
    if (
        method == "POST"
        and len(path_parts) == 3
        and path_parts[0] == "healing"
        and path_parts[2] == "stop"
    ):
        loop_id = path_parts[1]
        result_stop = service.stop_healing_loop(loop_id)
        if result_stop is None:
            return _json_response(404, {"error": "not_found", "loop_id": loop_id})
        return _json_response(200, result_stop)

    # GET /healing/{loop_id}/jobs
    if (
        method == "GET"
        and len(path_parts) == 3
        and path_parts[0] == "healing"
        and path_parts[2] == "jobs"
    ):
        loop_id = path_parts[1]
        jobs_payload = service.get_healing_jobs(loop_id)
        if jobs_payload is None:
            return _json_response(404, {"error": "not_found", "loop_id": loop_id})
        return _json_response(200, jobs_payload)

    return _json_response(404, {"error": "not_found"})


def _api_http_response(
    service: RemoteAPIService,
    *,
    method: str,
    request_path: str,
    request_body: bytes | None,
    request_id: str = "",
    client_ip: str = "",
    auth_mode: str = "off",
    expected_bearer_token: str | None = None,
    authorization_header: str | None = None,
    allow_unauthenticated_healthz: bool = True,
    jwt_settings: JWTSettings | None = None,
) -> tuple[int, str, bytes]:
    """Public entry point: wraps :func:`_api_http_dispatch` with audit emission."""
    status, content_type, body = _api_http_dispatch(
        service,
        method=method,
        request_path=request_path,
        request_body=request_body,
        request_id=request_id,
        client_ip=client_ip,
        auth_mode=auth_mode,
        expected_bearer_token=expected_bearer_token,
        authorization_header=authorization_header,
        allow_unauthenticated_healthz=allow_unauthenticated_healthz,
        jwt_settings=jwt_settings,
    )

    # Auth denials (401/403) are emitted inside _api_http_dispatch; skip here.
    if _audit_logger is not None and status not in {401, 403}:
        _route = urlsplit(request_path).path.rstrip("/") or "/"
        _pp = [p for p in _route.split("/") if p]
        _action, _rid = _audit_action_and_resource(
            method=method,
            route=_route,
            path_parts=_pp,
            status=status,
        )
        _outcome: Literal["ok", "denied", "error"] = "error" if status >= 500 else "ok"
        _audit_logger.log(
            AuditEvent(
                ts=utc_now_iso(),
                subject=auth_mode if auth_mode != "off" else "anonymous",
                roles=[],
                action=_action,
                resource_id=_rid,
                outcome=_outcome,
                detail=None,
            )
        )

    return status, content_type, body


def serve_remote_api(*, repo_root: Path, host: str, port: int) -> int:
    init_telemetry(
        service_name="lula-orchestrator",
        otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    log = get_logger()
    try:
        cfg = load_config(repo_root=repo_root)
    except Exception as exc:
        log.error("remote_api_config_load_failed", error=str(exc), repo_root=str(repo_root))
        return 2

    remote_api_cfg = cfg.remote_api
    _namespace = remote_api_cfg.default_namespace
    run_store: RunStore | None = None
    if remote_api_cfg.run_store_path:
        run_store = RunStore(db_path=Path(remote_api_cfg.run_store_path), namespace=_namespace)
    rate_limiter: _RateLimiter | None = None
    if remote_api_cfg.rate_limit_rps > 0:
        rps = remote_api_cfg.rate_limit_rps
        rate_limiter = _RateLimiter(capacity=max(rps * 2, 10), rate=float(rps))
    procedure_cache: ProcedureCache | None = None
    if remote_api_cfg.procedure_cache_path:
        procedure_cache = ProcedureCache(db_path=Path(remote_api_cfg.procedure_cache_path))
    service = RemoteAPIService(
        repo_root=repo_root,
        run_store=run_store,
        rate_limiter=rate_limiter,
        procedure_cache=procedure_cache,
        namespace=_namespace,
    )

    _jwt_settings = jwt_settings_from_config(
        jwt_secret=remote_api_cfg.jwt_secret,
        jwks_url=remote_api_cfg.jwks_url,
    )

    class RemoteAPIRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle_request(method="GET")

        def do_POST(self) -> None:
            self._handle_request(method="POST")

        def _handle_request(self, *, method: str) -> None:
            request_id = _request_id_from_value(self.headers.get(_REQUEST_ID_HEADER))
            route = urlsplit(self.path).path.rstrip("/") or "/"
            client_ip = _request_client_ip(
                client_address=self.client_address,
                forwarded_for=_non_empty_str(self.headers.get("X-Forwarded-For")),
                trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers,
            )
            scheme = _request_scheme(
                forwarded_proto=_non_empty_str(self.headers.get("X-Forwarded-Proto")),
                trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers,
            )
            started_at = time.perf_counter()
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            try:
                request_body = self.rfile.read(content_length) if content_length > 0 else None
                status, content_type, body = _api_http_response(
                    service,
                    method=method,
                    request_path=self.path,
                    request_body=request_body,
                    request_id=request_id,
                    client_ip=client_ip,
                    auth_mode=remote_api_cfg.auth_mode,
                    expected_bearer_token=remote_api_cfg.bearer_token,
                    authorization_header=self.headers.get("Authorization"),
                    allow_unauthenticated_healthz=remote_api_cfg.allow_unauthenticated_healthz,
                    jwt_settings=_jwt_settings,
                )
            except Exception as exc:
                log.error(
                    "remote_api_request_failed",
                    request_id=request_id,
                    method=method,
                    route=route,
                    client_ip=client_ip,
                    error=str(exc),
                )
                status, content_type, body = _json_response(500, {"error": "internal_server_error"})

            # SSE sentinel: stream run events rather than a normal HTTP response.
            if status == -1 and content_type == "sse":
                sse_run_id = body.decode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header(_REQUEST_ID_HEADER, request_id)
                self.end_headers()
                service.stream_run_sse(sse_run_id, self.wfile)
                return

            # New-format SSE sentinel for the standalone SPA (/runs/{id}/stream).
            if status == -2 and content_type == "sse_new":
                sse_run_id = body.decode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(_REQUEST_ID_HEADER, request_id)
                self.end_headers()
                _stream_new_sse(service, sse_run_id, self.wfile)
                return

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(_REQUEST_ID_HEADER, request_id)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            if remote_api_cfg.access_log_enabled:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                log.info(
                    "remote_api_access",
                    request_id=request_id,
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    scheme=scheme,
                    authenticated=bool(status < 400 and remote_api_cfg.auth_mode != "off"),
                )

        def log_message(self, format: str, *args: object) -> None:
            return

    global _audit_logger
    audit_cfg = cfg.audit
    _audit_sink = build_sink(audit_cfg)
    _audit_logger = AuditLogger(
        log_path=Path(audit_cfg.log_path),
        sink=_audit_sink,
    )

    try:
        with ThreadingHTTPServer((host, port), RemoteAPIRequestHandler) as server:
            log.info(
                "remote_api_listening",
                host=host,
                port=port,
                repo_root=str(repo_root),
                auth_mode=remote_api_cfg.auth_mode,
                trust_forwarded_headers=remote_api_cfg.trust_forwarded_headers,
            )
            print(f"Remote API listening on http://{host}:{port}")
            server.serve_forever()
    except OSError as exc:
        log.error("remote_api_bind_failed", host=host, port=port, error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        if _audit_logger is not None:
            _audit_logger.close()
            _audit_logger = None
    return 0
