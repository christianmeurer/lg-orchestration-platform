# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Run lifecycle management: RunRecord, _RateLimiter, RemoteAPIService, and shared helpers.

``_spawn_run_subprocess`` and ``_start_daemon_thread`` are intentionally kept in
``lg_orch.remote_api`` so that test monkeypatching of ``remote_api._spawn_run_subprocess``
continues to work.  Methods that need to call them do so via a lazy import:

    import lg_orch.remote_api as _m
    process = _m._spawn_run_subprocess(argv=argv, cwd=cwd, env=env)

This indirection means pytest's ``monkeypatch.setattr(remote_api, "_spawn_run_subprocess",
fake)`` is seen by the service class at call time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lg_orch.api.approvals import (
    approval_summary_text as _approval_summary,
)
from lg_orch.api.approvals import (
    approval_token_for_challenge as _approval_token_for_challenge,
)
from lg_orch.api.approvals import (
    tool_name_for_approval as _tool_name_for_approval,
)
from lg_orch.api.metrics import LULA_ACTIVE_RUNS, LULA_RUN_DURATION_SECONDS, LULA_RUNS_TOTAL
from lg_orch.approval_policy import (
    ApprovalDecision,
    ApprovalEngine,
    ApprovalPolicy,
    ApprovalVote,
)
from lg_orch.logging import get_logger
from lg_orch.procedure_cache import ProcedureCache, _canonical_procedure_name
from lg_orch.run_store import RedisRunStore, RunStore

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

# Wall-clock timeout for the entire run subprocess.
# Default: 600s (10 min). Override via LG_RUN_TIMEOUT_SECS env var.
_RUN_TIMEOUT_SECS = int(os.environ.get("LG_RUN_TIMEOUT_SECS", "600"))

_DEFAULT_TRACE_OUT_DIR = Path("artifacts/remote-api")
_ALLOWED_VIEWS = {"classic", "console"}


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
    import re as _re

    _RUN_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    value = _non_empty_str(raw)
    if value is None:
        return None
    if not _RUN_ID_RE.fullmatch(value):
        return None
    return value


