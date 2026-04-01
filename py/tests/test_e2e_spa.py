"""End-to-end smoke test: API -> SPA -> SSE contract verification.

These tests verify the API contract that the SPA and VS Code extension
depend on, without requiring a running agent or external services.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

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


def _setup_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_api,
        "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="step 1\nstep 2\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())


def test_spa_dist_served_or_503(tmp_path: Path) -> None:
    """Verify /app/ returns either Leptos SPA (200) or dist-not-found (503).

    The SPA serving endpoint checks for a compiled WASM dist directory.
    In test environments without a build, it should return 503 gracefully
    rather than crashing. In production with a dist directory, it returns 200.
    """
    service = RemoteAPIService(repo_root=tmp_path)

    # Without dist directory: expect 503 (dist not found)
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/app/",
        request_body=None,
    )
    assert status in {200, 503}, f"Expected 200 or 503, got {status}"

    if status == 503:
        # Response may be JSON or plain text depending on the SPA router
        decoded = body.decode("utf-8")
        # Just verify we got a non-empty response
        assert len(decoded) > 0


def test_sse_stream_endpoint_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify /v1/runs/{id}/stream returns SSE content-type or 404 for missing run.

    The SSE endpoint is the backbone of the real-time operations console in
    both the Leptos SPA and the VS Code extension. This test verifies:
    1. Existing runs return the SSE sentinel (status=-1, ct='sse')
    2. Missing runs return 404 with proper JSON error
    """
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    # 1. Create a run
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "Analyze the codebase",
            "run_id": "sse-test-run",
        }).encode(),
    )
    assert status == 201

    # 2. Stream endpoint for existing run returns SSE sentinel
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/sse-test-run/stream",
        request_body=None,
    )
    assert status == -1, "Expected SSE sentinel status"
    assert ct == "sse", "Expected SSE content type sentinel"

    # 3. Stream endpoint for missing run via legacy path returns 404
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/nonexistent-run/stream",
        request_body=None,
    )
    assert status == 404
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "not_found"


def test_approval_endpoint_rejects_bad_json(tmp_path: Path) -> None:
    """Verify /v1/runs/{id}/approve returns 400 for invalid JSON.

    The approval endpoint is critical for the VS Code extension's approval
    workflow. It must properly validate input and return structured errors.
    """
    service = RemoteAPIService(repo_root=tmp_path)

    # 1. Invalid JSON body
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/some-run/approve",
        request_body=b"this is not valid json {{{",
    )
    assert status == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "invalid_json"

    # 2. Non-dict JSON body
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/some-run/approve",
        request_body=b'["not", "a", "dict"]',
    )
    assert status == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "invalid_json"

    # 3. Valid JSON but run doesn't exist
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs/nonexistent/approve",
        request_body=json.dumps({"actor": "test-user"}).encode(),
    )
    assert status == 404
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "not_found"

    # 4. Wrong HTTP method
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/some-run/approve",
        request_body=None,
    )
    assert status == 405


def test_run_submission_returns_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POST /v1/runs returns a run_id in the response.

    This is the primary endpoint used by both the VS Code extension and
    the SPA to submit orchestration requests. The response contract must
    include: run_id, status, created_at, and request metadata.
    """
    service = RemoteAPIService(repo_root=tmp_path)
    _setup_spawn(monkeypatch)

    # 1. Submit a run with explicit run_id
    status, ct, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "Implement feature X",
            "run_id": "explicit-id",
        }).encode(),
    )
    assert status == 201
    assert ct == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["run_id"] == "explicit-id"
    assert "status" in payload
    assert "created_at" in payload
    assert payload["status"] in {"running", "succeeded", "failed"}

    # 2. Submit a run without run_id (auto-generated)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "Analyze code quality",
        }).encode(),
    )
    assert status == 201
    payload = json.loads(body.decode("utf-8"))
    assert payload["run_id"]  # must be non-empty
    assert len(payload["run_id"]) > 0

    # 3. Submit with empty request should fail
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": ""}).encode(),
    )
    assert status == 400

    # 4. Duplicate run_id should fail
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({
            "request": "Another request",
            "run_id": "explicit-id",
        }).encode(),
    )
    assert status == 409
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "duplicate_run_id"
