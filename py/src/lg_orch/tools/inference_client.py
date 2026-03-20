from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from lg_orch.model_routing import SlaRoutingPolicy

# ---------------------------------------------------------------------------
# Module-level SLA policy (injected at startup)
# ---------------------------------------------------------------------------

_sla_policy: SlaRoutingPolicy | None = None


def set_sla_policy(policy: SlaRoutingPolicy | None) -> None:
    global _sla_policy
    _sla_policy = policy

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

# ---------------------------------------------------------------------------
# httpx.Client singleton cache — one shared TCP pool per (base_url, api_key)
# ---------------------------------------------------------------------------

_client_cache: dict[tuple[str, str], httpx.Client] = {}
_client_cache_lock = threading.Lock()


def _get_or_create_client(base_url: str, api_key: str, timeout_s: int) -> httpx.Client:
    key = (base_url, api_key)
    with _client_cache_lock:
        if key not in _client_cache:
            headers = {
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            }
            _client_cache[key] = httpx.Client(
                base_url=base_url,
                timeout=float(timeout_s),
                headers=headers,
            )
        return _client_cache[key]


def clear_client_cache() -> None:
    """Close and remove all cached httpx.Client instances (for tests)."""
    with _client_cache_lock:
        for client in _client_cache.values():
            client.close()
        _client_cache.clear()


def _get_breaker(base_url: str) -> _CircuitBreaker:
    with _breakers_lock:
        if base_url not in _breakers:
            _breakers[base_url] = _CircuitBreaker()
        return _breakers[base_url]


# ---------------------------------------------------------------------------
# Function-calling dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]  # parsed from JSON string


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
    tool_calls: list[ToolCall] = field(default_factory=list)


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
            object.__setattr__(
                self,
                "_client",
                _get_or_create_client(self.base_url, self.api_key, self.timeout_s),
            )

    def close(self) -> None:
        # No-op: _client is a shared singleton; use clear_client_cache() to close all.
        pass

    def chat_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 1200,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
    ) -> InferenceResponse:
        policy = _sla_policy
        effective_model = policy.select_model(model) if policy is not None else model

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
                model=effective_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
            )

        # Outer retry for HTTP 429/5xx (up to 4 attempts).
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                result = _do_transport()
                breaker.record_success()
                if policy is not None:
                    policy.record_latency(effective_model, result.latency_ms / 1000.0)
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
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
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
        if tools is not None:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

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

        # Parse tool_calls if present
        parsed_tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls")
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            for tc in raw_tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("id", "")).strip()
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                tc_name = str(fn.get("name", "")).strip()
                tc_args_raw = fn.get("arguments", "{}")
                if isinstance(tc_args_raw, str):
                    try:
                        tc_args = json.loads(tc_args_raw)
                    except (json.JSONDecodeError, ValueError):
                        tc_args = {}
                elif isinstance(tc_args_raw, dict):
                    tc_args = tc_args_raw
                else:
                    tc_args = {}
                if tc_name:
                    parsed_tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=tc_args))

        content = message.get("content")
        if parsed_tool_calls:
            # tool_calls response — content may be absent or null
            text = content if isinstance(content, str) else ""
        else:
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("missing content")
            text = content

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
            text=text,
            latency_ms=latency_ms,
            provider=provider,
            model=model_used,
            usage=usage,
            cache_metadata=cache_metadata,
            headers=headers,
            tool_calls=parsed_tool_calls,
        )


    def chat_completion_stream_sync(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 1200,
    ) -> InferenceResponse:
        """Blocking wrapper around chat_completion_stream for use in sync graph nodes.

        Runs the async SSE stream in a dedicated event loop on a thread pool executor
        so it is safe to call from both sync contexts and threads that may already
        have a running event loop (e.g. LangGraph internal thread).  Falls back to
        chat_completion if streaming fails.
        """
        policy = _sla_policy
        effective_model = policy.select_model(model) if policy is not None else model

        started = time.perf_counter()
        breaker = _get_breaker(self.base_url)
        if not breaker.allow_request():
            raise RuntimeError("circuit_open")

        async def _run() -> str:
            tokens: list[str] = []
            async for token in self.chat_completion_stream(
                model=effective_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                tokens.append(token)
            return "".join(tokens)

        try:
            # Run in a fresh event loop on a background thread to avoid
            # "event loop already running" issues inside LangGraph.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                text = future.result()
            breaker.record_success()
        except Exception:
            breaker.record_failure()
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        if policy is not None:
            policy.record_latency(effective_model, latency_ms / 1000.0)
        return InferenceResponse(
            text=text,
            latency_ms=latency_ms,
            provider="",
            model=effective_model,
            usage=None,
            cache_metadata=None,
            headers=None,
        )

    async def chat_completion_stream(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 1200,
    ) -> AsyncGenerator[str, None]:
        policy = _sla_policy
        effective_model = policy.select_model(model) if policy is not None else model

        breaker = _get_breaker(self.base_url)
        if not breaker.allow_request():
            raise RuntimeError("circuit_open")

        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max(1, int(max_tokens)),
            "stream": True,
        }
        req_headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

        client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(float(self.timeout_s)),
        )
        try:
            @retry(
                reraise=True,
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
                retry=retry_if_exception_type(httpx.TransportError),
            )
            async def _connect() -> httpx.Response:
                return await client.post("/chat/completions", json=payload, headers=req_headers)

            try:
                resp = await _connect()
                resp.raise_for_status()
            except Exception:
                breaker.record_failure()
                raise

            _stream_started = time.perf_counter()
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: "):]
                    if data == "[DONE]":
                        continue
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if isinstance(delta, str) and delta:
                        yield delta
                breaker.record_success()
                if policy is not None:
                    _stream_latency_s = time.perf_counter() - _stream_started
                    policy.record_latency(effective_model, _stream_latency_s)
            except Exception:
                breaker.record_failure()
                raise
        finally:
            await client.aclose()


async def collect_stream(gen: AsyncGenerator[str, None]) -> str:
    """Collect all tokens from an async generator into a single string."""
    parts: list[str] = []
    async for token in gen:
        parts.append(token)
    return "".join(parts)


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
