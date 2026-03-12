from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lg_orch.logging import get_logger

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_TRACE_OUT_DIR = Path("artifacts/remote-api")
_ALLOWED_VIEWS = {"classic", "console"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _spawn_run_subprocess(*, argv: list[str], cwd: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
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


class RemoteAPIService:
    def __init__(self, *, repo_root: Path) -> None:
        self._repo_root = repo_root.resolve()
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}
        self._log = get_logger()

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        with self._lock:
            if run_id in self._runs:
                raise ValueError("duplicate_run_id")
            process = _spawn_run_subprocess(argv=argv, cwd=self._repo_root)
            self._runs[run_id] = RunRecord(
                run_id=run_id,
                request=request,
                argv=argv,
                trace_out_dir=trace_out_dir,
                trace_path=trace_path,
                process=process,
                created_at=created_at,
                started_at=created_at,
            )

        _start_daemon_thread(
            target=lambda: self._capture_process_output(run_id),
            name=f"lg-orch-run-{run_id}",
        )
        self._log.info(
            "remote_api_run_started",
            run_id=run_id,
            trace_path=str(trace_path),
            repo_root=str(self._repo_root),
        )
        detail = self.get_run(run_id)
        if detail is None:
            raise RuntimeError("run_not_found")
        return detail

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._runs.values())
            return [self._summary_payload_locked(record) for record in reversed(records)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = _normalized_run_id(run_id)
        if normalized_run_id is None:
            return None
        with self._lock:
            record = self._runs.get(normalized_run_id)
            if record is None:
                return None
            payload = self._summary_payload_locked(record)

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
                "logs": list(record.logs),
            }

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
            self._log.info("remote_api_run_finished", run_id=run_id, exit_code=exit_code)

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
            record.status = "succeeded" if exit_code == 0 else "failed"

    def _refresh_record_locked(self, record: RunRecord) -> None:
        if record.finished_at is not None:
            return
        exit_code = record.process.poll()
        if exit_code is None:
            return
        record.exit_code = exit_code
        record.finished_at = _utc_now()
        record.status = "succeeded" if exit_code == 0 else "failed"

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
        }

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
) -> tuple[int, str, bytes]:
    route = urlsplit(request_path).path.rstrip("/") or "/"
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
            return _json_response(201, service.create_run(payload_raw))
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
    service = RemoteAPIService(repo_root=repo_root)

    class RemoteAPIRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._handle_request(method="GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle_request(method="POST")

        def _handle_request(self, *, method: str) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            request_body = self.rfile.read(content_length) if content_length > 0 else None
            status, content_type, body = _api_http_response(
                service,
                method=method,
                request_path=self.path,
                request_body=request_body,
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        with ThreadingHTTPServer((host, port), RemoteAPIRequestHandler) as server:
            log.info("remote_api_listening", host=host, port=port, repo_root=str(repo_root))
            print(f"Remote API listening on http://{host}:{port}")
            server.serve_forever()
    except OSError as exc:
        log.error("remote_api_bind_failed", host=host, port=port, error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 0
    return 0
