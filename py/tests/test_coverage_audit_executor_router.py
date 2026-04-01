"""Tests for audit, executor helpers, and router coverage gaps."""
from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lg_orch.audit import (
    AuditEvent,
    AuditLogger,
    AuditSink,
    S3AuditSink,
    GCSAuditSink,
    to_jsonl,
    utc_now_iso,
)
from lg_orch.nodes.executor import (
    _as_int,
    _budget_failure_result,
    _coerce_approval_token,
    _configured_write_allowlist,
    _estimate_patch_bytes,
    _normalize_rel_path,
    _path_matches_allowlist,
    _apply_patch_changed_paths,
    _approval_for_tool,
)
from lg_orch.nodes.router import _classify_intent, _default_route


# ---------------------------------------------------------------------------
# AuditLogger — export with sink in running event loop
# ---------------------------------------------------------------------------


def _make_event(
    *,
    action: str = "run.create",
    outcome: str = "ok",
) -> AuditEvent:
    return AuditEvent(
        ts=utc_now_iso(),
        subject="test",
        roles=["operator"],
        action=action,
        resource_id="run-1",
        outcome=outcome,  # type: ignore[arg-type]
        detail=None,
    )


@pytest.mark.asyncio
async def test_audit_logger_export_in_event_loop(tmp_path: pathlib.Path) -> None:
    """When called inside a running event loop, _export_async creates a task."""
    exported_events: list[AuditEvent] = []

    class CaptureSink(AuditSink):
        async def export(self, event: AuditEvent) -> None:
            exported_events.append(event)

    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, sink=CaptureSink())
    try:
        logger.log(_make_event())
        # Give the task a moment to run
        await asyncio.sleep(0.1)
    finally:
        logger.close()

    assert len(exported_events) == 1


def test_audit_logger_export_no_event_loop(tmp_path: pathlib.Path) -> None:
    """When called outside an event loop, _export_async fires a background thread."""
    exported = threading.Event()

    class CaptureSink(AuditSink):
        async def export(self, event: AuditEvent) -> None:
            exported.set()

    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, sink=CaptureSink())
    try:
        logger.log(_make_event())
        assert exported.wait(timeout=2.0), "sink.export was not called via background thread"
    finally:
        logger.close()


@pytest.mark.asyncio
async def test_s3_sink_accumulates_until_batch_full() -> None:
    """S3AuditSink accumulates events in _batch until max_batch triggers a flush."""
    sink = S3AuditSink(bucket="b", prefix="p", region="us-east-1")
    sink._max_batch = 100  # very large so time-based flush triggers
    sink._flush_interval = 0.0  # immediate time-based flush

    # With aioboto3 missing, export is a no-op (import fails)
    with patch.dict("sys.modules", {"aioboto3": None}):
        await sink.export(_make_event())
    # batch stays empty because aioboto3 import fails early
    assert len(sink._batch) == 0


@pytest.mark.asyncio
async def test_gcs_sink_noop_import_error() -> None:
    """GCSAuditSink silently no-ops when google-cloud-storage is absent."""
    sink = GCSAuditSink(bucket="b", prefix="p")
    with patch.dict("sys.modules", {"google.cloud": None, "google.cloud.storage": None}):
        await sink.export(_make_event())
    # Should not have accumulated anything
    assert len(sink._batch) == 0


# ---------------------------------------------------------------------------
# Executor helpers: _as_int
# ---------------------------------------------------------------------------


def test_as_int_bool() -> None:
    assert _as_int(True, default=0) == 0


def test_as_int_int() -> None:
    assert _as_int(42, default=0) == 42


def test_as_int_string() -> None:
    assert _as_int(" 7 ", default=0) == 7


def test_as_int_bad_string() -> None:
    assert _as_int("abc", default=99) == 99


def test_as_int_none() -> None:
    assert _as_int(None, default=5) == 5


def test_as_int_float_returns_default() -> None:
    assert _as_int(3.14, default=0) == 0


# ---------------------------------------------------------------------------
# Executor helpers: _budget_failure_result
# ---------------------------------------------------------------------------


def test_budget_failure_result_basic() -> None:
    result = _budget_failure_result(
        tool="apply_patch",
        message="budget exceeded",
        error_tag="tool_call_budget_exceeded",
        route_metadata={"provider": "local"},
    )
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["tool"] == "apply_patch"
    assert result["artifacts"]["error"] == "tool_call_budget_exceeded"


def test_budget_failure_result_with_extra_artifacts() -> None:
    result = _budget_failure_result(
        tool="exec",
        message="too big",
        error_tag="patch_too_big",
        route_metadata={},
        artifacts_extra={"max_bytes": 100000},
    )
    assert result["artifacts"]["max_bytes"] == 100000
    assert result["artifacts"]["error"] == "patch_too_big"


# ---------------------------------------------------------------------------
# Executor helpers: _normalize_rel_path
# ---------------------------------------------------------------------------


def test_normalize_rel_path_backslash() -> None:
    assert _normalize_rel_path("src\\main\\file.py") == "src/main/file.py"


