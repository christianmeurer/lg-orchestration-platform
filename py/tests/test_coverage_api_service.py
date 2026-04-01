"""Tests for api/service.py helper functions to boost coverage."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lg_orch.api.service import (
    RunRecord,
    _RateLimiter,
    _apply_trace_state_to_payload,
    _non_empty_str,
    _normalized_run_id,
    _utc_now,
)


# ---------------------------------------------------------------------------
# _utc_now
# ---------------------------------------------------------------------------


def test_utc_now_format() -> None:
    ts = _utc_now()
    assert ts.endswith("Z")
    assert "+" not in ts


# ---------------------------------------------------------------------------
# _non_empty_str
# ---------------------------------------------------------------------------


def test_non_empty_str_valid() -> None:
    assert _non_empty_str("hello") == "hello"


def test_non_empty_str_strips() -> None:
    assert _non_empty_str("  hello  ") == "hello"


def test_non_empty_str_empty() -> None:
    assert _non_empty_str("") is None
    assert _non_empty_str("   ") is None


def test_non_empty_str_non_string() -> None:
    assert _non_empty_str(42) is None
    assert _non_empty_str(None) is None
    assert _non_empty_str([]) is None


# ---------------------------------------------------------------------------
# _normalized_run_id
# ---------------------------------------------------------------------------


def test_normalized_run_id_valid() -> None:
    assert _normalized_run_id("abc-123") == "abc-123"
    assert _normalized_run_id("run.test_1") == "run.test_1"


def test_normalized_run_id_invalid() -> None:
    assert _normalized_run_id("bad id") is None
    assert _normalized_run_id("-start-with-hyphen") is None
    assert _normalized_run_id("") is None
    assert _normalized_run_id(None) is None
    assert _normalized_run_id(42) is None


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_acquire() -> None:
    rl = _RateLimiter(capacity=5, rate=10.0)
    # Should be able to acquire a few tokens immediately
    assert rl.acquire() is True


def test_rate_limiter_exhaustion() -> None:
    rl = _RateLimiter(capacity=2, rate=0.01)  # very slow refill
    assert rl.acquire() is True
    assert rl.acquire() is True
    # Should be exhausted now
    assert rl.acquire() is False


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


def test_run_record_fields() -> None:
    r = RunRecord(
        run_id="abc",
        request="hello",
        argv=["python", "-m", "lg_orch"],
        trace_out_dir=Path("/tmp"),
        trace_path=Path("/tmp/run-abc.json"),
        process=None,
        created_at=_utc_now(),
        started_at=_utc_now(),
    )
    assert r.run_id == "abc"
    assert r.request == "hello"
    assert r.status == "running"
    assert r.process is None
    assert r.cancel_requested is False


# ---------------------------------------------------------------------------
# _apply_trace_state_to_payload
# ---------------------------------------------------------------------------


def test_apply_trace_state_to_payload_basic() -> None:
    payload: dict[str, Any] = {"run_id": "abc", "status": "running"}
    trace: dict[str, Any] = {
        "checkpoint": {"thread_id": "t1", "latest_checkpoint_id": "cp1"},
        "approval": {
            "pending": True,
            "summary": "Approval needed for apply_patch",
            "history": [{"action": "requested", "tool": "apply_patch"}],
        },
    }
    result = _apply_trace_state_to_payload(payload, trace)
    assert result["thread_id"] == "t1"
    assert result["checkpoint_id"] == "cp1"
    assert result["pending_approval"] is True
    assert "approval" in result["pending_approval_summary"].lower() or len(result["pending_approval_summary"]) > 0


def test_apply_trace_state_to_payload_no_trace() -> None:
    payload: dict[str, Any] = {"run_id": "abc"}
    result = _apply_trace_state_to_payload(payload, None)
    assert result["pending_approval"] is False
    assert result["thread_id"] == ""
    assert result["checkpoint_id"] == ""


def test_apply_trace_state_to_payload_empty_trace() -> None:
    payload: dict[str, Any] = {"run_id": "abc"}
    result = _apply_trace_state_to_payload(payload, {})
    assert result["pending_approval"] is False


def test_apply_trace_state_with_tool_result_approval() -> None:
    """When no explicit approval section, check tool_results for approval_required."""
    payload: dict[str, Any] = {"run_id": "abc"}
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
                        "summary": "Patch needs approval",
                    },
                },
            }
        ],
    }
    result = _apply_trace_state_to_payload(payload, trace)
    assert result["pending_approval"] is True
