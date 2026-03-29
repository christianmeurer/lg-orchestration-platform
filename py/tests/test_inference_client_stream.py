"""Tests for InferenceClient.chat_completion_stream and collect_stream."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lg_orch.tools.inference_client import InferenceClient, _breakers, collect_stream

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_lines(*chunks: str) -> list[str]:
    lines: list[str] = []
    for chunk in chunks:
        payload = {"choices": [{"delta": {"content": chunk}}]}
        lines.append(f"data: {json.dumps(payload)}")
    lines.append("data: [DONE]")
    return lines


def _make_client() -> InferenceClient:
    return InferenceClient(base_url="http://test.local", api_key="test-key")


def _clear_breaker(base_url: str) -> None:
    _breakers.pop(base_url, None)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _mock_send(lines: list[str]) -> AsyncMock:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200

    async def _aiter_lines():
        for line in lines:
            yield line

    mock_resp.aiter_lines = _aiter_lines
    return AsyncMock(return_value=mock_resp)


# ---------------------------------------------------------------------------
# Test: basic streaming yields tokens
# ---------------------------------------------------------------------------


def test_chat_completion_stream_yields_tokens() -> None:
    _clear_breaker("http://test.local")
    client = _make_client()
    mock = _mock_send(_sse_lines("Hello", ", ", "world", "!"))

    async def _go() -> list[str]:
        tokens: list[str] = []
        with patch("httpx.AsyncClient.send", mock):
            async for token in client.chat_completion_stream(
                model="gpt-4o",
                system_prompt="sys",
                user_prompt="hi",
                temperature=0.0,
            ):
                tokens.append(token)
        return tokens

    assert _run(_go()) == ["Hello", ", ", "world", "!"]


# ---------------------------------------------------------------------------
# Test: [DONE] and non-data lines are skipped
# ---------------------------------------------------------------------------


def test_chat_completion_stream_skips_non_data_lines() -> None:
    _clear_breaker("http://test.local")
    client = _make_client()
    chunk_payload = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    raw_lines = [
        ": keep-alive",
        "",
        f"data: {chunk_payload}",
        "data: [DONE]",
    ]
    mock = _mock_send(raw_lines)

    async def _go() -> list[str]:
        tokens: list[str] = []
        with patch("httpx.AsyncClient.send", mock):
            async for token in client.chat_completion_stream(
                model="gpt-4o",
                system_prompt="sys",
                user_prompt="hi",
                temperature=0.0,
            ):
                tokens.append(token)
        return tokens

    assert _run(_go()) == ["hi"]


# ---------------------------------------------------------------------------
# Test: collect_stream concatenates all tokens
# ---------------------------------------------------------------------------


def test_collect_stream_concatenates() -> None:
    async def _gen():
        for t in ["foo", " ", "bar"]:
            yield t

    assert _run(collect_stream(_gen())) == "foo bar"


def test_collect_stream_empty() -> None:
    async def _gen():
        return
        yield  # make it an async generator

    assert _run(collect_stream(_gen())) == ""


# ---------------------------------------------------------------------------
# Test: circuit-open raises before making a request
# ---------------------------------------------------------------------------


def test_circuit_open_raises_before_request() -> None:
    base_url = "http://cb-test.local"
    _clear_breaker(base_url)

    from lg_orch.tools.inference_client import _get_breaker

    breaker = _get_breaker(base_url)
    for _ in range(5):
        breaker.record_failure()

    client = InferenceClient(base_url=base_url, api_key="key")
    mock = AsyncMock()

    async def _go() -> None:
        with patch("httpx.AsyncClient.send", mock):
            async for _ in client.chat_completion_stream(
                model="gpt-4o",
                system_prompt="sys",
                user_prompt="hi",
                temperature=0.0,
            ):
                pass

    with pytest.raises(RuntimeError, match="circuit_open"):
        _run(_go())

    mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test: empty delta content is not yielded
# ---------------------------------------------------------------------------


def test_empty_delta_content_not_yielded() -> None:
    _clear_breaker("http://test.local")
    client = _make_client()
    lines = [
        f"data: {json.dumps({'choices': [{'delta': {'content': ''}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'ok'}}]})}",
        "data: [DONE]",
    ]
    mock = _mock_send(lines)

    async def _go() -> list[str]:
        tokens: list[str] = []
        with patch("httpx.AsyncClient.send", mock):
            async for token in client.chat_completion_stream(
                model="gpt-4o",
                system_prompt="sys",
                user_prompt="hi",
                temperature=0.0,
            ):
                tokens.append(token)
        return tokens

    assert _run(_go()) == ["ok"]
