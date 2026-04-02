# Wave 18 coverage tests — runner_client HTTP paths, router model output path
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from lg_orch.nodes.router import _router_model_output, router
from lg_orch.tools.runner_client import RunnerClient

# ---------------------------------------------------------------------------
# RunnerClient — execute_tool HTTP paths
# ---------------------------------------------------------------------------


def test_runner_client_execute_tool_success() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "list_files",
        "ok": True,
        "exit_code": 0,
        "stdout": "[]",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.execute_tool(tool="list_files", input={"path": "."})
    assert result["ok"] is True
    assert result["tool"] == "list_files"


def test_runner_client_execute_tool_with_route_and_checkpoint() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"tool": "exec", "ok": True}
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.execute_tool(
        tool="exec",
        input={
            "cmd": "ls",
            "_route": {"lane": "interactive"},
            "_checkpoint": {
                "thread_id": "t-1",
                "checkpoint_ns": "ns",
            },
        },
    )
    assert result["ok"] is True
    # Verify that the payload included route and checkpoint
    call_args = mock_http.post.call_args
    payload = call_args.kwargs.get("json") or call_args[1].get("json")
    assert "route" in payload
    assert "checkpoint" in payload


def test_runner_client_execute_tool_http_status_error() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 500
    error = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=mock_response)

    mock_http = MagicMock()
    mock_http.post.side_effect = error

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.execute_tool(tool="exec", input={"cmd": "ls"})
    assert result["ok"] is False
    assert result["artifacts"]["error"] == "runner_http_error"
    assert result["artifacts"]["status"] == 500


def test_runner_client_execute_tool_428_approval() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 428
    mock_response.json.return_value = {
        "approval": {
            "challenge_id": "approval:apply_patch",
            "required": True,
        }
    }
    error = httpx.HTTPStatusError(
        "Precondition Required", request=MagicMock(), response=mock_response
    )

    mock_http = MagicMock()
    mock_http.post.side_effect = error

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.execute_tool(tool="apply_patch", input={})
    assert result["ok"] is False
    assert result["artifacts"]["error"] == "approval_required"
    assert result["artifacts"]["approval"]["challenge_id"] == "approval:apply_patch"


def test_runner_client_execute_tool_http_error() -> None:
    mock_http = MagicMock()
    mock_http.post.side_effect = httpx.ConnectError("connection refused")

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.execute_tool(tool="exec", input={})
    assert result["ok"] is False
    assert result["artifacts"]["error"] == "runner_unavailable"


def test_runner_client_get_ast_index_summary_success() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "ast_index_summary",
        "ok": True,
        "exit_code": 0,
        "stdout": '{"files": 10}',
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.get_ast_index_summary(max_files=50, path_prefix="py/")
    assert result == {"files": 10}


def test_runner_client_get_ast_index_summary_not_ok() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"tool": "ast_index_summary", "ok": False}
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    result = client.get_ast_index_summary()
    assert result == {}


def test_runner_client_search_codebase_success() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "search_codebase",
        "ok": True,
        "exit_code": 0,
        "stdout": '[{"path": "a.py", "score": 0.9}]',
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    results = client.search_codebase(query="test", limit=5, path_prefix="py/")
    assert len(results) == 1
    assert results[0]["path"] == "a.py"


def test_runner_client_search_codebase_empty_query() -> None:
    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", MagicMock())

    assert client.search_codebase(query="  ") == []


def test_runner_client_batch_execute_tools_http_error() -> None:
    mock_http = MagicMock()
    mock_http.post.side_effect = httpx.ConnectError("connection refused")

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    results = client.batch_execute_tools(calls=[{"tool": "exec", "input": {"cmd": "ls"}}])
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["artifacts"]["error"] == "runner_unavailable"


