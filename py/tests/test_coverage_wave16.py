"""Comprehensive coverage boost tests for wave-16 targeting 83%+.

Focuses on api/service.py helpers, api/approvals.py, api/streaming.py,
and api/admin.py.
"""
from __future__ import annotations

import io
import json
import queue
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import lg_orch.remote_api as remote_api
from lg_orch.api.approvals import (
    approval_summary_text,
    approval_token_for_challenge,
    handle_spa_approve,
    tool_name_for_approval,
)
from lg_orch.api.service import (
    RemoteAPIService,
    RunRecord,
    _approval_state_from_trace,
    _apply_trace_state_to_payload,
    _non_empty_str,
    _normalized_run_id,
    _RateLimiter,
    _utc_now,
)
from lg_orch.api.streaming import push_run_event, _run_streams, _run_streams_lock
from lg_orch.remote_api import _api_http_response


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


# ---------------------------------------------------------------------------
# api/approvals.py
# ---------------------------------------------------------------------------


def test_approval_token_for_challenge_insecure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without LG_RUNNER_APPROVAL_SECRET, returns legacy plain-text format."""
    monkeypatch.delenv("LG_RUNNER_APPROVAL_SECRET", raising=False)
    token = approval_token_for_challenge("ch1")
    assert token == "approve:ch1"


def test_approval_token_for_challenge_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    """With LG_RUNNER_APPROVAL_SECRET, returns dot-separated HMAC format."""
    monkeypatch.setenv("LG_RUNNER_APPROVAL_SECRET", "s3cr3t")
    token = approval_token_for_challenge("ch2")
    parts = token.split(".")
    assert len(parts) == 4
    assert parts[0] == "ch2"


def test_tool_name_for_approval_apply_patch() -> None:
    assert tool_name_for_approval(operation_class="apply_patch", challenge_id="ch1") == "apply_patch"


def test_tool_name_for_approval_exec() -> None:
    assert tool_name_for_approval(operation_class="exec", challenge_id="ch1") == "exec"


def test_tool_name_for_approval_default() -> None:
    assert tool_name_for_approval(operation_class="other", challenge_id="ch1") == "apply_patch"


def test_approval_summary_text_basic() -> None:
    text = approval_summary_text({"operation_class": "apply_patch"})
    assert "apply_patch" in text
    assert "approval" in text


def test_approval_summary_text_with_challenge() -> None:
    text = approval_summary_text({
        "operation_class": "apply_patch",
        "challenge_id": "ch1",
    })
    assert "ch1" in text


def test_approval_summary_text_with_custom_reason() -> None:
    text = approval_summary_text({
        "operation_class": "apply_patch",
        "reason": "custom explanation",
    })
    assert "custom explanation" in text


def test_approval_summary_text_standard_reason_not_appended() -> None:
    text = approval_summary_text({
        "operation_class": "apply_patch",
        "reason": "approval_required",
    })
    # Should NOT append the reason because it's one of the standard reasons
    assert text.count("approval_required") <= 1


def test_handle_spa_approve_not_found() -> None:
    mock_service = MagicMock()
    mock_service.approve_run.return_value = None
    with pytest.raises(ValueError, match="run_not_found"):
        handle_spa_approve(mock_service, "run1", {})


def test_handle_spa_approve_with_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LG_RUNNER_APPROVAL_SECRET", raising=False)
    mock_service = MagicMock()
    mock_service.approve_run.return_value = {"run_id": "run1", "status": "running"}
    result = handle_spa_approve(
        mock_service,
        "run1",
        {"challenge_id": "ch1", "actor": "alice", "rationale": "looks good"},
        auth_subject="jwt-user",
    )
    assert result["run_id"] == "run1"
    mock_service.approve_run.assert_called_once()
    call_args = mock_service.approve_run.call_args
    payload = call_args[0][1]
    assert payload["challenge_id"] == "ch1"
    assert payload["actor"] == "alice"
    assert payload["rationale"] == "looks good"


# ---------------------------------------------------------------------------
# api/service.py: _approval_state_from_trace
# ---------------------------------------------------------------------------


def test_approval_state_from_trace_none() -> None:
    result = _approval_state_from_trace(None)
    assert result["pending"] is False
    assert result["summary"] == ""


def test_approval_state_from_trace_with_approval() -> None:
    trace = {
        "checkpoint": {"thread_id": "t1", "latest_checkpoint_id": "cp1"},
        "approval": {
            "pending": True,
            "summary": "Needs approval",
            "history": [{"action": "requested"}],
        },
    }
    result = _approval_state_from_trace(trace)
    assert result["pending"] is True
    assert result["thread_id"] == "t1"
    assert result["checkpoint_id"] == "cp1"
    assert len(result["history"]) == 1


def test_approval_state_from_trace_tool_results_fallback() -> None:
    """When no explicit approval section, detect from tool_results."""
    trace: dict[str, Any] = {
        "tool_results": [
            {
                "tool": "apply_patch",
                "ok": False,
                "artifacts": {
                    "error": "approval_required",
                    "approval": {
                        "challenge_id": "ch1",
                        "tool": "apply_patch",
                    },
                },
            }
        ],
    }
    result = _approval_state_from_trace(trace)
    assert result["pending"] is True


def test_approval_state_from_trace_resume_checkpoint() -> None:
    trace: dict[str, Any] = {
        "checkpoint": {"resume_checkpoint_id": "cp-resume"},
    }
    result = _approval_state_from_trace(trace)
    assert result["checkpoint_id"] == "cp-resume"


# ---------------------------------------------------------------------------
# api/streaming.py: push_run_event
# ---------------------------------------------------------------------------


def test_push_run_event_no_active_stream() -> None:
    """push_run_event is a no-op when no stream is active for the run."""
    push_run_event("nonexistent-run", {"kind": "test"})  # should not raise


def test_push_run_event_with_active_stream() -> None:
    """push_run_event puts events into the stream queue."""
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    with _run_streams_lock:
        _run_streams["test-stream-run"] = q
    try:
        push_run_event("test-stream-run", {"kind": "node", "data": {"name": "router"}})
        event = q.get_nowait()
        assert event is not None
        assert event["kind"] == "node"
    finally:
        with _run_streams_lock:
            _run_streams.pop("test-stream-run", None)


# ---------------------------------------------------------------------------
# api/admin.py — via _api_http_response
# ---------------------------------------------------------------------------


def test_admin_healing_start_method_not_allowed(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/healing/start",
        request_body=None,
    )
    assert status == 405


def test_admin_healing_start_bad_json(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=b"not json",
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_admin_healing_start_non_dict(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=b'"string"',
    )
    assert status == 400
    assert json.loads(body)["error"] == "invalid_json"


def test_admin_healing_start_missing_repo_path(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/start",
        request_body=json.dumps({}).encode(),
    )
    assert status == 400
    assert json.loads(body)["error"] == "missing_repo_path"


def test_admin_healing_stop_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="POST",
        request_path="/healing/loop1/stop",
        request_body=None,
    )
    assert status == 404


def test_admin_healing_jobs_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/healing/loop1/jobs",
        request_body=None,
    )
    assert status == 404


# ---------------------------------------------------------------------------
# _apply_trace_state_to_payload with more scenarios
# ---------------------------------------------------------------------------


def test_apply_trace_state_with_resume_checkpoint() -> None:
    payload: dict[str, Any] = {"run_id": "abc"}
    trace: dict[str, Any] = {
        "checkpoint": {"resume_checkpoint_id": "cp-resume"},
    }
    result = _apply_trace_state_to_payload(payload, trace)
    assert result["checkpoint_id"] == "cp-resume"


def test_apply_trace_state_preserves_existing_payload() -> None:
    payload: dict[str, Any] = {"run_id": "abc", "status": "running", "extra": True}
    result = _apply_trace_state_to_payload(payload, None)
    assert result["extra"] is True
    assert result["run_id"] == "abc"


# ---------------------------------------------------------------------------
# More remote_api handler edge cases
# ---------------------------------------------------------------------------


def test_api_v1_run_stream_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream endpoint returns a special sentinel for SSE handling."""
    service = RemoteAPIService(repo_root=tmp_path)
    monkeypatch.setattr(
        remote_api, "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="done\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test", "run_id": "stream-test"}).encode(),
    )
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/v1/runs/stream-test/stream",
        request_body=None,
    )
    # Stream endpoint returns sentinel status -1 for SSE
    assert status == -1
    assert ct == "sse"


