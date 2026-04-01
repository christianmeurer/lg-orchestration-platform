from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import lg_orch.tools.runner_client as rc_mod
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


def test_client_sets_request_id_header() -> None:
    client = RunnerClient(base_url="http://127.0.0.1:8088", api_key="token", request_id="req-1")
    try:
        assert client._client is not None
        assert client._client.headers["x-request-id"] == "req-1"
        assert client._client.headers["authorization"] == "Bearer token"
    finally:
        client.close()


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
            '[{"path":"py/a.py","language":"python","symbols":["a"],"snippet":"def a","score":0.1}]'
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


# ---------------------------------------------------------------------------
# Prometheus metric instrumentation tests
# ---------------------------------------------------------------------------


def test_tool_calls_total_incremented_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LULA_TOOL_CALLS_TOTAL is incremented with status='ok' on successful execute_tool."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "tool": "exec",
        "ok": True,
        "exit_code": 0,
        "stdout": "done",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp
    client = RunnerClient(base_url="http://127.0.0.1:8088", _client=mock_http)

    mock_counter = MagicMock()
    monkeypatch.setattr(rc_mod, "_TOOL_CALLS_TOTAL", mock_counter)

    result = client.execute_tool(tool="exec", input={"cmd": "ls"})
    assert result["ok"] is True

    mock_counter.labels.assert_called_once_with(tool_name="exec", status="ok")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_tool_calls_total_incremented_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LULA_TOOL_CALLS_TOTAL is incremented with status='error' on HTTPError."""
    client = RunnerClient(base_url="http://127.0.0.1:0")

    mock_counter = MagicMock()
    monkeypatch.setattr(rc_mod, "_TOOL_CALLS_TOTAL", mock_counter)

    result = client.execute_tool(tool="health", input={})
    assert result["ok"] is False

    label_calls = mock_counter.labels.call_args_list
    assert any(call.kwargs.get("status") == "error" for call in label_calls)


def test_tool_calls_total_none_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _TOOL_CALLS_TOTAL is None (ImportError guard), no AttributeError is raised."""
    client = RunnerClient(base_url="http://127.0.0.1:0")
    monkeypatch.setattr(rc_mod, "_TOOL_CALLS_TOTAL", None)

    result = client.execute_tool(tool="health", input={})
    assert result["ok"] is False  # network unreachable, but no crash from None counter


# ---------------------------------------------------------------------------
# _checkpoint_payload / _route_payload static method tests
# ---------------------------------------------------------------------------


def test_checkpoint_payload_returns_none_for_missing_key() -> None:
    assert RunnerClient._checkpoint_payload({}) is None


def test_checkpoint_payload_returns_none_for_non_dict() -> None:
    assert RunnerClient._checkpoint_payload({"_checkpoint": "not_a_dict"}) is None


def test_checkpoint_payload_returns_none_for_missing_thread_id() -> None:
    assert RunnerClient._checkpoint_payload({"_checkpoint": {"checkpoint_ns": "x"}}) is None


def test_checkpoint_payload_returns_none_for_empty_thread_id() -> None:
    assert RunnerClient._checkpoint_payload({"_checkpoint": {"thread_id": "  "}}) is None


def test_checkpoint_payload_minimal() -> None:
    result = RunnerClient._checkpoint_payload({"_checkpoint": {"thread_id": "t1"}})
    assert result is not None
    assert result["thread_id"] == "t1"
    assert result["checkpoint_ns"] == ""
    assert "checkpoint_id" not in result
    assert "run_id" not in result


def test_checkpoint_payload_with_resume_checkpoint_id() -> None:
    result = RunnerClient._checkpoint_payload(
        {"_checkpoint": {"thread_id": "t1", "resume_checkpoint_id": "cp-2"}}
    )
    assert result is not None
    assert result["checkpoint_id"] == "cp-2"


def test_checkpoint_payload_with_run_id() -> None:
    result = RunnerClient._checkpoint_payload(
        {"_checkpoint": {"thread_id": "t1", "run_id": "run-1"}}
    )
    assert result is not None
    assert result["run_id"] == "run-1"


def test_route_payload_returns_none_for_missing_key() -> None:
    assert RunnerClient._route_payload({}) is None


def test_route_payload_returns_none_for_non_dict() -> None:
    assert RunnerClient._route_payload({"_route": "string"}) is None


def test_route_payload_returns_dict() -> None:
    result = RunnerClient._route_payload({"_route": {"lane": "recovery"}})
    assert result == {"lane": "recovery"}


# ---------------------------------------------------------------------------
# RunnerClient read/search helpers
# ---------------------------------------------------------------------------


@patch("lg_orch.tools.runner_client.RunnerClient.execute_tool")
def test_search_codebase_returns_empty_on_failure(mock_execute: MagicMock) -> None:
    mock_execute.return_value = {"ok": False, "stderr": "error"}
    client = RunnerClient(base_url="http://127.0.0.1:8088")
    out = client.search_codebase(query="test")
    assert out == []


@patch("lg_orch.tools.runner_client.RunnerClient.execute_tool")
def test_get_ast_index_summary_returns_empty_on_failure(mock_execute: MagicMock) -> None:
    mock_execute.return_value = {"ok": False, "stderr": "error"}
    client = RunnerClient(base_url="http://127.0.0.1:8088")
    out = client.get_ast_index_summary()
    assert out == {}


def test_execute_tool_forwards_route_payload() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "tool": "exec",
        "ok": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 1,
        "artifacts": {},
    }
    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp
    client = RunnerClient(base_url="http://127.0.0.1:8088", _client=mock_http)

    client.execute_tool(tool="exec", input={"_route": {"lane": "recovery"}})
    call_kwargs = mock_http.post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["route"] == {"lane": "recovery"}