def test_runner_client_batch_execute_428() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 428
    mock_response.json.return_value = {
        "approval": {"challenge_id": "approval:apply_patch", "required": True}
    }
    error = httpx.HTTPStatusError(
        "Precondition Required", request=MagicMock(), response=mock_response
    )

    mock_http = MagicMock()
    mock_http.post.side_effect = error

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    results = client.batch_execute_tools(calls=[{"tool": "apply_patch", "input": {}}])
    assert len(results) == 1
    assert results[0]["artifacts"]["error"] == "approval_required"


# ---------------------------------------------------------------------------
# Router — _router_model_output path
# ---------------------------------------------------------------------------


def _minimal_router_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request": "test",
        "repo_context": {},
        "facts": [],
        "budgets": {},
        "_models": {
            "router": {
                "provider": "remote",
                "model": "test-model",
                "temperature": 0.0,
            }
        },
        "_model_routing_policy": {
            "interactive_context_limit": 1800,
            "default_cache_affinity": "workspace",
        },
    }
    base.update(overrides)
    return base


def test_router_model_output_local_provider() -> None:
    """When provider_used is local, _router_model_output returns None."""
    from lg_orch.nodes.router import _default_route

    state = _minimal_router_state()
    default_route = _default_route(state)
    result, response = _router_model_output(
        state,
        default_route=default_route,
        route_decision={"provider_used": "local"},
    )
    assert result is None
    assert response is None


def test_router_model_output_no_inference_client() -> None:
    """When resolve_inference_client fails, returns None."""
    from lg_orch.nodes.router import _default_route

    state = _minimal_router_state()
    default_route = _default_route(state)
    with patch(
        "lg_orch.nodes.router.resolve_inference_client",
        side_effect=ValueError("no client"),
    ):
        result, response = _router_model_output(
            state,
            default_route=default_route,
            route_decision={"provider_used": "remote"},
        )
    assert result is None
    assert response is None


def test_router_model_output_success() -> None:
    """When inference succeeds, returns a parsed RouterDecision."""
    from lg_orch.nodes.router import _default_route

    state = _minimal_router_state()
    default_route = _default_route(state)

    json_text = (
        '{"intent": "analysis", "task_class": "analysis", '
        '"lane": "interactive", "rationale": "test", '
        '"context_scope": "stable_prefix", "latency_sensitive": true, '
        '"cache_affinity": "workspace", "prefix_segment": "stable_prefix", '
        '"context_tokens": 0, "compression_pressure": 0, '
        '"fact_count": 0}'
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json_text
    # Both streaming and non-streaming paths should return the response
    mock_client.chat_completion.return_value = mock_response
    mock_client.chat_completion_stream_sync.return_value = mock_response

    with patch(
        "lg_orch.nodes.router.resolve_inference_client",
        return_value=(mock_client, "test-model"),
    ):
        result, _response = _router_model_output(
            state,
            default_route=default_route,
            route_decision={"provider_used": "remote", "lane": "deep_planning"},
        )
    assert result is not None
    assert result.intent == "analysis"
    mock_client.close.assert_called_once()


def test_router_falls_back_on_model_exception() -> None:
    """When model output fails, router uses the default route."""
    state = _minimal_router_state()
    with patch(
        "lg_orch.nodes.router._router_model_output",
        side_effect=ValueError("model failure"),
    ):
        out = router(state)
    # Should still return a route (the default)
    assert "route" in out
    assert out["route"]["lane"] in {"interactive", "deep_planning", "recovery"}


def test_runner_client_batch_execute_http_status_500() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.json.side_effect = Exception("no body")
    error = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=mock_response)

    mock_http = MagicMock()
    mock_http.post.side_effect = error

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    results = client.batch_execute_tools(
        calls=[
            {"tool": "exec", "input": {"cmd": "ls"}},
            {"tool": "read_file", "input": {"path": "a.py"}},
        ]
    )
    assert len(results) == 2
    assert all(r["ok"] is False for r in results)
    assert results[0]["artifacts"]["error"] == "runner_http_error"


def test_runner_client_get_ast_index_bad_json() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "ast_index_summary",
        "ok": True,
        "exit_code": 0,
        "stdout": "not json",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.get_ast_index_summary() == {}


