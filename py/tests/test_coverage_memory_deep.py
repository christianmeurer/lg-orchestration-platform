"""Deeper tests for memory.py helpers: summarize_tool_result, _truncate_text_to_budget,
_fit_segments, dedupe_semantic_hits, context_budget_settings, ensure_history_policy,
prune_pre_verification_history.
"""
from __future__ import annotations

from typing import Any

import pytest

from lg_orch.memory import (
    _first_nonempty_line,
    _fit_segments,
    _safe_json,
    _truncate_text_to_budget,
    approx_token_count,
    context_budget_settings,
    dedupe_semantic_hits,
    ensure_history_policy,
    prune_pre_verification_history,
    summarize_tool_result,
)


# ---------------------------------------------------------------------------
# _safe_json
# ---------------------------------------------------------------------------


def test_safe_json_dict() -> None:
    result = _safe_json({"a": 1})
    assert '"a"' in result


def test_safe_json_non_serializable() -> None:
    result = _safe_json(object())
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _first_nonempty_line
# ---------------------------------------------------------------------------


def test_first_nonempty_line_blank() -> None:
    assert _first_nonempty_line("") == ""
    assert _first_nonempty_line("\n\n") == ""


def test_first_nonempty_line_skips_blank() -> None:
    assert _first_nonempty_line("\n  \nhello\nworld") == "hello"


# ---------------------------------------------------------------------------
# dedupe_semantic_hits
# ---------------------------------------------------------------------------


def test_dedupe_semantic_hits_empty() -> None:
    assert dedupe_semantic_hits([]) == []


def test_dedupe_semantic_hits_dedupes_by_path() -> None:
    hits = [
        {"path": "a.py", "snippet": "code", "score": 0.5},
        {"path": "a.py", "snippet": "other", "score": 0.9},
        {"path": "b.py", "snippet": "code", "score": 0.3},
    ]
    result = dedupe_semantic_hits(hits)
    paths = [h["path"] for h in result]
    assert paths.count("a.py") == 1
    # Higher score wins
    a_hit = next(h for h in result if h["path"] == "a.py")
    assert a_hit["score"] == 0.9


def test_dedupe_semantic_hits_skips_empty_key() -> None:
    hits = [
        {"path": "", "snippet": "", "score": 0.5},
        {"path": "a.py", "snippet": "code", "score": 0.3},
    ]
    result = dedupe_semantic_hits(hits)
    assert len(result) == 1
    assert result[0]["path"] == "a.py"


def test_dedupe_semantic_hits_sorted_by_score() -> None:
    hits = [
        {"path": "a.py", "score": 0.3},
        {"path": "b.py", "score": 0.9},
        {"path": "c.py", "score": 0.6},
    ]
    result = dedupe_semantic_hits(hits)
    scores = [h["score"] for h in result]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# summarize_tool_result
# ---------------------------------------------------------------------------


def test_summarize_tool_result_basic() -> None:
    result = summarize_tool_result(
        {"tool": "exec", "ok": True, "exit_code": 0, "stdout": "output line\n"},
        max_chars=100,
    )
    assert result["tool"] == "exec"
    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_summarize_tool_result_from_diagnostics() -> None:
    result = summarize_tool_result(
        {
            "tool": "exec",
            "ok": False,
            "diagnostics": [{"message": "compilation failed"}],
            "stderr": "other error",
        },
        max_chars=100,
    )
    assert "compilation failed" in result["summary"]


def test_summarize_tool_result_from_stderr() -> None:
    result = summarize_tool_result(
        {"tool": "exec", "ok": False, "stderr": "error occurred"},
        max_chars=100,
    )
    assert "error occurred" in result["summary"]


def test_summarize_tool_result_truncates() -> None:
    result = summarize_tool_result(
        {"tool": "exec", "ok": False, "stderr": "x" * 500},
        max_chars=50,
    )
    assert len(result["summary"]) <= 50
    assert result["summary"].endswith("...")


def test_summarize_tool_result_with_error_artifact() -> None:
    result = summarize_tool_result(
        {"tool": "exec", "ok": False, "artifacts": {"error": "approval_required"}},
        max_chars=100,
    )
    assert result["error"] == "approval_required"


