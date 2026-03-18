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
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self) -> int:
        return self._returncode

    def terminate(self) -> None:
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

    def wait(self) -> int:
        self._running = False
        return self._returncode


def test_api_http_response_creates_run_lists_runs_and_loads_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    captured_argv: list[str] = []
    captured_cwd: list[Path] = []
    captured_env: dict[str, str] = {}

    def fake_spawn(*, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> DummyProcess:
        captured_argv[:] = list(argv)
        captured_cwd[:] = [cwd]
        captured_env.clear()
        if env is not None:
            captured_env.update(env)
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
        request_id="req-123",
        client_ip="203.0.113.10",
    )
    assert status == 201
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["run_id"] == "abc"
    assert payload["status"] == "succeeded"
    assert payload["trace_ready"] is False
    assert payload["request_id"] == "req-123"
    assert payload["client_ip"] == "203.0.113.10"
    assert captured_cwd == [tmp_path.resolve()]
    assert "--trace" in captured_argv
    assert captured_argv[captured_argv.index("--run-id") + 1] == "abc"
    assert Path(captured_argv[captured_argv.index("--trace-out-dir") + 1]) == Path("artifacts/api")
    assert captured_env["LG_REQUEST_ID"] == "req-123"
    assert captured_env["LG_REMOTE_API_CLIENT_IP"] == "203.0.113.10"

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
        lambda *, argv, cwd, env=None: DummyProcess(output="", returncode=0),
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


def test_api_http_response_enforces_bearer_auth(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs",
        request_body=None,
        auth_mode="bearer",
        expected_bearer_token="secret-token",
    )
    assert status == 401
    assert json.loads(body.decode("utf-8"))["error"] == "missing_bearer_token"

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs",
        request_body=None,
        auth_mode="bearer",
        expected_bearer_token="secret-token",
        authorization_header="Bearer wrong-token",
    )
    assert status == 403
    assert json.loads(body.decode("utf-8"))["error"] == "invalid_bearer_token"

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs",
        request_body=None,
        auth_mode="bearer",
        expected_bearer_token="secret-token",
        authorization_header="Bearer secret-token",
    )
    assert status == 200
    assert json.loads(body.decode("utf-8"))["runs"] == []

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
        auth_mode="bearer",
        expected_bearer_token="secret-token",
        allow_unauthenticated_healthz=True,
    )
    assert status == 200
    assert json.loads(body.decode("utf-8"))["ok"] is True


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


def test_api_http_response_can_cancel_running_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    process = RunningDummyProcess(output="running\n")

    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: process,
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: None)

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "abc"}).encode("utf-8"),
    )
    assert status == 201
    created = json.loads(body.decode("utf-8"))
    assert created["status"] == "running"
    assert created["cancellable"] is True

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc/cancel",
        request_body=None,
    )
    assert status == 202
    payload = json.loads(body.decode("utf-8"))
    assert payload["run_id"] == "abc"
    assert payload["status"] in {"cancelling", "cancelled"}
    assert payload["cancel_requested"] is True
    assert process.terminated is True

    detail = service.get_run("abc")
    assert detail is not None
    assert detail["cancel_requested"] is True


def test_api_http_response_cancel_returns_not_found_for_missing_run(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/missing/cancel",
        request_body=None,
    )
    assert status == 404
    assert json.loads(body.decode("utf-8"))["error"] == "not_found"


def test_run_store_persists_on_create_and_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lg_orch.run_store import RunStore

    db_path = tmp_path / "runs.sqlite"
    store = RunStore(db_path=db_path)
    service = RemoteAPIService(repo_root=tmp_path, run_store=store)

    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="line1\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test persist", "run_id": "persist1"}).encode("utf-8"),
    )
    assert status == 201

    # record should be in the store after create + finish (daemon ran synchronously)
    row = store.get_run("persist1")
    assert row is not None
    assert row["run_id"] == "persist1"
    assert row["status"] in {"succeeded", "failed", "running"}

    # list_runs should include the persisted record
    all_runs = service.list_runs()
    run_ids = [r["run_id"] for r in all_runs]
    assert "persist1" in run_ids

    store.close()


