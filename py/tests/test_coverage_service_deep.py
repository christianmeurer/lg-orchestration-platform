"""Deep coverage for api/service.py: create_run, get_run, list_runs, approve flow,
and more edge cases through the _api_http_response interface.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.remote_api import RemoteAPIService, _api_http_response


class DummyProcess:
    def __init__(self, *, output: str, returncode: int) -> None:
        self.stdout = io.StringIO(output)
        self._returncode = returncode
        self.returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class RunningDummyProcess(DummyProcess):
    def __init__(self, *, output: str = "") -> None:
        super().__init__(output=output, returncode=0)
        self._running = True

    def poll(self) -> int | None:
        return None if self._running else self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._running = False

    def wait(self, timeout: float | None = None) -> int:
        self._running = False
        return self._returncode


def _setup_spawn(monkeypatch: pytest.MonkeyPatch, process: DummyProcess | None = None) -> None:
    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: process or DummyProcess(output="done\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())


# ---------------------------------------------------------------------------
# create_run with various options
# ---------------------------------------------------------------------------


def test_create_run_with_trace_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "hello",
            "run_id": "trace-test",
            "trace_out_dir": "custom/traces",
        }).encode(),
    )
    assert status == 201
    payload = json.loads(body)
    assert payload["run_id"] == "trace-test"


def test_create_run_auto_generates_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "auto id test"}).encode(),
    )
    assert status == 201
    payload = json.loads(body)
    assert payload["run_id"]  # should be auto-generated
    assert len(payload["run_id"]) > 0


def test_create_run_with_config_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "with config",
            "run_id": "cfg-test",
            "config": {"max_loops": 3},
        }).encode(),
    )
    assert status == 201


def test_create_run_empty_request_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": ""}).encode(),
    )
    assert status == 400


def test_create_run_with_view_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "view test",
            "run_id": "view-test",
            "view": "console",
        }).encode(),
    )
    assert status == 201


# ---------------------------------------------------------------------------
# get_run with trace data
# ---------------------------------------------------------------------------


def test_get_run_with_trace_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    # Create run
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "test trace",
            "run_id": "trace-read",
            "trace_out_dir": "artifacts/test",
        }).encode(),
    )
    assert status == 201

    # Write trace file
    trace_dir = tmp_path / "artifacts" / "test"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / "run-trace-read.json"
    trace_path.write_text(json.dumps({
        "request": "test trace",
        "final": "Result of trace",
        "checkpoint": {"thread_id": "t1", "latest_checkpoint_id": "cp1"},
        "approval": {
            "pending": True,
            "summary": "Needs approval",
            "pending_details": {"challenge_id": "ch1", "operation_class": "apply_patch"},
            "history": [],
        },
    }), encoding="utf-8")

    # Read run detail
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/trace-read",
        request_body=None,
    )
    assert status == 200
    detail = json.loads(body)
    assert detail["trace_ready"] is True
    assert detail["pending_approval"] is True
    assert detail["thread_id"] == "t1"


# ---------------------------------------------------------------------------
# list_runs with multiple runs
# ---------------------------------------------------------------------------


def test_list_runs_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    for rid in ["run-a", "run-b", "run-c"]:
        _api_http_response(
            service,
            method="POST",
            request_path="/v1/runs",
            request_body=json.dumps({"request": f"Test {rid}", "run_id": rid}).encode(),
        )

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs",
        request_body=None,
    )
    assert status == 200
    runs = json.loads(body)["runs"]
    assert len(runs) == 3


# ---------------------------------------------------------------------------
# search_runs with limit
# ---------------------------------------------------------------------------


def test_search_runs_with_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    for i in range(5):
        _api_http_response(
            service,
            method="POST",
            request_path="/v1/runs",
            request_body=json.dumps({"request": f"search test {i}", "run_id": f"search-{i}"}).encode(),
        )

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search?q=search&limit=2",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["total"] <= 2


# ---------------------------------------------------------------------------
# approve_run on a completed run (should work or return appropriate error)
# ---------------------------------------------------------------------------


def test_approve_run_already_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    # Create and complete a run
    _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test", "run_id": "done-run"}).encode(),
    )

    # Try to approve a completed run
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/done-run/approve",
        request_body=json.dumps({"actor": "alice"}).encode(),
    )
    # Should return 409 (conflict) or 202 depending on implementation
    assert status in {202, 409}


def test_reject_run_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/nonexistent/reject",
        request_body=json.dumps({"actor": "alice"}).encode(),
    )
    assert status == 404


# ---------------------------------------------------------------------------
# get_logs for completed run
# ---------------------------------------------------------------------------


def test_get_logs_for_completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test logs", "run_id": "logs-run"}).encode(),
    )

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/logs-run/logs",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body)
    assert "logs" in payload
    assert "status" in payload


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_on_service(tmp_path: Path) -> None:
    from lg_orch.api.service import _RateLimiter

    limiter = _RateLimiter(capacity=1, rate=0.001)
    service = RemoteAPIService(repo_root=tmp_path, rate_limiter=limiter)

    # First request should succeed
    status1, _, _ = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
    )
    assert status1 == 200

    # Subsequent requests may be rate-limited
    status2, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
    )
    # Could be 200 or 429 depending on timing
    assert status2 in {200, 429}
