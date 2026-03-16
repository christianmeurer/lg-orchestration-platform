from __future__ import annotations

import hmac
import json
import os
import re
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
from typing import Any
from urllib.parse import urlsplit

from lg_orch.config import load_config
from lg_orch.logging import get_logger
from lg_orch.procedure_cache import ProcedureCache, _canonical_procedure_name
from lg_orch.run_store import RunStore

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_TRACE_OUT_DIR = Path("artifacts/remote-api")
_ALLOWED_VIEWS = {"classic", "console"}
_REQUEST_ID_HEADER = "X-Request-ID"


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


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status, _JSON_CONTENT_TYPE, body


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
        self._log = get_logger()
        self._run_store = run_store
        self._rate_limiter = rate_limiter
        self._procedure_cache = procedure_cache
        self._namespace = namespace.strip()

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
            )

        if self._run_store is not None:
            with self._lock:
                _record = self._runs.get(run_id)
                if _record is not None:
                    self._run_store.upsert(self._summary_payload_locked(_record))
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
        payload["trace_ready"] = trace_payload is not None
        payload["trace"] = trace_payload
        return payload

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
            payload = self._summary_payload_locked(record)
            trace_path = record.trace_path
        if self._run_store is not None:
            self._run_store.upsert(payload)
            try:
                trace_raw = self._load_trace(trace_path)
                if trace_raw is not None:
                    state_dict = trace_raw.get("state", trace_raw)
                    facts_raw = state_dict.get("facts", []) if isinstance(state_dict, dict) else []
                    facts = facts_raw if isinstance(facts_raw, list) else []
                    if facts:
                        self._run_store.upsert_recovery_facts(run_id, facts)
            except Exception:
                pass

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
                    payload = {
                        **summary,
                        "log_lines": len(record.logs),
                        "new_log_lines": new_logs,
                        "trace_ready": trace is not None,
                        "trace": trace,
                    }
            if payload is None:
                data = json.dumps({"error": "not_found", "run_id": run_id})
                try:
                    wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    wfile.flush()
                except OSError:
                    return
                return
            data = json.dumps(payload, ensure_ascii=False)
            try:
                wfile.write(f"data: {data}\n\n".encode("utf-8"))
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
        return auth_error

    if service._rate_limiter is not None and not service._rate_limiter.acquire():
        return _json_response(429, {"error": "rate_limit_exceeded"})

    if route in {"/", "/ui"}:
        if method != "GET":
            return _json_response(405, {"error": "method_not_allowed"})
        from lg_orch.visualize import render_run_viewer_spa
        from lg_orch.graph import export_mermaid
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

    return _json_response(404, {"error": "not_found"})


def serve_remote_api(*, repo_root: Path, host: str, port: int) -> int:
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
    return 0
