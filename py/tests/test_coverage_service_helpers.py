"""Tests for api/service.py internal helpers: _resume_argv, _semantic_memories_from_trace,
_write_trace_approval_state, _apply_approval_state_to_record.
Also covers api/metrics.py and more of api/streaming.py.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import field
from pathlib import Path
from typing import Any

import pytest

from lg_orch.api.service import (
    RunRecord,
    _apply_approval_state_to_record,
    _approval_state_from_trace,
    _resume_argv,
    _semantic_memories_from_trace,
    _trace_path_for_run,
    _utc_now,
    _write_trace_approval_state,
)


# ---------------------------------------------------------------------------
# _trace_path_for_run
# ---------------------------------------------------------------------------


def test_trace_path_for_run_relative(tmp_path: Path) -> None:
    result = _trace_path_for_run(tmp_path, Path("artifacts/api"), "abc")
    assert result == (tmp_path / "artifacts" / "api" / "run-abc.json").resolve()


def test_trace_path_for_run_absolute(tmp_path: Path) -> None:
    abs_dir = tmp_path / "custom"
    result = _trace_path_for_run(tmp_path, abs_dir, "abc")
    assert result == (abs_dir / "run-abc.json").resolve()


# ---------------------------------------------------------------------------
# _resume_argv
# ---------------------------------------------------------------------------


def _make_record(**kwargs: Any) -> RunRecord:
    defaults = {
        "run_id": "test",
        "request": "hello",
        "argv": ["python", "-m", "lg_orch", "run", "--trace"],
        "trace_out_dir": Path("/tmp"),
        "trace_path": Path("/tmp/run-test.json"),
        "process": None,
        "created_at": _utc_now(),
        "started_at": _utc_now(),
    }
    defaults.update(kwargs)
    return RunRecord(**defaults)


def test_resume_argv_basic() -> None:
    record = _make_record()
    result = _resume_argv(record)
    assert "--resume" in result
    assert "python" in result


def test_resume_argv_with_thread_id() -> None:
    record = _make_record(thread_id="t1", checkpoint_id="cp1")
    result = _resume_argv(record)
    assert "--resume" in result
    assert "--thread-id" in result
    assert "t1" in result
    assert "--checkpoint-id" in result
    assert "cp1" in result


def test_resume_argv_strips_existing_resume() -> None:
    record = _make_record(
        argv=["python", "--resume", "--thread-id", "old-t", "--checkpoint-id", "old-cp", "--trace"],
        thread_id="new-t",
        checkpoint_id="new-cp",
    )
    result = _resume_argv(record)
    # Old resume/thread-id/checkpoint-id should be stripped
    assert result.count("--resume") == 1
    assert result.count("--thread-id") == 1
    assert "new-t" in result
    assert "old-t" not in result


# ---------------------------------------------------------------------------
# _semantic_memories_from_trace
# ---------------------------------------------------------------------------


def test_semantic_memories_from_trace_none() -> None:
    assert _semantic_memories_from_trace(None, request="hello") == []


def test_semantic_memories_from_trace_basic() -> None:
    trace: dict[str, Any] = {
        "request": "Fix the bug",
        "final": "Bug has been fixed.",
    }
    result = _semantic_memories_from_trace(trace, request="Fix the bug")
    kinds = {m["kind"] for m in result}
    assert "request" in kinds
    assert "final_output" in kinds


def test_semantic_memories_from_trace_loop_summaries() -> None:
    trace: dict[str, Any] = {
        "request": "test",
        "loop_summaries": [
            {"summary": "First loop completed", "failure_class": "compile_error"},
            {"summary": "Second loop fixed it"},
            {"summary": ""},  # empty should be skipped
        ],
    }
    result = _semantic_memories_from_trace(trace, request="test")
    loop_mems = [m for m in result if m["kind"] == "loop_summary"]
    assert len(loop_mems) == 2
    assert loop_mems[0]["source"] == "compile_error"
    assert loop_mems[1]["source"] == "loop_summary"  # default


def test_semantic_memories_from_trace_approval_history() -> None:
    trace: dict[str, Any] = {
        "request": "test",
        "approval": {
            "pending": True,
            "summary": "Needs approval",
            "pending_details": {"operation_class": "apply_patch"},
            "history": [
                {
                    "decision": "approved",
                    "actor": "alice",
                    "challenge_id": "ch1",
                    "rationale": "looks good",
                }
            ],
        },
    }
    result = _semantic_memories_from_trace(trace, request="test")
    approval_mems = [m for m in result if m["kind"] == "approval_summary"]
    assert len(approval_mems) == 1
    history_mems = [m for m in result if m["kind"] == "approval_history"]
    assert len(history_mems) == 1
    assert "alice" in history_mems[0]["summary"]
    assert "ch1" in history_mems[0]["summary"]
    assert "looks good" in history_mems[0]["summary"]


# ---------------------------------------------------------------------------
# _write_trace_approval_state
# ---------------------------------------------------------------------------


def test_write_trace_approval_state_creates_approval(tmp_path: Path) -> None:
    trace_path = tmp_path / "run-abc.json"
    trace_path.write_text(json.dumps({"request": "hello"}), encoding="utf-8")

    _write_trace_approval_state(
        trace_path=trace_path,
        pending=True,
        pending_details={"challenge_id": "ch1", "operation_class": "apply_patch"},
        history=[{"action": "requested"}],
        last_decision={"decision": "approved", "actor": "bob"},
    )

    updated = json.loads(trace_path.read_text(encoding="utf-8"))
    assert updated["approval"]["pending"] is True
    assert updated["approval"]["last_decision"]["actor"] == "bob"
    assert "apply_patch" in updated["approval"]["summary"]


def test_write_trace_approval_state_clears_details(tmp_path: Path) -> None:
    trace_path = tmp_path / "run-abc.json"
    trace_path.write_text(
        json.dumps({"approval": {"pending_details": {"old": "data"}, "summary": "old"}}),
        encoding="utf-8",
    )

    _write_trace_approval_state(
        trace_path=trace_path,
        pending=False,
        pending_details=None,
        history=[],
        last_decision=None,
    )

    updated = json.loads(trace_path.read_text(encoding="utf-8"))
    assert updated["approval"]["pending"] is False
    assert "pending_details" not in updated["approval"]
    assert updated["approval"]["summary"] == ""


def test_write_trace_approval_state_missing_file(tmp_path: Path) -> None:
    trace_path = tmp_path / "nonexistent.json"
    # Should not raise
    _write_trace_approval_state(
        trace_path=trace_path,
        pending=True,
        pending_details=None,
        history=[],
        last_decision=None,
    )


def test_write_trace_approval_state_invalid_json(tmp_path: Path) -> None:
    trace_path = tmp_path / "bad.json"
    trace_path.write_text("not json", encoding="utf-8")
    # Should not raise
    _write_trace_approval_state(
        trace_path=trace_path,
        pending=True,
        pending_details=None,
        history=[],
        last_decision=None,
    )


# ---------------------------------------------------------------------------
# _apply_approval_state_to_record
# ---------------------------------------------------------------------------


def test_apply_approval_state_to_record_basic() -> None:
    record = _make_record()
    _apply_approval_state_to_record(record, {
        "pending": True,
        "summary": "Needs approval",
        "details": {"challenge_id": "ch1", "operation_class": "apply_patch"},
        "history": [{"action": "requested", "tool": "apply_patch"}],
        "thread_id": "t1",
        "checkpoint_id": "cp1",
    })
    assert record.thread_id == "t1"
    assert record.checkpoint_id == "cp1"
    assert record.pending_approval is True
    assert "approval" in record.pending_approval_summary.lower() or len(record.pending_approval_summary) > 0
    assert len(record.approval_history) == 1


def test_apply_approval_state_to_record_no_pending() -> None:
    record = _make_record()
    _apply_approval_state_to_record(record, {
        "pending": False,
        "summary": "",
        "details": {},
        "history": [],
        "thread_id": "",
        "checkpoint_id": "",
    })
    assert record.pending_approval is False
    assert record.pending_approval_summary == ""


# ---------------------------------------------------------------------------
# api/metrics.py
# ---------------------------------------------------------------------------


def test_metrics_handle_returns_200() -> None:
    from lg_orch.api.metrics import handle_metrics

    status, ct, body = handle_metrics("GET")
    assert status == 200
    assert "text/plain" in ct or "text" in ct


def test_metrics_handle_post_returns_405() -> None:
    from lg_orch.api.metrics import handle_metrics

    status, _, _ = handle_metrics("POST")
    assert status == 405
