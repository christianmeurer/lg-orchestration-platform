from __future__ import annotations

from lg_orch.memory import (
    build_context_layers,
    ensure_history_policy,
    prune_post_verification_history,
    prune_pre_verification_history,
)


def _mk_result(tool: str, stdout: str, *, ok: bool = True) -> dict[str, object]:
    return {
        "tool": tool,
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "stdout": stdout,
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 0,
        "artifacts": {"path": "py/sample.py"} if tool == "read_file" else {},
    }


def test_ensure_history_policy_defaults() -> None:
    out = ensure_history_policy({"request": "x"})
    policy = out["history_policy"]
    assert policy["schema_version"] == 1
    assert policy["retain_recent_tool_results"] == 40
    assert policy["read_file_prune_threshold_chars"] == 4000


def test_prune_pre_verification_trims_tool_window_and_records_provenance() -> None:
    tool_results = [_mk_result("search_files", f"line-{idx}") for idx in range(12)]
    out = prune_pre_verification_history(
        {
            "request": "x",
            "history_policy": {"retain_recent_tool_results": 5},
            "tool_results": tool_results,
            "provenance": [],
        }
    )
    trimmed = out["tool_results"]
    assert len(trimmed) == 5
    assert trimmed[0]["stdout"] == "line-7"
    assert out["provenance"][-1]["event"] == "tool_result_window_trim"


def test_prune_post_verification_evicts_large_read_payload_after_apply_patch_verification() -> None:
    large = "a" * 6000
    out = prune_post_verification_history(
        {
            "request": "x",
            "history_policy": {
                "retain_recent_tool_results": 40,
                "read_file_prune_threshold_chars": 5000,
            },
            "verification": {"ok": True},
            "tool_results": [
                _mk_result("read_file", large),
                _mk_result("apply_patch", "ok", ok=True),
            ],
            "provenance": [],
        }
    )
    pruned_stdout = out["tool_results"][0]["stdout"]
    assert isinstance(pruned_stdout, str)
    assert pruned_stdout.startswith("[pruned_read_file_payload]")
    artifacts = out["tool_results"][0]["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts["pruned"]["stdout_chars"] == 6000
    assert out["provenance"][-1]["event"] == "read_file_payload_evicted"


def test_prune_post_verification_skips_without_successful_apply_patch() -> None:
    large = "b" * 6000
    state = {
        "request": "x",
        "history_policy": {"read_file_prune_threshold_chars": 5000},
        "verification": {"ok": True},
        "tool_results": [_mk_result("read_file", large)],
        "provenance": [],
    }
    out = prune_post_verification_history(state)
    assert out["tool_results"][0]["stdout"] == large
    assert out["provenance"] == []


def test_build_context_layers_emits_recovery_fact_pack_and_pressure() -> None:
    layers = build_context_layers(
        state={
            "facts": [
                {
                    "kind": "recovery_fact",
                    "loop": 2,
                    "failure_class": "verification_failed",
                    "failure_fingerprint": "fp-1",
                    "summary": "verification_failed: test assertion failed",
                    "last_check": "test assertion failed",
                    "salience": 8,
                }
            ],
            "recovery_packet": {
                "failure_class": "verification_failed",
                "failure_fingerprint": "fp-1",
                "summary": "verification_failed: test assertion failed",
                "last_check": "test assertion failed",
                "context_scope": "working_set",
                "plan_action": "keep",
                "retry_target": "planner",
            },
            "_budget_context": {
                "stable_prefix_tokens": 220,
                "working_set_tokens": 220,
                "tool_result_summary_chars": 160,
            },
        },
        repo_context={
            "repo_root": ".",
            "has_py": True,
            "has_rs": False,
            "top_level": ["py", "README.md"],
            "repo_map": "py\nREADME.md",
            "semantic_hits": [],
        },
    )
    assert "recovery_fact_pack" in layers["working_set"]["content"]
    assert layers["planner_context"]["fact_count"] == 1
    assert "pressure" in layers["compression"]
    assert isinstance(layers["compression"]["pressure"]["overall"]["score"], int)


def test_build_context_layers_counts_semantic_memories_in_fact_count() -> None:
    layers = build_context_layers(
        state={
            "facts": [],
            "_budget_context": {
                "stable_prefix_tokens": 240,
                "working_set_tokens": 220,
                "tool_result_summary_chars": 160,
            },
        },
        repo_context={
            "repo_root": ".",
            "has_py": True,
            "has_rs": False,
            "top_level": ["py", "README.md"],
            "repo_map": "py\nREADME.md",
            "semantic_hits": [],
            "semantic_memories": [
                {"kind": "approval_history", "summary": "approved apply patch"},
                {"kind": "loop_summary", "summary": "previous repair succeeded"},
            ],
        },
    )
    assert "semantic_memories" in layers["stable_prefix"]["content"]
    assert layers["planner_context"]["semantic_memory_count"] == 2
    assert layers["planner_context"]["fact_count"] == 2
