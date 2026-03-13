from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Circuit-breaker
# ---------------------------------------------------------------------------

_CB_FAILURE_THRESHOLD = 5
_CB_OPEN_SECONDS = 30.0


class _CircuitBreaker:
    """Per-base-url circuit breaker (closed → open → half_open → closed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: str = "closed"  # closed | open | half_open
        self._failures: int = 0
        self._opened_at: float = 0.0

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() - self._opened_at >= _CB_OPEN_SECONDS:
                    self._state = "half_open"
                    return True
                return False
            # half_open: allow the probe
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                # probe failed → reset open timer
                self._state = "open"
                self._opened_at = time.monotonic()
                return
            self._failures += 1
            if self._failures >= _CB_FAILURE_THRESHOLD:
                self._state = "open"
                self._opened_at = time.monotonic()


_breakers: dict[str, _CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def _get_breaker(base_url: str) -> _CircuitBreaker:
    with _breakers_lock:
        if base_url not in _breakers:
            _breakers[base_url] = _CircuitBreaker()
        return _breakers[base_url]


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceResponse:
    text: str
    latency_ms: int
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] | None = None
    cache_metadata: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceClient:
    base_url: str
    api_key: str
    timeout_s: int = 60
    _client: httpx.Client | None = field(default=None, compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            headers = {
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            }
            object.__setattr__(
                self,
                "_client",
                httpx.Client(base_url=self.base_url, timeout=float(self.timeout_s), headers=headers),
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def chat_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 1200,
    ) -> InferenceResponse:
        breaker = _get_breaker(self.base_url)
        if not breaker.allow_request():
            raise RuntimeError("circuit_open")

        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            retry=retry_if_exception_type(httpx.TransportError),
        )
        def _do_transport() -> InferenceResponse:
            return self._execute_request(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        # Outer retry for HTTP 429/5xx (up to 4 attempts).
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                result = _do_transport()
                breaker.record_success()
                return result
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429 or status >= 500:
                    last_exc = exc
                    if attempt < 3:
                        wait_s = _retry_wait_for_http(exc, attempt)
                        time.sleep(wait_s)
                    else:
                        breaker.record_failure()
                        raise
                else:
                    breaker.record_failure()
                    raise
            except Exception as exc:
                breaker.record_failure()
                raise exc

        # Unreachable, but satisfies the type checker.
        breaker.record_failure()
        assert last_exc is not None
        raise last_exc

    def _execute_request(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> InferenceResponse:
        if self._client is None:
            raise RuntimeError("client not initialized")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max(1, int(max_tokens)),
        }
        started = time.perf_counter()
        resp = self._client.post("/chat/completions", json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("invalid completion payload")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("missing choices in completion payload")
        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("invalid first choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("missing message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("missing content")
        usage_raw = body.get("usage")
        usage = dict(usage_raw) if isinstance(usage_raw, dict) else {}

        headers: dict[str, str] = {}
        cache_metadata: dict[str, Any] = {}
        for key, value in resp.headers.items():
            key_l = key.lower()
            if any(marker in key_l for marker in ("cache", "affinity", "provider", "model", "request-id")):
                headers[key_l] = value
            if any(marker in key_l for marker in ("cache", "affinity", "prefix")):
                cache_metadata[key_l] = value

        provider = str(body.get("provider", "")).strip() or headers.get("x-model-provider", "")
        model_used = str(body.get("model", "")).strip() or model
        return InferenceResponse(
            text=content,
            latency_ms=latency_ms,
            provider=provider,
            model=model_used,
            usage=usage,
            cache_metadata=cache_metadata,
            headers=headers,
        )


def _retry_wait_for_http(exc: httpx.HTTPStatusError, attempt: int) -> float:
    """Return seconds to sleep before next retry."""
    if exc.response.status_code == 429:
        raw = exc.response.headers.get("retry-after", "")
        try:
            parsed = int(raw.strip())
            return float(max(1, min(parsed, 60)))
        except (ValueError, AttributeError):
            pass
        # fallback exponential
        return min(2.0 ** attempt, 30.0)
    # 5xx exponential
    return min(1.0 * (2.0 ** attempt), 30.0)
