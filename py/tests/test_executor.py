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
