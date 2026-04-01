from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from lg_orch.nodes.executor import (
    _apply_patch_changed_paths,
    _approval_for_tool,
    _as_int,
    _budget_failure_result,
    _coerce_approval_token,
    _configured_write_allowlist,
    _estimate_patch_bytes,
    _normalize_rel_path,
    _path_matches_allowlist,
    executor,
)


def _base_state(**overrides: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "request": "test",
        "_runner_enabled": True,
        "_runner_base_url": "http://127.0.0.1:8088",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "tools": [{"tool": "list_files", "input": {"path": "."}}],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
        "tool_results": [],
    }
    s.update(overrides)
    return s


def test_executor_skips_when_runner_disabled() -> None:
    state = _base_state(_runner_enabled=False)
    out = executor(state)
    assert out is state  # returns same object, unchanged


def test_executor_skips_when_plan_not_dict() -> None:
    state = _base_state(plan=None)
    out = executor(state)
    assert out.get("tool_results", []) == []


def test_executor_skips_when_plan_is_string() -> None:
    state = _base_state(plan="not a dict")
    out = executor(state)
    assert out.get("tool_results", []) == []


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_calls_runner(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 10,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance
    out = executor(_base_state())
    assert len(out["tool_results"]) == 1
    assert out["tool_results"][0]["ok"] is True


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_accumulates_results(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance
    state = _base_state(
        tool_results=[
            {
                "tool": "existing",
                "ok": True,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": {},
            }
        ]
    )
    out = executor(state)
    assert len(out["tool_results"]) == 2


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_creates_trace_events(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 5,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance
    out = executor(_base_state())
    events = out.get("_trace_events", [])
    assert any(e["kind"] == "tools" for e in events)
    assert any(e["kind"] == "node" and e["data"].get("name") == "executor" for e in events)


def test_executor_handles_empty_steps() -> None:
    state = _base_state(plan={"steps": [], "verification": [], "rollback": "none"})
    out = executor(state)
    assert out.get("tool_results", []) == []


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_handles_step_with_no_tools(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    state = _base_state(
        plan={
            "steps": [{"id": "step-1", "tools": []}],
            "verification": [],
            "rollback": "none",
        }
    )
    out = executor(state)
    mock_instance.batch_execute_tools.assert_not_called()
    assert out.get("tool_results", []) == []


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_pre_verification_prunes_tool_result_window(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "list_files",
            "ok": True,
            "exit_code": 0,
            "stdout": f"payload-{idx}",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {},
        }
        for idx in range(10)
    ]
    mock_cls.return_value = mock_instance
    state = _base_state(history_policy={"retain_recent_tool_results": 4})
    out = executor(state)
    results = out.get("tool_results", [])
    assert len(results) == 5
    assert results[0]["stdout"] == "payload-5"
    provenance = out.get("provenance", [])
    assert provenance[-1]["event"] == "tool_result_window_trim"


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_blocks_apply_patch_without_approval(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    state = _base_state(
        guards={"require_approval_for_mutations": True},
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {
                                "changes": [{"path": "py/new.txt", "op": "add", "content": "hello"}]
                            },
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
    )

    out = executor(state)

    mock_instance.batch_execute_tools.assert_not_called()
    assert out["tool_results"][0]["artifacts"]["error"] == "approval_required"
    assert out["tool_results"][0]["artifacts"]["approval"]["challenge_id"] == "approval:apply_patch"


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_blocks_apply_patch_outside_allowed_write_paths(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    state = _base_state(
        guards={
            "require_approval_for_mutations": False,
            "allowed_write_paths": ["py/**"],
        },
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {
                                "changes": [
                                    {"path": "docs/new.md", "op": "add", "content": "hello"}
                                ]
                            },
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
    )

    out = executor(state)

    mock_instance.batch_execute_tools.assert_not_called()
    assert out["tool_results"][0]["artifacts"]["error"] == "write_path_not_allowed"
    assert out["tool_results"][0]["artifacts"]["path"] == "docs/new.md"


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_injects_apply_patch_approval_when_present(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "apply_patch",
            "ok": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 1,
            "artifacts": {},
        }
    ]
    mock_cls.return_value = mock_instance
    state = _base_state(
        guards={
            "require_approval_for_mutations": True,
            "allowed_write_paths": ["py/**"],
        },
        approvals={
            "apply_patch": {
                "challenge_id": "approval:apply_patch",
                "token": "approve:approval:apply_patch",
            }
        },
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "apply_patch",
                            "input": {
                                "changes": [{"path": "py/new.txt", "op": "add", "content": "hello"}]
                            },
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        },
    )

    out = executor(state)

    calls = mock_instance.batch_execute_tools.call_args.kwargs["calls"]
    assert calls[0]["input"]["approval"]["token"] == "approve:approval:apply_patch"
    assert out["tool_results"][0]["ok"] is True


@patch("lg_orch.nodes.executor.RunnerClient")
def test_executor_preserves_mcp_response_mapping(mock_cls: MagicMock) -> None:
    mock_instance = MagicMock()
    mock_instance.batch_execute_tools.return_value = [
        {
            "tool": "mcp_execute",
            "ok": True,
            "exit_code": 0,
            "stdout": '{"content":[{"type":"text","text":"ok"}],"isError":false}',
            "stderr": "",
            "diagnostics": [],
            "timing_ms": 9,
            "artifacts": {
                "redaction": {
                    "outbound": {
                        "total": 1,
                        "paths": 1,
                        "usernames": 0,
                        "ip_addresses": 0,
                    },
                    "inbound": {
                        "total": 0,
                        "paths": 0,
                        "usernames": 0,
                        "ip_addresses": 0,
                    },
                }
            },
            "mcp": {
                "server_name": "mock",
                "handshake_completed": True,
                "outbound_redactions": {
                    "total": 1,
                    "paths": 1,
                    "usernames": 0,
                    "ip_addresses": 0,
                },
                "inbound_redactions": {
                    "total": 0,
                    "paths": 0,
                    "usernames": 0,
                    "ip_addresses": 0,
                },
            },
        }
    ]
    mock_cls.return_value = mock_instance

    state = _base_state(
        plan={
            "steps": [
                {
                    "id": "step-1",
                    "tools": [
                        {
                            "tool": "mcp_execute",
                            "input": {"server_name": "mock", "tool_name": "echo", "args": {}},
                        }
                    ],
                }
            ],
            "verification": [],
            "rollback": "none",
        }
    )

    out = executor(state)
    assert len(out["tool_results"]) == 1
    result = out["tool_results"][0]
    assert result["tool"] == "mcp_execute"
    assert result["mcp"]["server_name"] == "mock"
    assert result["artifacts"]["redaction"]["outbound"]["paths"] == 1


# ---------------------------------------------------------------------------
# _coerce_approval_token structural validation
# ---------------------------------------------------------------------------


def test_coerce_approval_token_accepts_legacy_format() -> None:
    result = _coerce_approval_token(
        {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
    )
    assert result is not None
    assert result["challenge_id"] == "approval:apply_patch"
    assert result["token"] == "approve:approval:apply_patch"


def test_coerce_approval_token_accepts_hmac_format() -> None:
    # Dot-separated format (current, matches Rust runner)
    dot_token = (
        "approval:apply_patch.1700000000.abcdef1234567890abcdef1234567890"
        ".deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )
    result = _coerce_approval_token({"challenge_id": "approval:apply_patch", "token": dot_token})
    assert result is not None
    parts = result["token"].split(".")
    assert len(parts) == 4

    # Pipe-separated format (deprecated, still accepted)
    pipe_token = (
        "approval:apply_patch|1700000000|abcdef1234567890abcdef1234567890"
        "|deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )
    result2 = _coerce_approval_token({"challenge_id": "approval:apply_patch", "token": pipe_token})
    assert result2 is not None
    parts2 = result2["token"].split("|")
    assert len(parts2) == 4


def test_coerce_approval_token_rejects_malformed_pipe_count() -> None:
    result = _coerce_approval_token({"challenge_id": "approval:apply_patch", "token": "a|b|c"})
    assert result is None


def test_coerce_approval_token_rejects_hmac_with_empty_field() -> None:
    result = _coerce_approval_token(
        {"challenge_id": "approval:apply_patch", "token": "approval:apply_patch||nonce|sig"}
    )
    assert result is None


def test_coerce_approval_token_rejects_non_dict() -> None:
    assert _coerce_approval_token("bare-string") is None
    assert _coerce_approval_token(None) is None
    assert _coerce_approval_token(42) is None


def test_coerce_approval_token_rejects_missing_fields() -> None:
    assert _coerce_approval_token({"challenge_id": "id"}) is None
    assert _coerce_approval_token({"token": "tok"}) is None
    assert _coerce_approval_token({}) is None


# ---------------------------------------------------------------------------
# _as_int
# ---------------------------------------------------------------------------


def test_as_int_returns_int_directly() -> None:
    assert _as_int(42, default=0) == 42


def test_as_int_returns_default_for_bool() -> None:
    assert _as_int(True, default=7) == 7
    assert _as_int(False, default=7) == 7


def test_as_int_parses_string() -> None:
    assert _as_int("  99  ", default=0) == 99


def test_as_int_returns_default_for_bad_string() -> None:
    assert _as_int("abc", default=5) == 5


def test_as_int_returns_default_for_none() -> None:
    assert _as_int(None, default=3) == 3


def test_as_int_returns_default_for_float() -> None:
    assert _as_int(3.14, default=0) == 0


# ---------------------------------------------------------------------------
# _estimate_patch_bytes
# ---------------------------------------------------------------------------


def test_estimate_patch_bytes_from_patch_string() -> None:
    payload = {"patch": "hello world"}
    assert _estimate_patch_bytes(payload) == len(b"hello world")


def test_estimate_patch_bytes_from_changes_list() -> None:
    payload = {"changes": [{"path": "a.py", "content": "abc"}, {"path": "b.py", "patch": "xyz"}]}
    assert _estimate_patch_bytes(payload) == 6


def test_estimate_patch_bytes_fallback_to_json() -> None:
    payload = {"tool": "apply_patch", "some_key": "value"}
    result = _estimate_patch_bytes(payload)
    assert result > 0


# ---------------------------------------------------------------------------
# _budget_failure_result
# ---------------------------------------------------------------------------


def test_budget_failure_result_basic() -> None:
    result = _budget_failure_result(
        tool="apply_patch",
        message="budget exceeded",
        error_tag="budget_exceeded",
        route_metadata={"lane": "interactive"},
    )
    assert result["tool"] == "apply_patch"
    assert result["ok"] is False
    assert result["stderr"] == "budget exceeded"
    assert result["artifacts"]["error"] == "budget_exceeded"
    assert result["route"] == {"lane": "interactive"}


def test_budget_failure_result_with_extra_artifacts() -> None:
    result = _budget_failure_result(
        tool="apply_patch",
        message="denied",
        error_tag="write_path_not_allowed",
        route_metadata={},
        artifacts_extra={"path": "secret.env"},
    )
    assert result["artifacts"]["path"] == "secret.env"
    assert result["artifacts"]["error"] == "write_path_not_allowed"


# ---------------------------------------------------------------------------
# _normalize_rel_path
# ---------------------------------------------------------------------------


def test_normalize_rel_path_strips_and_converts_backslashes() -> None:
    assert _normalize_rel_path("  py\\src\\main.py  ") == "py/src/main.py"


def test_normalize_rel_path_noop_on_unix_path() -> None:
    assert _normalize_rel_path("py/src/main.py") == "py/src/main.py"


# ---------------------------------------------------------------------------
# _configured_write_allowlist
# ---------------------------------------------------------------------------


def test_configured_write_allowlist_returns_tuple() -> None:
    guards = {"allowed_write_paths": ["py/**", "docs/*.md"]}
    result = _configured_write_allowlist(guards)
    assert result == ("py/**", "docs/*.md")


def test_configured_write_allowlist_empty_when_missing() -> None:
    assert _configured_write_allowlist({}) == ()


def test_configured_write_allowlist_skips_non_strings() -> None:
    guards = {"allowed_write_paths": ["py/**", 42, None, "", "  "]}
    result = _configured_write_allowlist(guards)
    assert result == ("py/**",)


def test_configured_write_allowlist_non_list_returns_empty() -> None:
    assert _configured_write_allowlist({"allowed_write_paths": "py/**"}) == ()


# ---------------------------------------------------------------------------
# _apply_patch_changed_paths
# ---------------------------------------------------------------------------


def test_apply_patch_changed_paths_extracts_paths() -> None:
    payload = {
        "changes": [
            {"path": "py/new.txt", "op": "add", "content": "hello"},
            {"path": "py\\old.txt", "op": "modify", "content": "world"},
        ]
    }
    result = _apply_patch_changed_paths(payload)
    assert result == ["py/new.txt", "py/old.txt"]


def test_apply_patch_changed_paths_returns_none_for_empty() -> None:
    assert _apply_patch_changed_paths({"changes": []}) is None
    assert _apply_patch_changed_paths({}) is None


def test_apply_patch_changed_paths_returns_none_for_non_dict_change() -> None:
    assert _apply_patch_changed_paths({"changes": ["not_a_dict"]}) is None


def test_apply_patch_changed_paths_returns_none_for_missing_path() -> None:
    assert _apply_patch_changed_paths({"changes": [{"op": "add"}]}) is None


# ---------------------------------------------------------------------------
# _path_matches_allowlist
# ---------------------------------------------------------------------------


def test_path_matches_allowlist_glob_match() -> None:
    assert _path_matches_allowlist("py/src/main.py", ("py/**",)) is True


def test_path_matches_allowlist_no_match() -> None:
    assert _path_matches_allowlist("docs/README.md", ("py/**",)) is False


def test_path_matches_allowlist_normalizes_backslashes() -> None:
    assert _path_matches_allowlist("py\\src\\main.py", ("py/**",)) is True


# ---------------------------------------------------------------------------
# _approval_for_tool
# ---------------------------------------------------------------------------


def test_approval_for_tool_from_direct_input() -> None:
    state: dict[str, Any] = {}
    result = _approval_for_tool(
        state,
        tool_name="apply_patch",
        input_payload={
            "approval": {
                "challenge_id": "approval:apply_patch",
                "token": "approve:approval:apply_patch",
            }
        },
    )
    assert result is not None
    assert result["token"] == "approve:approval:apply_patch"


def test_approval_for_tool_from_state_approvals() -> None:
    state: dict[str, Any] = {
        "approvals": {
            "apply_patch": {
                "challenge_id": "cid",
                "token": "tok",
            }
        }
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["token"] == "tok"


def test_approval_for_tool_from_mutations_subkey() -> None:
    state: dict[str, Any] = {
        "approvals": {
            "mutations": {
                "apply_patch": {
                    "challenge_id": "cid",
                    "token": "tok2",
                }
            }
        }
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["token"] == "tok2"


def test_approval_for_tool_returns_none_when_missing() -> None:
    result = _approval_for_tool({}, tool_name="apply_patch", input_payload={})
    assert result is None


def test_approval_for_tool_falls_back_to_resume_approvals() -> None:
    state: dict[str, Any] = {
        "approvals": {},
        "_resume_approvals": {
            "apply_patch": {
                "challenge_id": "cid",
                "token": "resumed",
            }
        },
    }
    result = _approval_for_tool(state, tool_name="apply_patch", input_payload={})
    assert result is not None
    assert result["token"] == "resumed"