def test_normalize_rel_path_strips() -> None:
    assert _normalize_rel_path("  src/file.py  ") == "src/file.py"


# ---------------------------------------------------------------------------
# Executor helpers: _coerce_approval_token
# ---------------------------------------------------------------------------


def test_coerce_approval_token_non_dict() -> None:
    assert _coerce_approval_token("not a dict") is None


def test_coerce_approval_token_missing_challenge_id() -> None:
    assert _coerce_approval_token({"token": "abc"}) is None


def test_coerce_approval_token_missing_token() -> None:
    assert _coerce_approval_token({"challenge_id": "abc"}) is None


def test_coerce_approval_token_empty_challenge() -> None:
    assert _coerce_approval_token({"challenge_id": "  ", "token": "abc"}) is None


def test_coerce_approval_token_dot_separated() -> None:
    result = _coerce_approval_token({
        "challenge_id": "cid",
        "token": "a.b.c.d",
    })
    assert result is not None
    assert result["challenge_id"] == "cid"
    assert result["token"] == "a.b.c.d"


def test_coerce_approval_token_pipe_separated() -> None:
    result = _coerce_approval_token({
        "challenge_id": "cid",
        "token": "a|b|c|d",
    })
    assert result is not None
    assert result["token"] == "a|b|c|d"


def test_coerce_approval_token_legacy_plain() -> None:
    result = _coerce_approval_token({
        "challenge_id": "cid",
        "token": "simple-token",
    })
    assert result is not None
    assert result["token"] == "simple-token"


def test_coerce_approval_token_dot_with_empty_part() -> None:
    assert _coerce_approval_token({
        "challenge_id": "cid",
        "token": "a..c.d",
    }) is None


def test_coerce_approval_token_pipe_with_empty_part() -> None:
    assert _coerce_approval_token({
        "challenge_id": "cid",
        "token": "a||c|d",
    }) is None


def test_coerce_approval_token_bad_segment_count() -> None:
    """Tokens with 2 or 3 separators but not exactly 4 parts should be rejected."""
    assert _coerce_approval_token({
        "challenge_id": "cid",
        "token": "a.b.c",
    }) is None


# ---------------------------------------------------------------------------
# Executor helpers: _approval_for_tool
# ---------------------------------------------------------------------------


def test_approval_for_tool_direct_in_payload() -> None:
    state: dict[str, Any] = {}
    result = _approval_for_tool(
        state,
        tool_name="apply_patch",
        input_payload={"approval": {"challenge_id": "c1", "token": "t1"}},
    )
    assert result is not None
    assert result["challenge_id"] == "c1"


