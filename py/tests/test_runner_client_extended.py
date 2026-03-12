from __future__ import annotations

from unittest.mock import MagicMock, patch

from lg_orch.tools.runner_client import RunnerClient


def test_single_execute_when_runner_unavailable() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    result = client.execute_tool(tool="health", input={})
    assert result["ok"] is False
    assert result["tool"] == "health"
    assert result["exit_code"] == 1
    assert result["artifacts"]["error"] in {"runner_unavailable", "runner_http_error"}
    assert result["timing_ms"] == 0
    assert isinstance(result["stderr"], str)
    assert len(result["stderr"]) > 0


def test_batch_execute_returns_correct_count() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    calls = [
        {"tool": "a", "input": {}},
        {"tool": "b", "input": {}},
        {"tool": "c", "input": {}},
    ]
    results = client.batch_execute_tools(calls=calls)
    assert len(results) == 3


def test_batch_execute_preserves_tool_names() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    calls = [
        {"tool": "read_file", "input": {"path": "x"}},
        {"tool": "exec", "input": {"cmd": "python"}},
    ]
    results = client.batch_execute_tools(calls=calls)
    assert results[0]["tool"] == "read_file"
    assert results[1]["tool"] == "exec"


def test_client_close_is_idempotent() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    client.close()
    client.close()  # should not raise


def test_batch_envelope_keys() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:0")
    results = client.batch_execute_tools(calls=[{"tool": "t", "input": {}}])
    expected_keys = {
        "tool",
        "ok",
        "exit_code",
        "stdout",
        "stderr",
        "diagnostics",
        "timing_ms",
        "artifacts",
    }
    assert set(results[0].keys()) == expected_keys


@patch("lg_orch.tools.runner_client.RunnerClient.execute_tool")
def test_get_ast_index_summary_parses_payload(mock_execute: MagicMock) -> None:
    mock_execute.return_value = {
        "tool": "ast_index_summary",
        "ok": True,
        "stdout": '{"schema_version":1,"version":2,"files":[]}',
    }
    client = RunnerClient(base_url="http://127.0.0.1:8088")
    out = client.get_ast_index_summary(max_files=120)
    assert out["schema_version"] == 1
    assert out["version"] == 2
    mock_execute.assert_called_once()


@patch("lg_orch.tools.runner_client.RunnerClient.execute_tool")
def test_search_codebase_parses_hit_list(mock_execute: MagicMock) -> None:
    mock_execute.return_value = {
        "tool": "search_codebase",
        "ok": True,
        "stdout": (
            '[{"path":"py/a.py","language":"python","symbols":["a"],'
            '"snippet":"def a","score":0.1}]'
        ),
    }
    client = RunnerClient(base_url="http://127.0.0.1:8088")
    out = client.search_codebase(query="memory context", limit=5)
    assert len(out) == 1
    assert out[0]["path"] == "py/a.py"
    mock_execute.assert_called_once()


@patch("lg_orch.tools.runner_client.RunnerClient.execute_tool")
def test_search_codebase_empty_query_short_circuits(mock_execute: MagicMock) -> None:
    client = RunnerClient(base_url="http://127.0.0.1:8088")
    out = client.search_codebase(query="   ")
    assert out == []
    mock_execute.assert_not_called()


def test_execute_tool_forwards_checkpoint_payload() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "tool": "exec",
        "ok": True,
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 1,
        "artifacts": {},
    }

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp
    client = RunnerClient(base_url="http://127.0.0.1:8088", _client=mock_http)

    result = client.execute_tool(
        tool="exec",
        input={
            "cmd": "git",
            "args": ["status"],
            "_checkpoint": {
                "thread_id": "thread-1",
                "checkpoint_ns": "main",
                "latest_checkpoint_id": "cp-1",
                "run_id": "run-1",
            },
        },
    )
    assert result["ok"] is True
    call_kwargs = mock_http.post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["checkpoint"]["thread_id"] == "thread-1"
    assert payload["checkpoint"]["checkpoint_ns"] == "main"
    assert payload["checkpoint"]["checkpoint_id"] == "cp-1"
    assert payload["checkpoint"]["run_id"] == "run-1"