def test_runner_client_search_codebase_bad_json() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "search_codebase",
        "ok": True,
        "exit_code": 0,
        "stdout": "not json",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.search_codebase(query="test") == []


def test_runner_client_search_codebase_not_list() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "search_codebase",
        "ok": True,
        "exit_code": 0,
        "stdout": '{"not": "a list"}',
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.search_codebase(query="test") == []


def test_runner_client_batch_execute_success() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {"tool": "list_files", "ok": True, "exit_code": 0},
            {"tool": "read_file", "ok": True, "exit_code": 0},
        ]
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    results = client.batch_execute_tools(
        calls=[
            {
                "tool": "list_files",
                "input": {
                    "path": ".",
                    "_route": {"lane": "interactive"},
                    "_checkpoint": {
                        "thread_id": "t-1",
                        "checkpoint_ns": "ns",
                    },
                },
            },
            {"tool": "read_file", "input": {"path": "a.py"}},
        ]
    )
    assert len(results) == 2
    assert all(r["ok"] is True for r in results)

    # Verify the batch payload included route and checkpoint
    call_args = mock_http.post.call_args
    payload = call_args.kwargs.get("json") or call_args[1].get("json")
    first_call = payload["calls"][0]
    assert "route" in first_call
    assert "checkpoint" in first_call


def test_runner_client_get_ast_empty_stdout() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "ast_index_summary",
        "ok": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.get_ast_index_summary() == {}


def test_runner_client_get_ast_returns_list_not_dict() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "ast_index_summary",
        "ok": True,
        "exit_code": 0,
        "stdout": "[1,2,3]",
        "stderr": "",
        "diagnostics": [],
        "timing_ms": 5,
        "artifacts": {},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.get_ast_index_summary() == {}


def test_router_model_output_interactive_stream() -> None:
    """When lane is interactive, router tries streaming first."""
    from lg_orch.nodes.router import _default_route

    json_text = (
        '{"intent": "analysis", "task_class": "analysis", '
        '"lane": "interactive", "rationale": "test", '
        '"context_scope": "stable_prefix", "latency_sensitive": true, '
        '"cache_affinity": "workspace", "prefix_segment": "stable_prefix", '
        '"context_tokens": 0, "compression_pressure": 0, '
        '"fact_count": 0}'
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json_text
    mock_client.chat_completion_stream_sync.return_value = mock_response

    state = _minimal_router_state()
    default_route = _default_route(state)

    with patch(
        "lg_orch.nodes.router.resolve_inference_client",
        return_value=(mock_client, "test-model"),
    ):
        result, _response = _router_model_output(
            state,
            default_route=default_route,
            route_decision={"provider_used": "remote"},
        )
    assert result is not None
    mock_client.chat_completion_stream_sync.assert_called_once()
    mock_client.close.assert_called_once()


def test_runner_client_init_with_headers() -> None:
    """RunnerClient should set auth and request-id headers."""
    with patch("lg_orch.tools.runner_client.httpx.Client") as mock_client:
        RunnerClient(
            base_url="http://localhost:8088",
            api_key="test-key",
            request_id="req-123",
        )
        call_kwargs = mock_client.call_args.kwargs
        assert call_kwargs["headers"]["authorization"] == "Bearer test-key"
        assert call_kwargs["headers"]["x-request-id"] == "req-123"


def test_runner_client_search_not_ok() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"tool": "search_codebase", "ok": False}
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.search_codebase(query="test") == []


def test_runner_client_search_empty_stdout() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "tool": "search_codebase",
        "ok": True,
        "stdout": "",
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    client = RunnerClient.__new__(RunnerClient)
    object.__setattr__(client, "base_url", "http://localhost:8088")
    object.__setattr__(client, "api_key", None)
    object.__setattr__(client, "request_id", None)
    object.__setattr__(client, "_client", mock_http)

    assert client.search_codebase(query="test") == []
