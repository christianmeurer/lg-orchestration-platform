"""Tests for InferenceClient HTTP 429/5xx retry and circuit-breaker."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import lg_orch.tools.inference_client as ic_mod
from lg_orch.tools.inference_client import (
    InferenceClient,
    _breakers,
    _breakers_lock,
    _CircuitBreaker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(*, status: int, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = httpx.Headers(headers or {})
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "model": "test-model",
        }

    def _raise_for_status() -> None:
        if status >= 400:
            request = MagicMock(spec=httpx.Request)
            raise httpx.HTTPStatusError(
                message=f"HTTP {status}",
                request=request,
                response=resp,
            )

    resp.raise_for_status.side_effect = _raise_for_status
    return resp


def _make_client(base_url: str = "http://test.local") -> InferenceClient:
    mock_http = MagicMock(spec=httpx.Client)
    client = InferenceClient.__new__(InferenceClient)
    object.__setattr__(client, "base_url", base_url)
    object.__setattr__(client, "api_key", "key")
    object.__setattr__(client, "timeout_s", 60)
    object.__setattr__(client, "_client", mock_http)
    return client


def _clear_breaker(base_url: str) -> None:
    with _breakers_lock:
        _breakers.pop(base_url, None)


# ---------------------------------------------------------------------------
# _CircuitBreaker unit tests
# ---------------------------------------------------------------------------


def test_circuit_breaker_starts_closed() -> None:
    cb = _CircuitBreaker()
    assert cb.allow_request() is True


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = _CircuitBreaker()
    for _ in range(5):
        cb.record_failure()
    assert cb.allow_request() is False


def test_circuit_breaker_closed_after_success() -> None:
    cb = _CircuitBreaker()
    for _ in range(5):
        cb.record_failure()
    assert cb.allow_request() is False
    # Simulate half-open by fast-forwarding time
    cb._opened_at = time.monotonic() - 31.0  # type: ignore[attr-defined]
    assert cb.allow_request() is True
    cb.record_success()
    assert cb.allow_request() is True


def test_circuit_breaker_half_open_probe_failure_resets_timer() -> None:
    cb = _CircuitBreaker()
    for _ in range(5):
        cb.record_failure()
    cb._opened_at = time.monotonic() - 31.0  # type: ignore[attr-defined]
    assert cb.allow_request() is True  # enters half_open
    cb.record_failure()  # probe fails
    assert cb.allow_request() is False  # back to open


# ---------------------------------------------------------------------------
# 429 retry with Retry-After header
# ---------------------------------------------------------------------------


def test_429_retries_with_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "http://test-429.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    call_count = 0
    sleep_calls: list[float] = []

    ok_body = {
        "choices": [{"message": {"content": "hello"}}],
        "model": "m",
    }

    def fake_post(path: str, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mock_response(status=429, headers={"retry-after": "2"})
        return _mock_response(status=200, body=ok_body)

    client._client.post.side_effect = fake_post  # type: ignore[union-attr]
    monkeypatch.setattr(ic_mod.time, "sleep", lambda s: sleep_calls.append(s))

    result = client.chat_completion(
        model="m", system_prompt="s", user_prompt="u", temperature=0.0
    )

    assert call_count == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 2.0
    assert result.text == "hello"
    _clear_breaker(base_url)


def test_429_retry_after_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After values outside 1-60 are clamped."""
    from lg_orch.tools.inference_client import _retry_wait_for_http

    resp_low = _mock_response(status=429, headers={"retry-after": "0"})
    exc_low = httpx.HTTPStatusError(message="429", request=MagicMock(), response=resp_low)
    assert _retry_wait_for_http(exc_low, 0) == 1.0

    resp_high = _mock_response(status=429, headers={"retry-after": "999"})
    exc_high = httpx.HTTPStatusError(message="429", request=MagicMock(), response=resp_high)
    assert _retry_wait_for_http(exc_high, 0) == 60.0


# ---------------------------------------------------------------------------
# 5xx retry
# ---------------------------------------------------------------------------


def test_500_retries_up_to_limit_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "http://test-500.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    call_count = 0
    sleep_calls: list[float] = []

    def fake_post(path: str, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_response(status=500)

    client._client.post.side_effect = fake_post  # type: ignore[union-attr]
    monkeypatch.setattr(ic_mod.time, "sleep", lambda s: sleep_calls.append(s))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.chat_completion(
            model="m", system_prompt="s", user_prompt="u", temperature=0.0
        )

    assert exc_info.value.response.status_code == 500
    # 4 total attempts, 3 sleeps between them
    assert call_count == 4
    assert len(sleep_calls) == 3
    _clear_breaker(base_url)


# ---------------------------------------------------------------------------
# Circuit-breaker integration
# ---------------------------------------------------------------------------


def test_5_failures_open_circuit_and_next_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "http://test-cb.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    monkeypatch.setattr(ic_mod.time, "sleep", lambda s: None)

    def always_500(path: str, **kwargs: Any) -> MagicMock:
        return _mock_response(status=500)

    client._client.post.side_effect = always_500  # type: ignore[union-attr]

    # Each chat_completion makes 4 HTTP attempts and records 1 failure on the breaker.
    # We need 5 breaker failures to open it.
    for _ in range(5):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat_completion(
                model="m", system_prompt="s", user_prompt="u", temperature=0.0
            )

    with pytest.raises(RuntimeError, match="circuit_open"):
        client.chat_completion(
            model="m", system_prompt="s", user_prompt="u", temperature=0.0
        )
    _clear_breaker(base_url)


def test_half_open_probe_success_closes_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "http://test-cb-halfopen.local"
    _clear_breaker(base_url)
    client = _make_client(base_url)

    monkeypatch.setattr(ic_mod.time, "sleep", lambda s: None)

    ok_body = {"choices": [{"message": {"content": "hi"}}], "model": "m"}

    # Manually open the circuit breaker
    cb = ic_mod._get_breaker(base_url)
    for _ in range(5):
        cb.record_failure()
    assert not cb.allow_request()

    # Advance time so it enters half_open
    cb._opened_at = time.monotonic() - 31.0  # type: ignore[attr-defined]

    def success_post(path: str, **kwargs: Any) -> MagicMock:
        return _mock_response(status=200, body=ok_body)

    client._client.post.side_effect = success_post  # type: ignore[union-attr]

    result = client.chat_completion(
        model="m", system_prompt="s", user_prompt="u", temperature=0.0
    )
    assert result.text == "hi"
    # Circuit should now be closed
    assert cb.allow_request() is True
    _clear_breaker(base_url)
