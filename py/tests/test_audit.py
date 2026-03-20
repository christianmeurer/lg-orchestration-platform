from __future__ import annotations

import json
import pathlib
import threading
import unittest.mock
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lg_orch.audit import (
    AuditConfig,
    AuditEvent,
    AuditLogger,
    GCSAuditSink,
    S3AuditSink,
    build_sink,
    to_jsonl,
    utc_now_iso,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    action: str = "run.create",
    outcome: str = "ok",
    subject: str = "user-1",
    roles: list[str] | None = None,
    resource_id: str | None = "run-abc",
    detail: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        ts=utc_now_iso(),
        subject=subject,
        roles=roles if roles is not None else ["operator"],
        action=action,
        resource_id=resource_id,
        outcome=outcome,  # type: ignore[arg-type]
        detail=detail,
    )


# ---------------------------------------------------------------------------
# to_jsonl
# ---------------------------------------------------------------------------


def test_to_jsonl_round_trips() -> None:
    event = _make_event()
    line = to_jsonl(event)
    parsed: dict[str, Any] = json.loads(line)
    assert parsed["subject"] == event.subject
    assert parsed["action"] == event.action
    assert parsed["outcome"] == event.outcome
    assert parsed["resource_id"] == event.resource_id
    assert parsed["roles"] == event.roles
    assert parsed["ts"] == event.ts
    assert parsed["detail"] == event.detail


def test_to_jsonl_no_trailing_newline() -> None:
    line = to_jsonl(_make_event())
    assert not line.endswith("\n")


def test_to_jsonl_none_fields() -> None:
    event = _make_event(resource_id=None, detail=None)
    parsed: dict[str, Any] = json.loads(to_jsonl(event))
    assert parsed["resource_id"] is None
    assert parsed["detail"] is None


# ---------------------------------------------------------------------------
# AuditLogger — basic write
# ---------------------------------------------------------------------------