# ---------------------------------------------------------------------------
# _truncate_text_to_budget
# ---------------------------------------------------------------------------


def test_truncate_text_fits() -> None:
    text, truncated = _truncate_text_to_budget("hello", budget_tokens=100)
    assert text == "hello"
    assert truncated is False


def test_truncate_text_zero_budget() -> None:
    text, truncated = _truncate_text_to_budget("hello", budget_tokens=0)
    assert text == ""
    assert truncated is True


def test_truncate_text_small_budget() -> None:
    text, truncated = _truncate_text_to_budget("x" * 500, budget_tokens=5)
    assert truncated is True
    assert len(text) <= 20


def test_truncate_text_large_text() -> None:
    big_text = "line " * 10000
    text, truncated = _truncate_text_to_budget(big_text, budget_tokens=100)
    assert truncated is True
    assert "...[compressed]..." in text


# ---------------------------------------------------------------------------
# _fit_segments
# ---------------------------------------------------------------------------


def test_fit_segments_all_fit() -> None:
    segments = [("repo_map", "short text"), ("facts", "few facts")]
    combined, decisions = _fit_segments(segments, budget_tokens=1000)
    assert "repo_map" in combined
    assert "facts" in combined
    assert len(decisions) == 0  # no compression needed


def test_fit_segments_drops_when_exhausted() -> None:
    segments = [
        ("big", "x" * 10000),
        ("small", "tiny"),
    ]
    combined, decisions = _fit_segments(segments, budget_tokens=10)
    # At least one segment should be dropped or compressed
    actions = [d["action"] for d in decisions]
    assert "dropped" in actions or "compressed" in actions


def test_fit_segments_empty_segments_skipped() -> None:
    segments = [("empty", "  "), ("valid", "content")]
    combined, decisions = _fit_segments(segments, budget_tokens=100)
    assert "valid" in combined


# ---------------------------------------------------------------------------
# context_budget_settings
# ---------------------------------------------------------------------------


def test_context_budget_settings_defaults() -> None:
    result = context_budget_settings({})
    assert result["stable_prefix_tokens"] >= 200
    assert result["working_set_tokens"] >= 200
    assert result["tool_result_summary_chars"] >= 80


def test_context_budget_settings_custom() -> None:
    result = context_budget_settings({
        "_budget_context": {
            "stable_prefix_tokens": 500,
            "working_set_tokens": 800,
            "tool_result_summary_chars": 200,
        }
    })
    assert result["stable_prefix_tokens"] == 500
    assert result["working_set_tokens"] == 800
    assert result["tool_result_summary_chars"] == 200


# ---------------------------------------------------------------------------
# ensure_history_policy
# ---------------------------------------------------------------------------


def test_ensure_history_policy_adds_default() -> None:
    state: dict[str, Any] = {}
    result = ensure_history_policy(state)
    assert "history_policy" in result
    hp = result["history_policy"]
    assert "schema_version" in hp
    assert hp["retain_recent_tool_results"] >= 5


def test_ensure_history_policy_preserves_existing() -> None:
    state: dict[str, Any] = {
        "history_policy": {"schema_version": 1, "retain_recent_tool_results": 100, "read_file_prune_threshold_chars": 5000}
    }
    result = ensure_history_policy(state)
    assert result["history_policy"]["retain_recent_tool_results"] == 100


# ---------------------------------------------------------------------------
# prune_pre_verification_history
# ---------------------------------------------------------------------------


def test_prune_pre_verification_history_basic() -> None:
    state: dict[str, Any] = {
        "tool_results": [
            {"tool": "exec", "ok": True},
            {"tool": "read_file", "ok": True, "stdout": "x" * 10000},
            {"tool": "exec", "ok": True},
        ],
        "_history_policy": {
            "schema_version": 1,
            "retain_recent_tool_results": 2,
            "read_file_prune_threshold_chars": 100,
        },
    }
    result = prune_pre_verification_history(state)
    # Should retain at least the most recent 2
    assert len(result.get("tool_results", [])) >= 2


def test_prune_pre_verification_no_results() -> None:
    state: dict[str, Any] = {"tool_results": []}
    result = prune_pre_verification_history(state)
    assert result.get("tool_results") == []