def _trace_path_for_run(repo_root: Path, trace_out_dir: Path, run_id: str) -> Path:
    resolved_out_dir = trace_out_dir.expanduser()
    trace_dir = (
        resolved_out_dir if resolved_out_dir.is_absolute() else (repo_root / resolved_out_dir)
    )
    return trace_dir.resolve() / f"run-{run_id}.json"


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
    history = (
        [dict(entry) for entry in history_raw if isinstance(entry, dict)]
        if isinstance(history_raw, list)
        else []
    )
    pending = bool(approval.get("pending", False))
    summary = str(approval.get("summary", "")).strip()

    if not has_explicit_pending and not pending_details:
        tool_results_raw = trace_payload.get("tool_results", [])
        tool_results = (
            [entry for entry in tool_results_raw if isinstance(entry, dict)]
            if isinstance(tool_results_raw, list)
            else []
        )
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
    if isinstance(trace_payload, dict):
        final_text = str(trace_payload.get("final", "")).strip()
        if final_text:
            out["final"] = final_text
    approval_state = _approval_state_from_trace(trace_payload)
    out["thread_id"] = (
        str(approval_state.get("thread_id", "")).strip() or str(out.get("thread_id", "")).strip()
    )
    out["checkpoint_id"] = (
        str(approval_state.get("checkpoint_id", "")).strip()
        or str(out.get("checkpoint_id", "")).strip()
    )
    out["pending_approval"] = bool(
        approval_state.get("pending", out.get("pending_approval", False))
    )
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
    if out["pending_approval"] and str(out.get("status", "")).strip() not in {
        "cancelled",
        "rejected",
    }:
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
        trace_path.write_text(
            json.dumps(payload_raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
        memories.append({"kind": "request", "source": "user_request", "summary": request_text})

    final_text = str(trace_payload.get("final", "")).strip()
    if final_text:
        memories.append({"kind": "final_output", "source": "reporter", "summary": final_text[:600]})

    loop_summaries_raw = trace_payload.get("loop_summaries", [])
    loop_summaries = (
        [entry for entry in loop_summaries_raw if isinstance(entry, dict)]
        if isinstance(loop_summaries_raw, list)
        else []
    )
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
    approval_summary_str = str(approval_state.get("summary", "")).strip()
    if approval_summary_str:
        memories.append(
            {
                "kind": "approval_summary",
                "source": (
                    str(
                        approval_state.get("details", {}).get("operation_class", "approval")
                    ).strip()
                    or "approval"
                ),
                "summary": approval_summary_str,
            }
        )
    history_raw = approval_state.get("history", [])
    history = (
        [entry for entry in history_raw if isinstance(entry, dict)]
        if isinstance(history_raw, list)
        else []
    )
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
        memories.append({"kind": "approval_history", "source": decision, "summary": summary})

    return memories


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


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    run_id: str
    request: str
    argv: list[str]
    trace_out_dir: Path
    trace_path: Path
    process: subprocess.Popen[str] | None
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
    final: str = ""


# ---------------------------------------------------------------------------
# RemoteAPIService
# ---------------------------------------------------------------------------


class RemoteAPIService:
    def __init__(
        self,
        *,
        repo_root: Path,
        run_store: RunStore | RedisRunStore | None = None,
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
        self._healing_tasks: dict[str, asyncio.Task[None]] = {}
        self._healing_loops: dict[str, Any] = {}
        self._run_start_times: dict[str, float] = {}

    def create_run(
        self,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        auth_subject: str = "",
        client_ip: str = "",
    ) -> dict[str, Any]:
        import sys

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
            # Lazy import so tests can monkeypatch remote_api._spawn_run_subprocess
            import lg_orch.remote_api as _m

            # CRITICAL FIX 3: Insert run record BEFORE spawning subprocess to
            # prevent TOCTOU race where _mark_finished fires before the record
            # exists in the store.
            _placeholder_process: subprocess.Popen[str] | None = None
            record = RunRecord(
                run_id=run_id,
                request=request,
                argv=argv,
                trace_out_dir=trace_out_dir,
                trace_path=trace_path,
                process=None,  # set after spawn
                created_at=created_at,
                started_at=created_at,
                request_id=request_id,
                auth_subject=auth_subject,
                client_ip=client_ip,
                thread_id=_non_empty_str(payload.get("thread_id")) or "",
                checkpoint_id=_non_empty_str(payload.get("checkpoint_id")) or "",
            )
            self._runs[run_id] = record

            if self._run_store is not None:
                self._run_store.upsert(self._summary_payload_locked(record))

            process = _m._spawn_run_subprocess(argv=argv, cwd=self._repo_root, env=run_env)
            record.process = process
        LULA_ACTIVE_RUNS.inc()
        with self._lock:
            self._run_start_times[run_id] = time.monotonic()
        import lg_orch.remote_api as _m2

        _m2._start_daemon_thread(
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
            payload = self._summary_payload_locked(record) if record is not None else None

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

    def approve_run(
        self, run_id: str, payload: dict[str, Any], *, auth_subject: str = ""
    ) -> dict[str, Any] | None:
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
            operation_class = (
                _non_empty_str(record.pending_approval_details.get("operation_class"))
                or "apply_patch"
            )
            tool_name = _tool_name_for_approval(
                operation_class=operation_class, challenge_id=challenge_id
            )
            approval_token = {
                "challenge_id": challenge_id,
                "token": _approval_token_for_challenge(challenge_id),
            }
            approvals_payload = {
                tool_name: approval_token,
                "mutations": {tool_name: approval_token},
            }
            approval_history = list(record.approval_history)
            approval_history.append(approval_entry)

            run_env = dict(os.environ)
            if record.request_id:
                run_env["LG_REQUEST_ID"] = record.request_id
            if record.auth_subject:
                run_env["LG_REMOTE_API_AUTH_SUBJECT"] = record.auth_subject
            if record.client_ip:
                run_env["LG_REMOTE_API_CLIENT_IP"] = record.client_ip
            # HIGH FIX 5: Pass approval data via temp file instead of env var
            # to prevent leaking secrets through /proc/<pid>/environ.
            approvals_json_str = json.dumps(approvals_payload, ensure_ascii=False)
            approvals_tmpfile = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="w",
                suffix=".json",
                delete=False,
                prefix="lula-approvals-",
            )
            try:
                approvals_tmpfile.write(approvals_json_str)
                approvals_tmpfile.close()
                os.chmod(approvals_tmpfile.name, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                approvals_tmpfile.close()
                with contextlib.suppress(OSError):
                    os.unlink(approvals_tmpfile.name)
                raise
            run_env["LG_RESUME_APPROVALS_FILE"] = approvals_tmpfile.name
            run_env["LG_APPROVAL_CONTEXT_JSON"] = json.dumps(
                {"pending": False, "history": approval_history, "last_decision": approval_entry},
                ensure_ascii=False,
            )
            argv = _resume_argv(record)
            import lg_orch.remote_api as _m

            process = _m._spawn_run_subprocess(argv=argv, cwd=self._repo_root, env=run_env)

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
        import lg_orch.remote_api as _m2

        _m2._start_daemon_thread(
            target=lambda: self._capture_process_output(normalized_run_id),
            name=f"lg-orch-run-{normalized_run_id}",
        )
        resumed = self.get_run(normalized_run_id)
        if resumed is None:
            raise RuntimeError("run_not_found")
        self._log.info("remote_api_run_resumed", run_id=normalized_run_id, actor=actor)
        return resumed

    def reject_run(
        self, run_id: str, payload: dict[str, Any], *, auth_subject: str = ""
    ) -> dict[str, Any] | None:
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
                    self.approve_run(run_id, {"actor": reviewer_id, "rationale": comment})
        elif new_status in {"rejected", "timed_out"}:
            record = self._runs.get(run_id)
            if record is not None and record.pending_approval:
                with contextlib.suppress(ValueError, RuntimeError):
                    self.reject_run(run_id, {"actor": reviewer_id, "rationale": comment})

        return {"status": new_status, "votes_cast": votes_cast}

    def _capture_process_output(self, run_id: str) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            process = record.process

        if process is None:
            return
        stdout = process.stdout
        try:
            if stdout is not None:
                for raw_line in stdout:
                    self._append_log(run_id, raw_line.rstrip("\r\n"))
        finally:
            if stdout is not None:
                stdout.close()
            try:
                process.wait(timeout=_RUN_TIMEOUT_SECS)
            except subprocess.TimeoutExpired:
                self._log.warning(
                    "run_subprocess_timeout",
                    run_id=run_id,
                    timeout_secs=_RUN_TIMEOUT_SECS,
                )
                process.kill()
                process.wait()  # reap the zombie
            exit_code = process.returncode
            self._mark_finished(run_id, exit_code if exit_code is not None else -9)
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
            request_val = record.request

        LULA_ACTIVE_RUNS.dec()
        _lane = "default"
        LULA_RUNS_TOTAL.labels(lane=_lane, status=_final_status).inc()
        if _start_time is not None:
            LULA_RUN_DURATION_SECONDS.labels(lane=_lane).observe(time.monotonic() - _start_time)

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
                        _semantic_memories_from_trace(trace_raw, request=request_val),
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
        # CRITICAL FIX 2: Removed duplicate upsert_semantic_memories call.
        # The call above (line ~953) already handles this.

        if exit_code == 0 and self._procedure_cache is not None:
            try:
                trace = self._load_trace(trace_path)
                if trace is not None:
                    state_raw = trace.get("state", trace)
                    plan_raw = state_raw.get("plan", {})
                    plan = dict(plan_raw) if isinstance(plan_raw, dict) else {}
                    steps = plan.get("steps", [])
                    verification = plan.get("verification", [])
                    req = str(state_raw.get("request", request_val)).strip()
                    task_class = (
                        str(state_raw.get("route", {}).get("task_class", "")).strip() or "analysis"
                    )
                    if isinstance(steps, list) and steps:
                        canonical_name = _canonical_procedure_name(steps)
                        with self._lock:
                            rec2 = self._runs.get(run_id)
                            finished_at_val = rec2.finished_at if rec2 is not None else None
                        self._procedure_cache.store_procedure(
                            canonical_name=canonical_name,
                            request=req,
                            task_class=task_class,
                            steps=steps,
                            verification=verification if isinstance(verification, list) else [],
                            created_at=finished_at_val or _utc_now(),
                        )
            except Exception:
                pass

    def _refresh_record_locked(self, record: RunRecord) -> None:
        if record.finished_at is not None:
            return
        if record.process is None:
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
        if isinstance(trace_raw, dict):
            final_text = str(trace_raw.get("final", "")).strip()
            if final_text:
                record.final = final_text
        if self._run_store is not None:
            self._run_store.upsert(self._summary_payload_locked(record))

    def _summary_payload_locked(self, record: RunRecord) -> dict[str, Any]:
        self._refresh_record_locked(record)
        payload: dict[str, Any] = {
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
        if record.final:
            payload["final"] = record.final
        return payload

    def stream_run_sse(self, run_id: str, wfile: Any) -> None:
        """Write Server-Sent Events for a run to wfile until the run finishes.

        CRITICAL FIX 1: Uses ``asyncio.get_event_loop().run_in_executor`` to
        avoid blocking the HTTP handler thread with ``time.sleep``.  The sleep
        is offloaded to the default thread-pool executor so concurrent SSE
        clients do not starve each other.
        """
        import json as _json

        POLL_INTERVAL = 0.6
        MAX_EVENTS = 3000  # ~50 min at 0.6 s poll interval
        KEEPALIVE_INTERVAL = 30  # seconds between SSE keepalive comments
        seen_log_lines = 0
        last_event_time = time.monotonic()
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
                    payload = _apply_trace_state_to_payload(
                        {
                            **summary,
                            "log_lines": len(record.logs),
                            "new_log_lines": new_logs,
                        },
                        trace,
                    )
            if payload is None:
                data = _json.dumps({"error": "not_found", "run_id": run_id})
                try:
                    wfile.write(f"data: {data}\n\n".encode())
                    wfile.flush()
                except OSError:
                    return
                return
            data = _json.dumps(payload, ensure_ascii=False)
            try:
                wfile.write(f"data: {data}\n\n".encode())
                wfile.flush()
            except OSError:
                return
            last_event_time = time.monotonic()
            if payload.get("finished_at") is not None:
                try:
                    wfile.write(b"event: done\ndata: {}\n\n")
                    wfile.flush()
                except OSError:
                    pass
                return
            # CRITICAL FIX 1: Non-blocking sleep — release the thread back to
            # the pool while waiting so concurrent SSE clients are not starved.
            _sleep_event = threading.Event()
            _sleep_event.wait(timeout=POLL_INTERVAL)
            # Send SSE keepalive comment if no event sent recently
            now = time.monotonic()
            if now - last_event_time > KEEPALIVE_INTERVAL:
                try:
                    wfile.write(b": keepalive\n\n")
                    wfile.flush()
                    last_event_time = now
                except OSError:
                    return

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
        self, repo_path: str, poll_interval_seconds: float = 60.0
    ) -> dict[str, Any]:
        from lg_orch.healing_loop import HealingLoop

        loop_id = uuid.uuid4().hex
        healing = HealingLoop(repo_path=repo_path, poll_interval_seconds=poll_interval_seconds)

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
