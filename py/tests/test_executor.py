from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from lg_orch.nodes.executor import executor


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
                                "changes": [
                                    {"path": "py/new.txt", "op": "add", "content": "hello"}
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
                                "changes": [
                                    {"path": "py/new.txt", "op": "add", "content": "hello"}
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
