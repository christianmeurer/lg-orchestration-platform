from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.remote_api import RemoteAPIService, _api_http_response, _approval_token_for_challenge


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


def test_api_http_response_creates_run_lists_runs_and_loads_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    captured_argv: list[str] = []
    captured_cwd: list[Path] = []
    captured_env: dict[str, str] = {}

    def fake_spawn(
        *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> DummyProcess:
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

    status, _, _body = _api_http_response(
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

    def fake_spawn(
        *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> DummyProcess:
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

    def fake_spawn(
        *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> DummyProcess:
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
    assert spawn_calls[1]["argv"][-5:] == [
        "--resume",
        "--thread-id",
        "thread-abc",
        "--checkpoint-id",
        "cp-123",
    ]
    # HIGH FIX 5: Approvals are now passed via a temp file, not an env var.
    approvals_file = spawn_calls[1]["env"]["LG_RESUME_APPROVALS_FILE"]
    approvals = json.loads(Path(approvals_file).read_text(encoding="utf-8"))
    assert approvals["apply_patch"]["challenge_id"] == "approval:apply_patch"


def test_api_http_response_rejects_suspended_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    def fake_spawn(
        *, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> DummyProcess:
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


# ---------------------------------------------------------------------------
# _approval_token_for_challenge tests
# ---------------------------------------------------------------------------


def test_approval_token_uses_hmac_format_when_secret_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_RUNNER_APPROVAL_SECRET", "test-secret-value")
    token = _approval_token_for_challenge("approval:apply_patch")
    parts = token.split(".")
    assert len(parts) == 4, f"expected 4 dot-separated fields, got {len(parts)}: {token!r}"
    challenge_id, iat_str, nonce, signature = parts
    assert challenge_id == "approval:apply_patch"
    assert iat_str.isdigit()
    assert len(nonce) == 32
    assert len(signature) == 64


def test_approval_token_falls_back_to_legacy_when_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LG_RUNNER_APPROVAL_SECRET", raising=False)
    token = _approval_token_for_challenge("approval:apply_patch")
    assert token == "approve:approval:apply_patch"


def test_approval_token_hmac_is_consistent_with_known_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import hmac as _hmac

    monkeypatch.setenv("LG_RUNNER_APPROVAL_SECRET", "known-secret")
    token = _approval_token_for_challenge("chal-id")
    parts = token.split(".")
    assert len(parts) == 4
    challenge_id, iat_str, nonce, signature = parts
    # HMAC is computed over pipe-separated message, but token uses dot-separated format.
    message = f"{challenge_id}|{iat_str}|{nonce}"
    expected_sig = _hmac.new(b"known-secret", message.encode(), hashlib.sha256).hexdigest()
    assert signature == expected_sig


def test_approval_token_two_calls_produce_different_nonces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_RUNNER_APPROVAL_SECRET", "secret")
    t1 = _approval_token_for_challenge("chal")
    t2 = _approval_token_for_challenge("chal")
    assert t1.split(".")[2] != t2.split(".")[2], "nonces must differ between calls"


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
    # Root now serves a meta-refresh redirect to /app/
    assert b"/app/" in body


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
    # /ui now serves a meta-refresh redirect to /app/ (same as root)
    assert b"/app/" in body


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


# ---------------------------------------------------------------------------
# Wave-7 SSE streaming tests (/runs/{run_id}/stream)
# ---------------------------------------------------------------------------


def test_stream_completed_run_returns_events_and_done_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stream_new_sse replays trace events then sends the done sentinel."""
    import io as _io

    import lg_orch.remote_api as _ra
    from lg_orch.remote_api import _stream_new_sse

    service = RemoteAPIService(repo_root=tmp_path)
    monkeypatch.setattr(
        _ra,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="", returncode=0),
    )
    monkeypatch.setattr(_ra, "_start_daemon_thread", lambda *, target, name: target())

    # Create a run and let the DummyProcess finish immediately (exit 0).
    status, _, _body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps(
            {
                "request": "sse completed test",
                "run_id": "sse-done-001",
                "trace_out_dir": "artifacts/sse-t",
            }
        ).encode(),
    )
    assert status == 201

    # Write a trace file that contains two trace events.
    trace_path = tmp_path / "artifacts" / "sse-t" / "run-sse-done-001.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_events = [
        {"ts_ms": 1_000, "kind": "node_start", "data": {"name": "ingest"}},
        {"ts_ms": 2_000, "kind": "node_end", "data": {"name": "ingest"}},
    ]
    trace_path.write_text(
        json.dumps({"run_id": "sse-done-001", "events": trace_events}),
        encoding="utf-8",
    )

    wfile = _io.BytesIO()
    _stream_new_sse(service, "sse-done-001", wfile)
    output = wfile.getvalue().decode("utf-8")

    # All frames start with "data: "
    frames = [
        line[len("data: ") :].strip() for line in output.splitlines() if line.startswith("data: ")
    ]
    parsed = [json.loads(f) for f in frames if f]

    kinds = {ev.get("kind") for ev in parsed}
    assert "node_start" in kinds
    assert "node_end" in kinds
    # Last frame must be the done sentinel
    assert parsed[-1] == {"type": "done"}


def test_stream_nonexistent_run_returns_404(tmp_path: Path) -> None:
    """GET /runs/<unknown>/stream returns HTTP 404 before any stream is opened."""
    service = RemoteAPIService(repo_root=tmp_path)

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/not-a-real-run/stream",
        request_body=None,
    )
    assert status == 404
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "not_found"
    assert payload["run_id"] == "not-a-real-run"


def test_push_run_event_sends_through_active_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """push_run_event forwards a live event through an active _stream_new_sse.

    The sequence is:
    1. Start a thread running _stream_new_sse for an active run.
    2. Wait for _stream_new_sse to register its queue in _run_streams.
    3. Push a live event via push_run_event and then a None sentinel.
    4. The stream should drain the queue, emit the event, send the done
       sentinel, and return — all within 5 seconds.
    """
    import io as _io
    import threading as _th
    import time as _time_mod

    import lg_orch.remote_api as _ra
    from lg_orch.remote_api import _stream_new_sse, push_run_event

    service = RemoteAPIService(repo_root=tmp_path)
    monkeypatch.setattr(
        _ra,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: RunningDummyProcess(),
    )
    # Don't start the capture thread — keep the run alive (poll() returns None).
    monkeypatch.setattr(_ra, "_start_daemon_thread", lambda *, target, name: None)

    status, _, _body2 = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps(
            {
                "request": "live sse test",
                "run_id": "sse-live-001",
                "trace_out_dir": "artifacts/sse-live",
            }
        ).encode(),
    )
    assert status == 201

    live_event: dict[str, object] = {
        "ts_ms": 5_000,
        "kind": "tool_call",
        "data": {"tool": "exec"},
    }

    wfile = _io.BytesIO()
    errors: list[Exception] = []

    def _run() -> None:
        try:
            _stream_new_sse(service, "sse-live-001", wfile)
        except Exception as exc:
            errors.append(exc)

    t = _th.Thread(target=_run, daemon=True)
    t.start()

    # Poll until _stream_new_sse registers its queue (up to 2 s).
    for _ in range(200):
        with _ra._run_streams_lock:
            if "sse-live-001" in _ra._run_streams:
                break
        _time_mod.sleep(0.01)

    # Push a live event and the None end-of-stream sentinel.
    push_run_event("sse-live-001", live_event)  # type: ignore[arg-type]
    with _ra._run_streams_lock:
        registered_q = _ra._run_streams.get("sse-live-001")
    if registered_q is not None:
        registered_q.put_nowait(None)

    t.join(timeout=5)
    assert not t.is_alive(), "stream did not terminate within 5 s"
    assert not errors

    with _ra._run_streams_lock:
        _ra._run_streams.pop("sse-live-001", None)

    output = wfile.getvalue().decode("utf-8")
    frames = [
        line[len("data: ") :].strip() for line in output.splitlines() if line.startswith("data: ")
    ]
    parsed = [json.loads(f) for f in frames if f]

    kinds = {ev.get("kind") for ev in parsed}
    assert "tool_call" in kinds
    assert parsed[-1] == {"type": "done"}


# ---------------------------------------------------------------------------
# GET /runs/search tests
# ---------------------------------------------------------------------------


def test_runs_search_returns_results_when_matches_exist(tmp_path: Path) -> None:
    from lg_orch.run_store import RunStore

    db_path = tmp_path / "runs.sqlite"
    store = RunStore(db_path=db_path)
    store.upsert(
        {
            "run_id": "search-abc",
            "request": "deploy the kubernetes cluster",
            "status": "succeeded",
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "exit_code": 0,
            "trace_out_dir": "artifacts/runs",
            "trace_path": "artifacts/runs/run-search-abc.json",
            "request_id": "",
            "auth_subject": "",
            "client_ip": "",
        }
    )
    service = RemoteAPIService(repo_root=tmp_path, run_store=store)

    status, content_type, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search?q=kubernetes",
        request_body=None,
    )
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert "results" in payload
    assert "total" in payload
    assert payload["total"] >= 1
    run_ids = [r["run_id"] for r in payload["results"]]
    assert "search-abc" in run_ids

    store.close()


def test_runs_search_returns_empty_when_no_matches(tmp_path: Path) -> None:
    from lg_orch.run_store import RunStore

    db_path = tmp_path / "runs.sqlite"
    store = RunStore(db_path=db_path)
    store.upsert(
        {
            "run_id": "no-match-run",
            "request": "analyze the pipeline",
            "status": "running",
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "exit_code": None,
            "trace_out_dir": "artifacts/runs",
            "trace_path": "artifacts/runs/run-no-match-run.json",
            "request_id": "",
            "auth_subject": "",
            "client_ip": "",
        }
    )
    service = RemoteAPIService(repo_root=tmp_path, run_store=store)

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search?q=xyzzy_nonexistent_token_99",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["results"] == []
    assert payload["total"] == 0

    store.close()


def test_runs_search_returns_422_when_q_missing(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search",
        request_body=None,
    )
    assert status == 422
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "missing_required_param"
    assert payload["param"] == "q"


# ---------------------------------------------------------------------------
# Wave-8 multi-path approval policy tests
# ---------------------------------------------------------------------------


def test_set_approval_policy_timed(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, content_type, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-timed-001/approval-policy",
        request_body=json.dumps(
            {
                "policy": {
                    "kind": "timed",
                    "timeout_seconds": 120.0,
                    "auto_action": "reject",
                }
            }
        ).encode("utf-8"),
    )
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "policy_set"
    assert payload["run_id"] == "run-timed-001"


def test_vote_on_run_returns_pending(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    # set a quorum(2) policy first
    _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-quorum-001/approval-policy",
        request_body=json.dumps(
            {"policy": {"kind": "quorum", "required_approvals": 2, "required_rejections": 2}}
        ).encode("utf-8"),
    )

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-quorum-001/vote",
        request_body=json.dumps(
            {"reviewer_id": "alice", "role": None, "action": "approve", "comment": "lgtm"}
        ).encode("utf-8"),
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "pending"
    assert payload["votes_cast"] == 1


def test_vote_on_run_resolves_to_approved(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-quorum-002/approval-policy",
        request_body=json.dumps(
            {"policy": {"kind": "quorum", "required_approvals": 2, "required_rejections": 2}}
        ).encode("utf-8"),
    )

    _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-quorum-002/vote",
        request_body=json.dumps(
            {"reviewer_id": "alice", "role": None, "action": "approve", "comment": ""}
        ).encode("utf-8"),
    )

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-quorum-002/vote",
        request_body=json.dumps(
            {"reviewer_id": "bob", "role": None, "action": "approve", "comment": ""}
        ).encode("utf-8"),
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "approved"
    assert payload["votes_cast"] == 2


def test_vote_on_run_returns_404_without_policy(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run-no-policy/vote",
        request_body=json.dumps(
            {"reviewer_id": "alice", "role": None, "action": "approve", "comment": ""}
        ).encode("utf-8"),
    )
    assert status == 404
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "policy_not_found"
    assert payload["run_id"] == "run-no-policy"


# ---------------------------------------------------------------------------
# Healing loop endpoint tests
# ---------------------------------------------------------------------------


def test_start_healing_loop_returns_loop_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    service = RemoteAPIService(repo_root=tmp_path)

    started_threads: list[str] = []

    def fake_thread_start(self: threading.Thread) -> None:
        started_threads.append(self.name)

    monkeypatch.setattr(threading.Thread, "start", fake_thread_start)

    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=json.dumps({"repo_path": str(tmp_path), "poll_interval_seconds": 30.0}).encode(
            "utf-8"
        ),
    )
    assert status == 201
    payload = json.loads(body.decode("utf-8"))
    assert "loop_id" in payload
    assert payload["status"] == "started"
    assert len(payload["loop_id"]) > 0


def test_stop_healing_loop_returns_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    service = RemoteAPIService(repo_root=tmp_path)

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    # Start first
    start_status, _, start_body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=json.dumps({"repo_path": str(tmp_path)}).encode("utf-8"),
    )
    assert start_status == 201
    loop_id = json.loads(start_body.decode("utf-8"))["loop_id"]

    # Stop it
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path=f"/healing/{loop_id}/stop",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["loop_id"] == loop_id
    assert payload["status"] == "stopped"


def test_get_healing_jobs_returns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    service = RemoteAPIService(repo_root=tmp_path)

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    start_status, _, start_body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=json.dumps({"repo_path": str(tmp_path)}).encode("utf-8"),
    )
    assert start_status == 201
    loop_id = json.loads(start_body.decode("utf-8"))["loop_id"]

    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path=f"/healing/{loop_id}/jobs",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert "jobs" in payload
    assert isinstance(payload["jobs"], list)


# ---------------------------------------------------------------------------
# Wave 10.2 — /metrics endpoint tests
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_200(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, content_type, _body = _api_http_response(
        service,
        method="GET",
        request_path="/metrics",
        request_body=None,
    )
    assert status == 200
    assert "text/plain" in content_type


def test_metrics_endpoint_contains_lula_runs_total(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/metrics",
        request_body=None,
    )
    assert b"lula_runs_total" in body


def test_metrics_endpoint_unauthenticated(tmp_path: Path) -> None:
    """GET /metrics must be accessible without a bearer token even when auth_mode=bearer."""
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="GET",
        request_path="/metrics",
        request_body=None,
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Pure helper functions — _request_client_ip, _request_scheme, etc.
# ---------------------------------------------------------------------------


def test_request_client_ip_uses_forwarded_for_when_trusted() -> None:
    from lg_orch.remote_api import _request_client_ip

    result = _request_client_ip(
        client_address=("192.168.1.1", 5000),
        forwarded_for="10.0.0.1, 10.0.0.2",
        trust_forwarded_headers=True,
    )
    assert result == "10.0.0.1"


def test_request_client_ip_ignores_forwarded_for_when_untrusted() -> None:
    from lg_orch.remote_api import _request_client_ip

    result = _request_client_ip(
        client_address=("192.168.1.1", 5000),
        forwarded_for="10.0.0.1",
        trust_forwarded_headers=False,
    )
    assert result == "192.168.1.1"


def test_request_client_ip_returns_empty_when_no_address() -> None:
    from lg_orch.remote_api import _request_client_ip

    result = _request_client_ip(
        client_address=None,
        forwarded_for=None,
        trust_forwarded_headers=False,
    )
    assert result == ""


def test_request_scheme_uses_forwarded_proto_when_trusted() -> None:
    from lg_orch.remote_api import _request_scheme

    result = _request_scheme(forwarded_proto="https, http", trust_forwarded_headers=True)
    assert result == "https"


def test_request_scheme_defaults_to_http_when_untrusted() -> None:
    from lg_orch.remote_api import _request_scheme

    result = _request_scheme(forwarded_proto="https", trust_forwarded_headers=False)
    assert result == "http"


def test_request_scheme_defaults_to_http_when_no_proto() -> None:
    from lg_orch.remote_api import _request_scheme

    result = _request_scheme(forwarded_proto=None, trust_forwarded_headers=True)
    assert result == "http"


def test_request_id_from_value_returns_given_value() -> None:
    from lg_orch.remote_api import _request_id_from_value

    assert _request_id_from_value("req-123") == "req-123"


def test_request_id_from_value_generates_uuid_for_empty() -> None:
    from lg_orch.remote_api import _request_id_from_value

    result = _request_id_from_value("")
    assert len(result) == 32  # hex UUID


def test_request_id_from_value_generates_uuid_for_none() -> None:
    from lg_orch.remote_api import _request_id_from_value

    result = _request_id_from_value(None)
    assert len(result) == 32


def test_json_response_returns_proper_format() -> None:
    from lg_orch.remote_api import _json_response

    status, content_type, body = _json_response(200, {"ok": True})
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"ok": True}


def test_authorize_request_allows_healthz_unauthenticated() -> None:
    from lg_orch.remote_api import _authorize_request

    auth_subject, error = _authorize_request(
        route="/healthz",
        request_path="/healthz",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=True,
    )
    assert error is None
    assert auth_subject == ""


def test_authorize_request_allows_metrics_always() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/metrics",
        request_path="/metrics",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is None


def test_authorize_request_off_mode_allows_all() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="off",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is None


def test_authorize_request_rejects_unsupported_auth_mode() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="basic",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is not None
    assert error[0] == 500


def test_authorize_request_rejects_when_token_not_configured() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="bearer",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is not None
    assert error[0] == 503


def test_authorize_request_rejects_missing_bearer_token() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is not None
    assert error[0] == 401


def test_authorize_request_rejects_invalid_bearer_token() -> None:
    from lg_orch.remote_api import _authorize_request

    _, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header="Bearer wrong-token",
        allow_unauthenticated_healthz=False,
    )
    assert error is not None
    assert error[0] == 403


def test_authorize_request_accepts_valid_bearer_token() -> None:
    from lg_orch.remote_api import _authorize_request

    auth_subject, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header="Bearer secret",
        allow_unauthenticated_healthz=False,
    )
    assert error is None
    assert auth_subject == "bearer"


def test_authorize_request_accepts_query_string_token() -> None:
    from lg_orch.remote_api import _authorize_request

    auth_subject, error = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs?access_token=secret",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert error is None
    assert auth_subject == "bearer"


# ---------------------------------------------------------------------------
# _audit_action_and_resource
# ---------------------------------------------------------------------------


def test_audit_action_run_create() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, _resource = _audit_action_and_resource(
        method="POST", route="/v1/runs", path_parts=["v1", "runs"], status=201
    )
    assert action == "run.create"


def test_audit_action_run_list() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, _resource = _audit_action_and_resource(
        method="GET", route="/v1/runs", path_parts=["v1", "runs"], status=200
    )
    assert action == "run.list"


def test_audit_action_run_read_v1() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="GET", route="/v1/runs/abc", path_parts=["v1", "runs", "abc"], status=200
    )
    assert action == "run.read"
    assert resource == "abc"


def test_audit_action_run_read_short() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="GET", route="/runs/abc", path_parts=["runs", "abc"], status=200
    )
    assert action == "run.read"
    assert resource == "abc"


def test_audit_action_run_cancel_v1() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="POST",
        route="/v1/runs/abc/cancel",
        path_parts=["v1", "runs", "abc", "cancel"],
        status=202,
    )
    assert action == "run.cancel"
    assert resource == "abc"


def test_audit_action_run_cancel_short() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="POST",
        route="/runs/abc/cancel",
        path_parts=["runs", "abc", "cancel"],
        status=202,
    )
    assert action == "run.cancel"
    assert resource == "abc"


def test_audit_action_run_approve_v1() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="POST",
        route="/v1/runs/abc/approve",
        path_parts=["v1", "runs", "abc", "approve"],
        status=202,
    )
    assert action == "run.approve"
    assert resource == "abc"


def test_audit_action_run_approve_short() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="POST",
        route="/runs/abc/approve",
        path_parts=["runs", "abc", "approve"],
        status=202,
    )
    assert action == "run.approve"
    assert resource == "abc"


def test_audit_action_logs_stream() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="GET",
        route="/v1/runs/abc/logs",
        path_parts=["v1", "runs", "abc", "logs"],
        status=200,
    )
    assert action == "run.read"
    assert resource == "abc"


def test_audit_action_run_search() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, _resource = _audit_action_and_resource(
        method="GET",
        route="/runs/search",
        path_parts=["runs", "search"],
        status=200,
    )
    assert action == "run.search"


def test_audit_action_default() -> None:
    from lg_orch.remote_api import _audit_action_and_resource

    action, resource = _audit_action_and_resource(
        method="GET", route="/unknown", path_parts=["unknown"], status=404
    )
    assert action == "api.request"
    assert resource is None


# ---------------------------------------------------------------------------
# _match_parameterized route matching
# ---------------------------------------------------------------------------


def test_match_parameterized_v1_run_logs() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc/logs", "GET", ["v1", "runs", "abc", "logs"])
    assert h is not None


def test_match_parameterized_v1_run_cancel() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc/cancel", "POST", ["v1", "runs", "abc", "cancel"])
    assert h is not None


def test_match_parameterized_v1_run_approve() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc/approve", "POST", ["v1", "runs", "abc", "approve"])
    assert h is not None


def test_match_parameterized_v1_run_reject() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc/reject", "POST", ["v1", "runs", "abc", "reject"])
    assert h is not None


def test_match_parameterized_v1_run_stream() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc/stream", "GET", ["v1", "runs", "abc", "stream"])
    assert h is not None


def test_match_parameterized_v1_run_get() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/v1/runs/abc", "GET", ["v1", "runs", "abc"])
    assert h is not None


def test_match_parameterized_runs_stream() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/runs/abc/stream", "GET", ["runs", "abc", "stream"])
    assert h is not None


def test_match_parameterized_runs_approve() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/runs/abc/approve", "POST", ["runs", "abc", "approve"])
    assert h is not None


def test_match_parameterized_runs_approval_policy() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized(
        "/runs/abc/approval-policy", "POST", ["runs", "abc", "approval-policy"]
    )
    assert h is not None


def test_match_parameterized_runs_vote() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/runs/abc/vote", "POST", ["runs", "abc", "vote"])
    assert h is not None


def test_match_parameterized_app() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/app/index.html", "GET", ["app", "index.html"])
    assert h is not None


def test_match_parameterized_no_match() -> None:
    from lg_orch.remote_api import _match_parameterized

    h = _match_parameterized("/unknown", "GET", ["unknown"])
    assert h is None


# ---------------------------------------------------------------------------
# Short-form /runs/* handler integration tests
# ---------------------------------------------------------------------------


def test_runs_list_endpoint(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs",
        request_body=None,
    )
    assert status == 200
    assert "runs" in json.loads(body.decode("utf-8"))


def test_runs_list_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/",
        request_body=None,
    )
    assert status == 405


def test_healthz_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/healthz",
        request_body=None,
    )
    assert status == 405


def test_v1_run_get_missing(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/nonexistent",
        request_body=None,
    )
    assert status == 404


def test_v1_run_get_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc",
        request_body=None,
    )
    assert status == 405


def test_v1_run_logs_missing(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/nonexistent/logs",
        request_body=None,
    )
    assert status == 404


def test_v1_run_logs_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc/logs",
        request_body=None,
    )
    assert status == 405


def test_v1_run_cancel_rejects_get(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/abc/cancel",
        request_body=None,
    )
    assert status == 405


def test_v1_run_approve_rejects_get(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _ = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/abc/approve",
        request_body=None,
    )
    assert status == 405


def test_v1_run_approve_invalid_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/abc/approve",
        request_body=b"not json",
    )
    assert status == 400


def test_api_404_for_unknown_route(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, _body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/unknown",
        request_body=None,
    )
    assert status == 404
