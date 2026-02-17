from __future__ import annotations

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
    expected_keys = {"tool", "ok", "exit_code", "stdout", "stderr", "timing_ms", "artifacts"}
    assert set(results[0].keys()) == expected_keys
