from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.remote_api import RemoteAPIService, _api_http_response


class DummyProcess:
    def __init__(self, *, output: str, returncode: int) -> None:
        self.stdout = io.StringIO(output)
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode

    def wait(self) -> int:
        return self._returncode


def test_api_http_response_creates_run_lists_runs_and_loads_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    captured_argv: list[str] = []
    captured_cwd: list[Path] = []

    def fake_spawn(*, argv: list[str], cwd: Path) -> DummyProcess:
        captured_argv[:] = list(argv)
        captured_cwd[:] = [cwd]
        return DummyProcess(output="step 1\nstep 2\n", returncode=0)

    monkeypatch.setattr(remote_api, "_spawn_run_subprocess", fake_spawn)
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    status, content_type, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps(
            {"request": "Analyze logs", "run_id": "abc", "trace_out_dir": "artifacts/api"}
        ).encode("utf-8"),
    )
    assert status == 201
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["run_id"] == "abc"
    assert payload["status"] == "succeeded"
    assert payload["trace_ready"] is False
    assert captured_cwd == [tmp_path.resolve()]
    assert "--trace" in captured_argv
    assert captured_argv[captured_argv.index("--run-id") + 1] == "abc"
    assert Path(captured_argv[captured_argv.index("--trace-out-dir") + 1]) == Path("artifacts/api")

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs",
        request_body=None,
    )
    assert status == 200
    runs_payload = json.loads(body.decode("utf-8"))
    assert runs_payload["runs"][0]["run_id"] == "abc"
    assert runs_payload["runs"][0]["log_lines"] == 2

    trace_path = tmp_path / "artifacts" / "api" / "run-abc.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps({"run_id": "abc", "final": "done"}), encoding="utf-8")

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/abc",
        request_body=None,
    )
    assert status == 200
    detail_payload = json.loads(body.decode("utf-8"))
    assert detail_payload["trace_ready"] is True
    assert detail_payload["trace"]["run_id"] == "abc"

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/abc/logs",
        request_body=None,
    )
    assert status == 200
    logs_payload = json.loads(body.decode("utf-8"))
    assert logs_payload["status"] == "succeeded"
    assert logs_payload["logs"] == ["step 1", "step 2"]


def test_api_http_response_rejects_invalid_payloads_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd: DummyProcess(output="", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=b"{",
    )
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "invalid_json"

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "bad id"}).encode("utf-8"),
    )
    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"] == "invalid_run_id"

    first_status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "abc"}).encode("utf-8"),
    )
    assert first_status == 201

    second_status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze again", "run_id": "abc"}).encode("utf-8"),
    )
    assert second_status == 409
    assert json.loads(body.decode("utf-8"))["error"] == "duplicate_run_id"


def test_api_http_response_healthz_and_missing_run(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
    )
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"ok": True}

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/missing",
        request_body=None,
    )
    assert status == 404
    assert json.loads(body.decode("utf-8"))["error"] == "not_found"