def test_api_runs_stream_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy /runs/{id}/stream returns SSE or 404."""
    service = RemoteAPIService(repo_root=tmp_path)
    monkeypatch.setattr(
        remote_api, "_spawn_run_subprocess",
        lambda *, argv, cwd, env=None: DummyProcess(output="done\n", returncode=0),
    )
    monkeypatch.setattr(remote_api, "_start_daemon_thread", lambda *, target, name: target())

    _api_http_response(
        service,
        method="POST",
        request_path="/v1/runs",
        request_body=json.dumps({"request": "test", "run_id": "stream-legacy"}).encode(),
    )
    status, ct, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/stream-legacy/stream",
        request_body=None,
    )
    assert status == -2
    assert ct == "sse_new"


def test_api_runs_stream_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    status, _, body = _api_http_response(
        service,
        method="GET",
        request_path="/runs/nonexistent/stream",
        request_body=None,
    )
    assert status == 404


# ---------------------------------------------------------------------------
# Service: search_runs, get_logs, get_run details
# ---------------------------------------------------------------------------


def test_service_list_runs_empty(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    assert service.list_runs() == []


def test_service_get_run_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    assert service.get_run("nonexistent") is None


def test_service_get_logs_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    assert service.get_logs("nonexistent") is None


def test_service_cancel_run_not_found(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    assert service.cancel_run("nonexistent") is None


def test_service_search_runs_empty(tmp_path: Path) -> None:
    service = RemoteAPIService(repo_root=tmp_path)
    assert service.search_runs("anything") == []
