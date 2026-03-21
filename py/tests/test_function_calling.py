"""Tests for Wave 13 (Part B): InferenceClient function calling + JSON schema enforcement."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import jsonschema
import pytest

# Import the modules via sys.modules to avoid the lg_orch.nodes.__init__ attribute shadowing
# (lg_orch.nodes.__init__ exports `planner` and `verifier` as functions, which shadows the modules)
importlib.import_module("lg_orch.nodes.planner")
importlib.import_module("lg_orch.nodes.verifier")
_planner_module = sys.modules["lg_orch.nodes.planner"]
_verifier_module = sys.modules["lg_orch.nodes.verifier"]

from lg_orch.tools.inference_client import (  # noqa: E402
    InferenceClient,
    InferenceResponse,
    ToolCall,
    ToolDefinition,
    _breakers,
    _breakers_lock,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(base_url: str = "http://fc.test.local") -> InferenceClient:
    mock_http = MagicMock(spec=httpx.Client)
    client = InferenceClient.__new__(InferenceClient)
    object.__setattr__(client, "base_url", base_url)
    object.__setattr__(client, "api_key", "key-fc")
    object.__setattr__(client, "timeout_s", 60)
    object.__setattr__(client, "_client", mock_http)
    return client


def _clear_breaker(base_url: str) -> None:
    with _breakers_lock:
        _breakers.pop(base_url, None)


def _mock_ok_response(body: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = httpx.Headers({})
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# 1. tools key present in payload when ToolDefinition list is provided
# ---------------------------------------------------------------------------


def test_chat_completion_includes_tools_in_payload() -> None:
    base_url = "http://fc-tools-payload.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [{"message": {"content": "I'll use the tool."}}],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    tool_def = ToolDefinition(
        name="get_weather",
        description="Returns current weather.",
        parameters={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
        tools=[tool_def],
    )

    call_kwargs = client._client.post.call_args  # type: ignore[union-attr]
    payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert "tools" in payload
    assert len(payload["tools"]) == 1
    assert payload["tools"][0] == {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Returns current weather.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        },
    }


# ---------------------------------------------------------------------------
# 2. tool_calls in JSON response → InferenceResponse.tool_calls populated
# ---------------------------------------------------------------------------


def test_chat_completion_parses_tool_calls_from_response() -> None:
    base_url = "http://fc-parse-tc.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"location": "London"}',
                            },
                        }
                    ],
                }
            }
        ],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    tool_def = ToolDefinition(
        name="get_weather",
        description="Returns current weather.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    response = client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
        tools=[tool_def],
    )

    assert isinstance(response, InferenceResponse)
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "call_abc123"
    assert tc.name == "get_weather"


# ---------------------------------------------------------------------------
# 3. ToolCall.arguments is a parsed dict, not a JSON string
# ---------------------------------------------------------------------------


def test_tool_call_arguments_is_dict() -> None:
    base_url = "http://fc-args-dict.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"query": "python async", "limit": 5}',
                            },
                        }
                    ],
                }
            }
        ],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    tool_def = ToolDefinition(
        name="search",
        description="Search the web.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    response = client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
        tools=[tool_def],
    )

    tc = response.tool_calls[0]
    assert isinstance(tc.arguments, dict), "arguments must be a parsed dict"
    assert tc.arguments == {"query": "python async", "limit": 5}


# ---------------------------------------------------------------------------
# 4. Without tools, payload must not include "tools" key
# ---------------------------------------------------------------------------


def test_chat_completion_no_tools_key_when_none() -> None:
    base_url = "http://fc-no-tools.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [{"message": {"content": "hello"}}],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
    )

    call_kwargs = client._client.post.call_args  # type: ignore[union-attr]
    payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert "tools" not in payload
    assert "tool_choice" not in payload


# ---------------------------------------------------------------------------
# 5. Planner schema validation: valid JSON passes without warning
# ---------------------------------------------------------------------------


def test_planner_schema_real_valid_plan_passes() -> None:
    PLANNER_SCHEMA = getattr(_planner_module, "PLANNER_SCHEMA", {})
    if not PLANNER_SCHEMA:
        pytest.skip("PLANNER_SCHEMA not loaded")
    valid_plan = {
        "steps": [
            {
                "id": "step-1",
                "description": "Collect context.",
                "tools": [{"tool": "list_files", "input": {"path": "."}}],
                "expected_outcome": "Context captured.",
                "files_touched": [],
            }
        ],
        "verification": [],
        "rollback": "No changes made.",
        "acceptance_criteria": ["The task completes successfully."],
        "max_iterations": 3,
    }
    # Must not raise
    jsonschema.validate(instance=valid_plan, schema=PLANNER_SCHEMA)


# ---------------------------------------------------------------------------
# 6. Planner schema validation: invalid JSON logs warning, returns safe default
# ---------------------------------------------------------------------------


def test_planner_schema_invalid_logs_warning_and_returns_safe_default() -> None:
    """When the LLM output fails schema validation, _planner_model_output returns (None, None)."""
    pm = _planner_module
    PLANNER_SCHEMA = getattr(pm, "PLANNER_SCHEMA", {})
    if not PLANNER_SCHEMA:
        pytest.skip("PLANNER_SCHEMA not loaded")

    state: dict[str, Any] = {
        "request": "fix the bug",
        "_models": {
            "planner": {
                "provider": "openai_compatible",
                "model": "gpt-4o",
                "temperature": 0.0,
            }
        },
        "_model_provider_runtime": {
            "openai_compatible": {
                "api_key": "sk-test",
                "base_url": "http://mock-planner.local",
            }
        },
        "_repo_root": ".",
    }

    route_decision = {"provider_used": "openai_compatible", "lane": "deep_planning"}

    # The LLM returns JSON that fails schema (missing required "rollback")
    invalid_response_text = json.dumps(
        {
            "steps": [],
            "verification": [],
            # "rollback" intentionally omitted — fails schema
        }
    )

    mock_response = MagicMock()
    mock_response.text = invalid_response_text

    with patch("lg_orch.nodes.planner.InferenceClient") as MockClient:
        instance = MagicMock()
        instance.chat_completion.return_value = mock_response
        MockClient.return_value = instance

        with patch("lg_orch.nodes.planner.get_logger") as mock_logger_factory:
            mock_log = MagicMock()
            mock_logger_factory.return_value = mock_log

            result_plan, result_response = pm._planner_model_output(  # type: ignore[attr-defined]
                state, route_decision=route_decision
            )

    # Schema validation failure should yield safe default (None, None)
    assert result_plan is None
    assert result_response is None
    # Warning must have been logged
    mock_log.warning.assert_called_once()
    warning_call = mock_log.warning.call_args[0][0]
    assert warning_call == "planner_schema_validation_failed"


# ---------------------------------------------------------------------------
# 7. Verifier schema validation: invalid report logs warning, returns sentinel
# ---------------------------------------------------------------------------


def test_verifier_schema_invalid_report_returns_error_sentinel() -> None:
    vm = _verifier_module
    VERIFIER_SCHEMA = getattr(vm, "VERIFIER_SCHEMA", {})
    if not VERIFIER_SCHEMA:
        pytest.skip("VERIFIER_SCHEMA not loaded")

    state: dict[str, Any] = {
        "tool_results": [],
        "_runner_enabled": False,
        "plan": {
            "acceptance_criteria": [],
            "verification": [],
            "steps": [],
        },
        "budgets": {"current_loop": 0},
    }

    def _bad_validate(instance: Any, schema: Any, **kwargs: Any) -> None:
        raise jsonschema.ValidationError("injected_schema_error")

    with patch("lg_orch.nodes.verifier.jsonschema") as mock_js:
        mock_js.validate.side_effect = _bad_validate
        mock_js.ValidationError = jsonschema.ValidationError

        with patch("lg_orch.nodes.verifier.get_logger") as mock_logger_factory:
            mock_log = MagicMock()
            mock_logger_factory.return_value = mock_log

            out = vm.verifier(state)  # type: ignore[attr-defined]

    verification = out.get("verification", {})
    assert verification.get("ok") is False
    assert "schema_validation_failed" in (
        verification.get("failure_class", "") + verification.get("loop_summary", "")
    )
    mock_log.warning.assert_called()


# ---------------------------------------------------------------------------
# 8. jsonschema.validate called with correct schema args (mock to verify)
# ---------------------------------------------------------------------------


def test_planner_jsonschema_validate_called_with_schema() -> None:
    """Verify jsonschema.validate is invoked with PLANNER_SCHEMA as the schema arg."""
    pm = _planner_module
    PLANNER_SCHEMA = getattr(pm, "PLANNER_SCHEMA", {})
    if not PLANNER_SCHEMA:
        pytest.skip("PLANNER_SCHEMA not loaded")

    state: dict[str, Any] = {
        "request": "fix the bug",
        "_models": {
            "planner": {
                "provider": "openai_compatible",
                "model": "gpt-4o",
                "temperature": 0.0,
            }
        },
        "_model_provider_runtime": {
            "openai_compatible": {
                "api_key": "sk-test",
                "base_url": "http://mock-planner2.local",
            }
        },
        "_repo_root": ".",
    }
    route_decision = {"provider_used": "openai_compatible", "lane": "deep_planning"}

    valid_plan_json = json.dumps(
        {
            "steps": [
                {
                    "id": "step-1",
                    "description": "Collect context.",
                    "tools": [{"tool": "list_files", "input": {"path": "."}}],
                    "expected_outcome": "Context captured.",
                    "files_touched": [],
                }
            ],
            "verification": [],
            "rollback": "No changes.",
        }
    )
    mock_response = MagicMock()
    mock_response.text = valid_plan_json

    with patch("lg_orch.nodes.planner.InferenceClient") as MockClient:
        instance = MagicMock()
        instance.chat_completion.return_value = mock_response
        MockClient.return_value = instance

        with patch("lg_orch.nodes.planner.jsonschema.validate") as mock_validate:
            mock_validate.return_value = None

            pm._planner_model_output(state, route_decision=route_decision)  # type: ignore[attr-defined]

    mock_validate.assert_called_once()
    _, kwargs = mock_validate.call_args
    assert kwargs.get("schema") == PLANNER_SCHEMA


def test_verifier_jsonschema_validate_called_with_schema() -> None:
    """Verify jsonschema.validate is invoked with VERIFIER_SCHEMA as the schema arg."""
    vm = _verifier_module
    VERIFIER_SCHEMA = getattr(vm, "VERIFIER_SCHEMA", {})
    if not VERIFIER_SCHEMA:
        pytest.skip("VERIFIER_SCHEMA not loaded")

    state: dict[str, Any] = {
        "tool_results": [],
        "_runner_enabled": False,
        "plan": {
            "acceptance_criteria": [],
            "verification": [],
            "steps": [],
        },
        "budgets": {"current_loop": 0},
    }

    with patch("lg_orch.nodes.verifier.jsonschema.validate") as mock_validate:
        mock_validate.return_value = None

        vm.verifier(state)  # type: ignore[attr-defined]

    mock_validate.assert_called_once()
    _, kwargs = mock_validate.call_args
    assert kwargs.get("schema") == VERIFIER_SCHEMA


# ---------------------------------------------------------------------------
# 9. tool_choice included in payload when provided
# ---------------------------------------------------------------------------


def test_chat_completion_includes_tool_choice_in_payload() -> None:
    base_url = "http://fc-tool-choice.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [{"message": {"content": "answer"}}],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    tool_def = ToolDefinition(
        name="lookup",
        description="Look something up.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
        tools=[tool_def],
        tool_choice="auto",
    )

    call_kwargs = client._client.post.call_args  # type: ignore[union-attr]
    payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert payload.get("tool_choice") == "auto"


# ---------------------------------------------------------------------------
# 10. InferenceResponse with no tool_calls has empty list by default
# ---------------------------------------------------------------------------


def test_inference_response_tool_calls_default_empty() -> None:
    base_url = "http://fc-default-empty.test.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    body = {
        "choices": [{"message": {"content": "hello world"}}],
        "model": "gpt-4o",
    }
    client._client.post.return_value = _mock_ok_response(body)  # type: ignore[union-attr]

    response = client.chat_completion(
        model="gpt-4o",
        system_prompt="sys",
        user_prompt="user",
        temperature=0.0,
    )

    assert response.tool_calls == []
