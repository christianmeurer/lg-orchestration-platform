"""Tests for remote_api.py handler helpers and route dispatch to boost coverage."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.remote_api import (
    RemoteAPIService,
    _api_http_response,
    _audit_action_and_resource,
    _authorize_request,
    _json_response,
    _request_client_ip,
    _request_scheme,
)


# ---------------------------------------------------------------------------
# _json_response
# ---------------------------------------------------------------------------


def test_json_response_returns_correct_structure() -> None:
    status, ct, body = _json_response(200, {"ok": True})
    assert status == 200
    assert ct == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"ok": True}


def test_json_response_with_error() -> None:
    status, _, body = _json_response(500, {"error": "internal"})
    assert status == 500
    assert json.loads(body)["error"] == "internal"


# ---------------------------------------------------------------------------
# _request_client_ip
# ---------------------------------------------------------------------------


def test_request_client_ip_from_forwarded_for() -> None:
    ip = _request_client_ip(
        client_address=("10.0.0.1", 8080),
        forwarded_for="203.0.113.5, 10.0.0.2",
        trust_forwarded_headers=True,
    )
    assert ip == "203.0.113.5"


def test_request_client_ip_from_client_address() -> None:
    ip = _request_client_ip(
        client_address=("10.0.0.1", 8080),
        forwarded_for=None,
        trust_forwarded_headers=True,
    )
    assert ip == "10.0.0.1"


def test_request_client_ip_no_trust_forwarded() -> None:
    ip = _request_client_ip(
        client_address=("10.0.0.1", 8080),
        forwarded_for="203.0.113.5",
        trust_forwarded_headers=False,
    )
    assert ip == "10.0.0.1"


def test_request_client_ip_no_address() -> None:
    ip = _request_client_ip(
        client_address=None,
        forwarded_for=None,
        trust_forwarded_headers=False,
    )
    assert ip == ""


# ---------------------------------------------------------------------------
# _request_scheme
# ---------------------------------------------------------------------------


def test_request_scheme_from_forwarded_proto() -> None:
    scheme = _request_scheme(forwarded_proto="https", trust_forwarded_headers=True)
    assert scheme == "https"


def test_request_scheme_no_trust_returns_http() -> None:
    scheme = _request_scheme(forwarded_proto="https", trust_forwarded_headers=False)
    assert scheme == "http"


def test_request_scheme_no_header_returns_http() -> None:
    scheme = _request_scheme(forwarded_proto=None, trust_forwarded_headers=True)
    assert scheme == "http"


# ---------------------------------------------------------------------------
# _authorize_request
# ---------------------------------------------------------------------------


def test_authorize_healthz_unauthenticated() -> None:
    subject, err = _authorize_request(
        route="/healthz",
        request_path="/healthz",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=True,
    )
    assert err is None
    assert subject == ""


def test_authorize_metrics_always_allowed() -> None:
    subject, err = _authorize_request(
        route="/metrics",
        request_path="/metrics",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert err is None


def test_authorize_off_mode() -> None:
    subject, err = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="off",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert err is None


def test_authorize_unsupported_auth_mode() -> None:
    _, err = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="oauth",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert err is not None
    status, _, body = err
    assert status == 500


def test_authorize_bearer_not_configured() -> None:
    _, err = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs",
        auth_mode="bearer",
        expected_bearer_token=None,
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert err is not None
    status, _, _ = err
    assert status == 503


def test_authorize_bearer_from_query_string() -> None:
    subject, err = _authorize_request(
        route="/v1/runs",
        request_path="/v1/runs?access_token=secret",
        auth_mode="bearer",
        expected_bearer_token="secret",
        authorization_header=None,
        allow_unauthenticated_healthz=False,
    )
    assert err is None
    assert subject == "bearer"


# ---------------------------------------------------------------------------
# _audit_action_and_resource
# ---------------------------------------------------------------------------


def test_audit_action_run_create() -> None:
    action, rid = _audit_action_and_resource(
        method="POST", route="/v1/runs", path_parts=["v1", "runs"], status=201
    )
    assert action == "run.create"


def test_audit_action_run_list() -> None:
    action, _ = _audit_action_and_resource(
        method="GET", route="/v1/runs", path_parts=["v1", "runs"], status=200
    )
    assert action == "run.list"


def test_audit_action_run_read_v1() -> None:
    action, rid = _audit_action_and_resource(
        method="GET", route="/v1/runs/abc", path_parts=["v1", "runs", "abc"], status=200
    )
    assert action == "run.read"
    assert rid == "abc"


def test_audit_action_run_read_legacy() -> None:
    action, rid = _audit_action_and_resource(
        method="GET", route="/runs/abc", path_parts=["runs", "abc"], status=200
    )
    assert action == "run.read"
    assert rid == "abc"


def test_audit_action_run_cancel_v1() -> None:
    action, rid = _audit_action_and_resource(
        method="POST",
        route="/v1/runs/abc/cancel",
        path_parts=["v1", "runs", "abc", "cancel"],
        status=202,
    )
    assert action == "run.cancel"
    assert rid == "abc"


def test_audit_action_run_cancel_legacy() -> None:
    action, rid = _audit_action_and_resource(
        method="POST",
        route="/runs/abc/cancel",
        path_parts=["runs", "abc", "cancel"],
        status=202,
    )
    assert action == "run.cancel"
    assert rid == "abc"


def test_audit_action_run_approve_v1() -> None:
    action, rid = _audit_action_and_resource(
        method="POST",
        route="/v1/runs/abc/approve",
        path_parts=["v1", "runs", "abc", "approve"],
        status=202,
    )
    assert action == "run.approve"
    assert rid == "abc"


def test_audit_action_run_reject_legacy() -> None:
    action, rid = _audit_action_and_resource(
        method="POST",
        route="/runs/abc/reject",
        path_parts=["runs", "abc", "reject"],
        status=202,
    )
    assert action == "run.approve"
    assert rid == "abc"


def test_audit_action_stream() -> None:
    action, rid = _audit_action_and_resource(
        method="GET",
        route="/v1/runs/abc/stream",
        path_parts=["v1", "runs", "abc", "stream"],
        status=200,
    )
    assert action == "run.read"
    assert rid == "abc"


def test_audit_action_logs() -> None:
    action, rid = _audit_action_and_resource(
        method="GET",
        route="/v1/runs/abc/logs",
        path_parts=["v1", "runs", "abc", "logs"],
        status=200,
    )
    assert action == "run.read"
    assert rid == "abc"


def test_audit_action_run_search() -> None:
    action, _ = _audit_action_and_resource(
        method="GET",
        route="/runs/search",
        path_parts=["runs", "search"],
        status=200,
    )
    assert action == "run.search"


def test_audit_action_unknown_route() -> None:
    action, _ = _audit_action_and_resource(
        method="GET",
        route="/unknown",
        path_parts=["unknown"],
        status=200,
    )
    assert action == "api.request"


# ---------------------------------------------------------------------------
# API handler integration: additional route coverage
# ---------------------------------------------------------------------------


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


def test_api_root_redirects_to_spa(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/",
        request_body=None,
    )
    assert status == 200
    assert "text/html" in ct
    assert b"/app/" in body


def test_api_root_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/",
        request_body=None,
    )
    assert status == 405


def test_api_healthz_rejects_post(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healthz",
        request_body=None,
    )
    assert status == 405


def test_api_runs_list_legacy_endpoint(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/",
        request_body=None,
    )
    assert status == 200
    assert json.loads(body)["runs"] == []


def test_api_runs_search_missing_query(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search",
        request_body=None,
    )
    assert status == 422
    assert json.loads(body)["error"] == "missing_required_param"


def test_api_runs_search_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="done\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    # Create a run first
    _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "Analyze search test"}).encode(),
    )
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/search?q=Analyze",
        request_body=None,
    )
    assert status == 200
    payload = json.loads(body)
    assert "results" in payload


def test_api_v1_runs_method_not_allowed(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="DELETE",
        request_path="/v1/runs",
        request_body=None,
    )
    # DELETE to /v1/runs should return 405
    assert status == 405


def test_api_v1_run_logs_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/missing/logs",
        request_body=None,
    )
    assert status == 404


def test_api_v1_run_cancel_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/missing/cancel",
        request_body=None,
    )
    assert status == 404


def test_api_v1_run_approve_rejects_bad_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/some-run/approve",
        request_body=b"not valid json {{{",
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_api_v1_run_approve_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/missing/approve",
        request_body=json.dumps({}).encode(),
    )
    assert status == 404


def test_api_v1_run_reject_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/missing/reject",
        request_body=json.dumps({}).encode(),
    )
    assert status == 404


def test_api_v1_run_get_method_not_allowed(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/some-run",
        request_body=None,
    )
    # POST to a single run resource should be 405
    assert status == 405


def test_api_metrics_endpoint(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/metrics",
        request_body=None,
    )
    # Metrics endpoint should return 200 with text content
    assert status == 200


def test_api_v1_run_non_array_json_returns_400(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=b'"just a string"',
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"