def test_audit_logger_writes_line(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    try:
        event = _make_event()
        logger.log(event)
    finally:
        logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed: dict[str, Any] = json.loads(lines[0])
    assert parsed["action"] == "run.create"
    assert parsed["outcome"] == "ok"


def test_audit_logger_multiple_events_in_order(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    actions = ["run.create", "run.read", "run.cancel", "run.list", "run.search"]
    try:
        for action in actions:
            logger.log(_make_event(action=action))
    finally:
        logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(actions)
    for i, action in enumerate(actions):
        assert json.loads(lines[i])["action"] == action


def test_audit_logger_creates_parent_dirs(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / "nested" / "deep" / "audit.jsonl"
    logger = AuditLogger(log_path)
    try:
        logger.log(_make_event())
    finally:
        logger.close()
    assert log_path.exists()


# ---------------------------------------------------------------------------
# AuditLogger — thread safety
# ---------------------------------------------------------------------------


def test_audit_logger_thread_safety(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    n_threads = 50
    events_per_thread = 10

    def _write() -> None:
        for _ in range(events_per_thread):
            logger.log(_make_event())

    threads = [threading.Thread(target=_write) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * events_per_thread
    # Each line must be valid JSON
    for line in lines:
        parsed = json.loads(line)
        assert "action" in parsed


# ---------------------------------------------------------------------------
# build_sink
# ---------------------------------------------------------------------------


def test_build_sink_returns_none_when_no_sink_type() -> None:
    cfg = AuditConfig(sink_type=None)
    assert build_sink(cfg) is None


def test_build_sink_returns_none_for_s3_when_no_bucket() -> None:
    cfg = AuditConfig(sink_type="s3", s3_bucket=None)
    assert build_sink(cfg) is None


def test_build_sink_returns_none_for_gcs_when_no_bucket() -> None:
    cfg = AuditConfig(sink_type="gcs", gcs_bucket=None)
    assert build_sink(cfg) is None


def test_build_sink_returns_s3_sink() -> None:
    cfg = AuditConfig(sink_type="s3", s3_bucket="my-bucket", s3_prefix="logs", s3_region="eu-west-1")
    sink = build_sink(cfg)
    assert isinstance(sink, S3AuditSink)


def test_build_sink_returns_gcs_sink() -> None:
    cfg = AuditConfig(sink_type="gcs", gcs_bucket="my-bucket", gcs_prefix="logs")
    sink = build_sink(cfg)
    assert isinstance(sink, GCSAuditSink)


def test_build_sink_returns_none_for_unknown_type() -> None:
    cfg = AuditConfig(sink_type="unknown")
    assert build_sink(cfg) is None


# ---------------------------------------------------------------------------
# S3AuditSink graceful no-op when aioboto3 is not installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_sink_noop_when_aioboto3_missing() -> None:
    """S3AuditSink.export silently no-ops when aioboto3 is absent."""
    import sys

    sink = S3AuditSink(bucket="b", prefix="p", region="us-east-1")
    # Setting sys.modules entry to None causes `import aioboto3` to raise ImportError.
    with patch.dict("sys.modules", {"aioboto3": None}):
        # Should complete without raising
        await sink.export(_make_event())

    # Verify no side-effects: batch still empty (noop path taken)
    assert sink._batch == []


@pytest.mark.asyncio
async def test_s3_sink_noop_import_error_via_mock() -> None:
    """S3AuditSink.export silently no-ops when aioboto3 raises ImportError."""
    sink = S3AuditSink(bucket="b", prefix="p", region="us-east-1")

    with patch.dict("sys.modules", {"aioboto3": None}):
        await sink.export(_make_event())


# ---------------------------------------------------------------------------
# GCSAuditSink graceful no-op when google-cloud-storage is not installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gcs_sink_noop_when_gcs_missing() -> None:
    sink = GCSAuditSink(bucket="b", prefix="p")
    import sys

    # Remove the google.cloud.storage module if present so import fails
    gcs_key = "google.cloud.storage"
    original = sys.modules.pop(gcs_key, unittest.mock.sentinel.absent)
    google_cloud_key = "google.cloud"
    original_gc = sys.modules.pop(google_cloud_key, unittest.mock.sentinel.absent)
    google_key = "google"
    original_g = sys.modules.get(google_key, unittest.mock.sentinel.absent)

    try:
        # Should silently no-op
        await sink.export(_make_event())
    finally:
        if original is not unittest.mock.sentinel.absent:
            sys.modules[gcs_key] = original  # type: ignore[assignment]
        if original_gc is not unittest.mock.sentinel.absent:
            sys.modules[google_cloud_key] = original_gc  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Integration: AuditEvent emitted on successful run.create via _api_http_response
# ---------------------------------------------------------------------------


def test_audit_event_emitted_on_run_create(tmp_path: pathlib.Path) -> None:
    """AuditEvent with action=run.create and outcome=ok is written for a successful POST /runs."""
    import lg_orch.remote_api as api_mod
    from lg_orch.remote_api import RemoteAPIService, _api_http_response

    # Build a minimal mock service that succeeds on create_run
    mock_service = MagicMock(spec=RemoteAPIService)
    mock_service._rate_limiter = None
    mock_service.create_run.return_value = {
        "run_id": "abc123",
        "status": "running",
        "created_at": utc_now_iso(),
        "started_at": utc_now_iso(),
        "finished_at": None,
        "exit_code": None,
        "trace_out_dir": "artifacts",
        "trace_path": "artifacts/run-abc123.json",
        "log_lines": 0,
        "request_id": "",
        "auth_subject": "",
        "client_ip": "",
        "thread_id": "",
        "checkpoint_id": "",
        "pending_approval": False,
        "pending_approval_summary": "",
        "approval_history": [],
        "cancel_requested": False,
        "cancellable": True,
        "request": "hello",
    }

    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    original = api_mod._audit_logger
    api_mod._audit_logger = logger

    try:
        body = json.dumps({"request": "hello"}).encode()
        status, _ct, _body = _api_http_response(
            mock_service,
            method="POST",
            request_path="/v1/runs",
            request_body=body,
            auth_mode="off",
        )
        assert status == 201
    finally:
        api_mod._audit_logger = original
        logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed: dict[str, Any] = json.loads(lines[0])
    assert parsed["action"] == "run.create"
    assert parsed["outcome"] == "ok"


def test_audit_event_denied_on_auth_failure(tmp_path: pathlib.Path) -> None:
    """AuditEvent with outcome=denied is written when bearer auth fails."""
    import lg_orch.remote_api as api_mod
    from lg_orch.remote_api import RemoteAPIService, _api_http_response

    mock_service = MagicMock(spec=RemoteAPIService)
    mock_service._rate_limiter = None

    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    original = api_mod._audit_logger
    api_mod._audit_logger = logger

    try:
        status, _ct, _body = _api_http_response(
            mock_service,
            method="POST",
            request_path="/v1/runs",
            request_body=b'{"request":"test"}',
            auth_mode="bearer",
            expected_bearer_token="secret",
            authorization_header="Bearer wrong-token",
        )
        assert status == 403
    finally:
        api_mod._audit_logger = original
        logger.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["outcome"] == "denied"
