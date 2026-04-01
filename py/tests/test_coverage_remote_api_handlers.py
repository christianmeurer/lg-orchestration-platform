"""Additional remote_api handler coverage for approval-policy, vote, SPA, and admin routes."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.remote_api import (
    RemoteAPIService,
    _api_http_response,
)


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


def _create_run(
    service: RemoteAPIService,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str = "test-run",
) -> dict[str, Any]:
    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="done\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test", "run_id": run_id}).encode(),
    )
    assert status == 201
    return json.loads(body)


# ---------------------------------------------------------------------------
# Legacy /runs/{id}/approve and /runs/{id}/reject
# ---------------------------------------------------------------------------


def test_legacy_runs_approve_bad_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/approve",
        request_body=b"not json",
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_legacy_runs_approve_non_dict(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/approve",
        request_body=b'"a string"',
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_legacy_runs_approve_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/missing/approve",
        request_body=json.dumps({}).encode(),
    )
    assert status == 404


def test_legacy_runs_reject_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/missing/reject",
        request_body=json.dumps({}).encode(),
    )
    assert status == 404


# ---------------------------------------------------------------------------
# /runs/{id}/approval-policy
# ---------------------------------------------------------------------------


def test_runs_approval_policy_bad_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/approval-policy",
        request_body=b"not json",
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_runs_approval_policy_missing_policy(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/approval-policy",
        request_body=json.dumps({}).encode(),
    )
    assert status == 400
    assert json.loads(body)["error"] == "missing_policy"


def test_runs_approval_policy_unknown_kind(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/approval-policy",
        request_body=json.dumps({"policy": {"kind": "unknown"}}).encode(),
    )
    assert status == 400
    assert json.loads(body)["error"] == "unknown_policy_kind"


def test_runs_approval_policy_timed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _create_run(service, monkeypatch, "r1")
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/r1/approval-policy",
        request_body=json.dumps({
            "policy": {"kind": "timed", "timeout_seconds": 60, "auto_action": "approve"}
        }).encode(),
    )
    assert status == 200


def test_runs_approval_policy_quorum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _create_run(service, monkeypatch, "r2")
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/r2/approval-policy",
        request_body=json.dumps({
            "policy": {"kind": "quorum", "required_approvals": 2, "required_rejections": 1}
        }).encode(),
    )
    assert status == 200


def test_runs_approval_policy_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    _create_run(service, monkeypatch, "r3")
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/r3/approval-policy",
        request_body=json.dumps({
            "policy": {"kind": "role", "required_roles": ["admin"]}
        }).encode(),
    )
    assert status == 200


# ---------------------------------------------------------------------------
# /runs/{id}/vote
# ---------------------------------------------------------------------------


def test_runs_vote_bad_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/vote",
        request_body=b"not json",
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_runs_vote_missing_reviewer_id(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/vote",
        request_body=json.dumps({"action": "approve"}).encode(),
    )
    assert status == 400
    assert json.loads(body)["error"] == "missing_reviewer_id"


def test_runs_vote_invalid_action(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/vote",
        request_body=json.dumps({"reviewer_id": "alice", "action": "defer"}).encode(),
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_action"


def test_runs_vote_policy_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/run1/vote",
        request_body=json.dumps({
            "reviewer_id": "alice", "action": "approve"
        }).encode(),
    )
    assert status == 404
    assert json.loads(body)["error"] == "policy_not_found"


# ---------------------------------------------------------------------------
# SPA routes
# ---------------------------------------------------------------------------


def test_spa_route_returns_response(tmp_path: Path) -> None:
    """SPA route via /app/ should return either dist content or 503."""
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/app/",
        request_body=None,
    )
    # Without a dist directory, should return 503 (dist not found)
    assert status in {200, 503}


def test_spa_route_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/app/something",
        request_body=None,
    )
    assert status == 405


# ---------------------------------------------------------------------------
# /runs/search edge cases
# ---------------------------------------------------------------------------


def test_runs_search_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/search",
        request_body=None,
    )
    assert status == 405


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------


def test_unknown_route_returns_404(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/unknown/path",
        request_body=None,
    )
    assert status == 404


# ---------------------------------------------------------------------------
# /runs/ legacy list endpoint
# ---------------------------------------------------------------------------


def test_legacy_runs_list_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/runs/",
        request_body=None,
    )
    assert status == 405


# ---------------------------------------------------------------------------
# /v1/runs/{id}/approve with method not allowed
# ---------------------------------------------------------------------------


def test_v1_run_approve_rejects_get(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/some-run/approve",
        request_body=None,
    )
    assert status == 405


def test_v1_run_cancel_rejects_get(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/some-run/cancel",
        request_body=None,
    )
    assert status == 405


def test_v1_run_logs_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/some-run/logs",
        request_body=None,
    )
    assert status == 405