def test_api_http_response_marks_run_suspended_when_trace_requires_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    def fake_spawn(*, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> DummyProcess:
        run_id = argv[argv.index("--run-id") + 1]
        trace_dir = Path(argv[argv.index("--trace-out-dir") + 1])
        trace_path = (cwd / trace_dir / f"run-{run_id}.json").resolve()
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "tool_results": [
                        {
                            "tool": "apply_patch",
                            "ok": False,
                            "artifacts": {
                                "error": "approval_required",
                                "approval": {
                                    "required": True,
                                    "status": "challenge_required",
                                    "operation_class": "apply_patch",
                                    "challenge_id": "approval:apply_patch",
                                    "reason": "missing_approval_token",
                                },
                            },
                        }
                    ],
                    "checkpoint": {
                        "thread_id": "thread-abc",
                        "latest_checkpoint_id": "cp-123",
                    },
                }
            ),
            encoding="utf-8",
        )
        return DummyProcess(output="approval needed\n", returncode=1)

    monkeypatch.setattr(remote_api, "_spawn_run_subprocess", fake_spawn)
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "abc"}).encode("utf-8"),
    )
    assert status == 201
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "suspended"
    assert payload["pending_approval"] is True
    assert payload["checkpoint_id"] == "cp-123"
    assert payload["thread_id"] == "thread-abc"


def test_api_http_response_approves_suspended_run_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    spawn_calls: list[dict[str, Any]] = []

    def fake_spawn(*, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> DummyProcess:
        call_no = len(spawn_calls)
        spawn_calls.append({"argv": list(argv), "env": dict(env) if env is not None else {}})
        run_id = argv[argv.index("--run-id") + 1]
        trace_dir = Path(argv[argv.index("--trace-out-dir") + 1])
        trace_path = (cwd / trace_dir / f"run-{run_id}.json").resolve()
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        if call_no == 0:
            trace_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "tool_results": [
                            {
                                "tool": "apply_patch",
                                "ok": False,
                                "artifacts": {
                                    "error": "approval_required",
                                    "approval": {
                                        "required": True,
                                        "status": "challenge_required",
                                        "operation_class": "apply_patch",
                                        "challenge_id": "approval:apply_patch",
                                        "reason": "missing_approval_token",
                                    },
                                },
                            }
                        ],
                        "checkpoint": {
                            "thread_id": "thread-abc",
                            "latest_checkpoint_id": "cp-123",
                        },
                    }
                ),
                encoding="utf-8",
            )
            return DummyProcess(output="approval needed\n", returncode=1)
        return RunningDummyProcess(output="resumed\n")

    thread_calls = {"count": 0}

    def fake_thread(*, target, name):
        thread_calls["count"] += 1
        if thread_calls["count"] == 1:
            target()

    monkeypatch.setattr(remote_api, "_spawn_run_subprocess", fake_spawn)
    monkeypatch.setattr(remote_api, "_start_daemon_thread", fake_thread)

    status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "abc"}).encode("utf-8"),
    )
    assert status == 201

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc/approve",
        request_body=json.dumps({"actor": "chris", "rationale": "looks safe"}).encode("utf-8"),
    )
    assert status == 202
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "running"
    assert payload["pending_approval"] is False
    assert spawn_calls[1]["argv"][-5:] == ["--resume", "--thread-id", "thread-abc", "--checkpoint-id", "cp-123"]
    approvals = json.loads(spawn_calls[1]["env"]["LG_RESUME_APPROVALS_JSON"])
    assert approvals["apply_patch"]["challenge_id"] == "approval:apply_patch"