def test_approval_for_tool_from_state_approvals() -> None:
    state: dict[str, Any] = {
        "approvals": {
            "apply_patch": {"challenge_id": "c2", "token": "t2"},
        }
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["challenge_id"] == "c2"


def test_approval_for_tool_from_resume_approvals() -> None:
    state: dict[str, Any] = {
        "approvals": {},
        "_resume_approvals": {
            "apply_patch": {"challenge_id": "c3", "token": "t3"},
        },
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["challenge_id"] == "c3"


def test_approval_for_tool_from_mutations() -> None:
    state: dict[str, Any] = {
        "approvals": {
            "mutations": {
                "apply_patch": {"challenge_id": "c4", "token": "t4"},
            }
        }
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["challenge_id"] == "c4"


def test_approval_for_tool_none_when_absent() -> None:
    state: dict[str, Any] = {}
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is None


# ---------------------------------------------------------------------------
# Executor helpers: _estimate_patch_bytes
# ---------------------------------------------------------------------------


def test_estimate_patch_bytes_from_patch_string() -> None:
    assert _estimate_patch_bytes({"patch": "hello"}) == 5


def test_estimate_patch_bytes_from_changes() -> None:
    changes = [
        {"path": "a.py", "content": "abc"},
        {"path": "b.py", "patch": "def"},
    ]
    assert _estimate_patch_bytes({"changes": changes}) == 6


def test_estimate_patch_bytes_fallback_to_json() -> None:
    payload: dict[str, Any] = {"tool": "exec", "cmd": "ls"}
    result = _estimate_patch_bytes(payload)
    assert result > 0


# ---------------------------------------------------------------------------
# Executor helpers: _configured_write_allowlist
# ---------------------------------------------------------------------------


def test_configured_write_allowlist_from_list() -> None:
    result = _configured_write_allowlist({"allowed_write_paths": ["py/**", "docs/**"]})
    assert result == ("py/**", "docs/**")


def test_configured_write_allowlist_non_list_returns_empty() -> None:
    assert _configured_write_allowlist({"allowed_write_paths": "not_a_list"}) == ()


def test_configured_write_allowlist_empty() -> None:
    assert _configured_write_allowlist({}) == ()


# ---------------------------------------------------------------------------
# Executor helpers: _apply_patch_changed_paths
# ---------------------------------------------------------------------------


def test_apply_patch_changed_paths_valid() -> None:
    changes = [{"path": "src/a.py"}, {"path": "src\\b.py"}]
    result = _apply_patch_changed_paths({"changes": changes})
    assert result == ["src/a.py", "src/b.py"]


def test_apply_patch_changed_paths_non_list() -> None:
    assert _apply_patch_changed_paths({"changes": "not a list"}) is None


def test_apply_patch_changed_paths_empty() -> None:
    assert _apply_patch_changed_paths({"changes": []}) is None


def test_apply_patch_changed_paths_non_dict_entry() -> None:
    assert _apply_patch_changed_paths({"changes": ["bad"]}) is None


def test_apply_patch_changed_paths_missing_path() -> None:
    assert _apply_patch_changed_paths({"changes": [{"content": "x"}]}) is None


# ---------------------------------------------------------------------------
# Executor helpers: _path_matches_allowlist
# ---------------------------------------------------------------------------


def test_path_matches_allowlist_match() -> None:
    assert _path_matches_allowlist("py/main.py", ("py/**",)) is True


def test_path_matches_allowlist_no_match() -> None:
    assert _path_matches_allowlist("rs/main.rs", ("py/**",)) is False


def test_path_matches_allowlist_backslash_normalized() -> None:
    assert _path_matches_allowlist("py\\main.py", ("py/**",)) is True


# ---------------------------------------------------------------------------
# Router: _classify_intent
# ---------------------------------------------------------------------------


def test_classify_intent_code_change() -> None:
    assert _classify_intent("implement a new feature") == "code_change"


def test_classify_intent_debug() -> None:
    assert _classify_intent("debug this error") == "debug"


def test_classify_intent_stack_trace() -> None:
    assert _classify_intent("look at this stack trace") == "debug"


def test_classify_intent_research() -> None:
    assert _classify_intent("research the latest trends") == "research"


def test_classify_intent_question() -> None:
    assert _classify_intent("why does this happen") == "question"
    assert _classify_intent("how does the router work") == "question"
    assert _classify_intent("explain the architecture") == "question"


def test_classify_intent_analysis_fallback() -> None:
    assert _classify_intent("summarize the repository") == "analysis"


# ---------------------------------------------------------------------------
# Router: _default_route
# ---------------------------------------------------------------------------


def test_default_route_recovery_lane() -> None:
    route = _default_route({
        "request": "fix the tests",
        "retry_target": "router",
        "verification": {"failure_class": "test_failure"},
        "repo_context": {"planner_context": {"token_estimate": 100}},
        "facts": [],
        "budgets": {"current_loop": 1},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "recovery"
    assert route.task_class == "test_failure"


def test_default_route_recovery_via_recovery_dict() -> None:
    route = _default_route({
        "request": "try again",
        "verification": {"recovery": {"failure_class": "compile_error", "context_scope": "stable_prefix"}},
        "repo_context": {"planner_context": {"token_estimate": 100}},
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "recovery"
    assert route.prefix_segment == "stable_prefix"


def test_default_route_deep_planning_high_tokens() -> None:
    route = _default_route({
        "request": "summarize the repository",
        "repo_context": {
            "planner_context": {
                "token_estimate": 5000,
                "working_set_token_estimate": 200,
                "compression_pressure": 0,
                "fact_count": 0,
            },
            "compression": {"pressure": {"overall": {"score": 0}}},
        },
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "deep_planning"


def test_default_route_deep_planning_compression_pressure() -> None:
    route = _default_route({
        "request": "summarize",
        "repo_context": {
            "planner_context": {
                "token_estimate": 100,
                "compression_pressure": 5,
            },
            "compression": {"pressure": {"overall": {"score": 5}}},
        },
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "deep_planning"
    assert "compression" in route.rationale


def test_default_route_deep_planning_high_fact_count() -> None:
    route = _default_route({
        "request": "summarize",
        "repo_context": {
            "planner_context": {
                "token_estimate": 100,
                "fact_count": 5,
            },
            "compression": {"pressure": {"overall": {"score": 0}}},
        },
        "facts": ["a", "b", "c", "d", "e"],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "deep_planning"
    assert "recovery memory" in route.rationale


def test_default_route_interactive_with_failure_fingerprint() -> None:
    route = _default_route({
        "request": "summarize",
        "verification": {"failure_fingerprint": "fp123"},
        "repo_context": {
            "planner_context": {"token_estimate": 100},
            "compression": {"pressure": {"overall": {"score": 0}}},
        },
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "interactive"
    assert "failure signal" in route.rationale


def test_default_route_recovery_via_recovery_packet() -> None:
    """When verification.recovery_packet is set, it takes priority."""
    route = _default_route({
        "request": "fix",
        "verification": {
            "recovery_packet": {"failure_class": "runtime_error", "context_scope": "working_set"},
        },
        "repo_context": {"planner_context": {"token_estimate": 100}},
        "facts": [],
        "budgets": {"current_loop": 0},
        "_model_routing_policy": {"interactive_context_limit": 1800, "default_cache_affinity": "workspace"},
    })
    assert route.lane == "recovery"
    assert route.task_class == "runtime_error"
