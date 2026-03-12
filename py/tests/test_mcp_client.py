from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lg_orch.tools.mcp_client import MCPClient
from lg_orch.tools.runner_client import RunnerClient


def _runner_mock() -> RunnerClient:
    return RunnerClient(base_url="http://127.0.0.1:8088", _client=MagicMock())


def test_discover_tools_uses_runner_mcp_discover() -> None:
    runner = _runner_mock()
    with patch.object(
        RunnerClient,
        "execute_tool",
        return_value={
            "tool": "mcp_discover",
            "ok": True,
            "stdout": '[{"name":"echo","description":"Echo"}]',
        },
    ) as mocked_execute:
        client = MCPClient(
            runner_client=runner,
            server_configs={"mock": {"command": "python", "args": ["server.py"]}},
        )

        tools = client.discover_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "echo"
        assert tools[0]["server_name"] == "mock"
        call = mocked_execute.call_args
        assert call.kwargs["tool"] == "mcp_discover"


def test_execute_tool_uses_runner_mcp_execute() -> None:
    runner = _runner_mock()
    with patch.object(
        RunnerClient,
        "execute_tool",
        return_value={
            "tool": "mcp_execute",
            "ok": True,
            "stdout": "{}",
            "stderr": "",
            "timing_ms": 1,
            "diagnostics": [],
            "artifacts": {},
            "exit_code": 0,
        },
    ) as mocked_execute:
        client = MCPClient(
            runner_client=runner,
            server_configs={"mock": {"command": "python", "args": ["server.py"]}},
        )

        result = client.execute_tool("mock", "echo", {"x": 1})
        assert result["ok"] is True
        call = mocked_execute.call_args
        assert call.kwargs["tool"] == "mcp_execute"
        assert call.kwargs["input"]["server_name"] == "mock"
        assert call.kwargs["input"]["tool_name"] == "echo"


def test_execute_tool_invalid_server_raises() -> None:
    client = MCPClient(runner_client=_runner_mock(), server_configs={})
    with pytest.raises(ValueError):
        client.execute_tool("unknown", "echo", {})