def test_api_http_response_rejects_suspended_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    def fake_spawn(*, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> DummyProcess:
        run_id = argv[argv.index("--run-id") + 1]
        trace_dir = Path(argv[argv.index("--trace-out-dir") + 1])
        trace_path = (cwd / trace_dir / f"run-{run_id}.json").resolve()
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "tool_results": [
                        {
                            "tool": "apply_patch",
                            "ok": False,
                            "artifacts": {
                                "error": "approval_required",
                                "approval": {
                                    "required": True,
                                    "status": "challenge_required",
                                    "operation_class": "apply_patch",
                                    "challenge_id": "approval:apply_patch",
                                    "reason": "missing_approval_token",
                                },
                            },
                        }
                    ],
                    "checkpoint": {
                        "thread_id": "thread-abc",
                        "latest_checkpoint_id": "cp-123",
                    },
                }
            ),
            encoding="utf-8",
        )
        return DummyProcess(output="approval needed\n", returncode=1)

    monkeypatch.setattr(remote_api, "_spawn_run_subprocess", fake_spawn)
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze", "run_id": "abc"}).encode("utf-8"),
    )
    assert status == 201

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc/reject",
        request_body=json.dumps({"actor": "chris"}).encode("utf-8"),
    )
    assert status == 202
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "rejected"
    assert payload["pending_approval"] is False
    assert payload["approval_history"][-1]["decision"] == "rejected"


# ---------------------------------------------------------------------------
# _RateLimiter tests
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_one_and_blocks_second() -> None:
    from lg_orch.remote_api import _RateLimiter

    rl = _RateLimiter(capacity=1, rate=100.0)
    assert rl.acquire() is True
    assert rl.acquire() is False


def test_rate_limiter_refills_over_time() -> None:
    import time as _time

    from lg_orch.remote_api import _RateLimiter

    rl = _RateLimiter(capacity=1, rate=100.0)
    assert rl.acquire() is True
    assert rl.acquire() is False
    _time.sleep(0.02)  # 100 tokens/s → 2 tokens in 20ms
    assert rl.acquire() is True


def test_api_http_response_returns_429_when_rate_limited(tmp_path: Path) -> None:
    from lg_orch.remote_api import _RateLimiter

    rl = _RateLimiter(capacity=1, rate=0.001)
    # drain the single token
    assert rl.acquire() is True

    service = RemoteAPIService(repo_root=tmp_path, rate_limiter=rl)

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
    )
    assert status == 429
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "rate_limit_exceeded"


def test_api_http_response_passes_when_rate_limiter_not_set(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path, rate_limiter=None)

    status, _, _ = _api_http_response(
        service,
        method="GET",
        request_path="/healthz",
        request_body=None,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------


def test_remote_api_service_namespace_isolation(tmp_path: Path) -> None:
    from lg_orch.run_store import RunStore

    db = tmp_path / "runs.sqlite"
    store_a = RunStore(db_path=db, namespace="alpha")
    store_b = RunStore(db_path=db, namespace="beta")

    record_a = {
        "run_id": "run-alpha-1",
        "request": "task alpha",
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "trace_out_dir": "artifacts/runs",
        "trace_path": "artifacts/runs/run-alpha-1.json",
        "request_id": "",
        "auth_subject": "",
        "client_ip": "",
    }
    record_b = {
        "run_id": "run-beta-1",
        "request": "task beta",
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "trace_out_dir": "artifacts/runs",
        "trace_path": "artifacts/runs/run-beta-1.json",
        "request_id": "",
        "auth_subject": "",
        "client_ip": "",
    }
    store_a.upsert(record_a)
    store_b.upsert(record_b)

    runs_a = store_a.list_runs()
    runs_b = store_b.list_runs()

    assert len(runs_a) == 1
    assert runs_a[0]["run_id"] == "run-alpha-1"
    assert len(runs_b) == 1
    assert runs_b[0]["run_id"] == "run-beta-1"

    store_a.close()
    store_b.close()


# ---------------------------------------------------------------------------
# SPA route tests
# ---------------------------------------------------------------------------


def test_spa_served_at_root(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/",
        request_body=None,
    )
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert b"<!DOCTYPE html>" in body


def test_spa_served_at_ui(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/ui",
        request_body=None,
    )
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert b"<!DOCTYPE html>" in body


def test_spa_method_not_allowed(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/",
        request_body=None,
    )
    assert status == 405
    assert json.loads(body.decode("utf-8"))["error"] == "method_not_allowed"
