from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class InferenceResponse:
    text: str
    latency_ms: int
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] | None = None
    cache_metadata: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class InferenceClient:
    base_url: str
    api_key: str
    timeout_s: int = 60
    _client: httpx.Client | None = None

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
        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            retry=retry_if_exception_type(httpx.TransportError),
        )
        def _do() -> InferenceResponse:
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

        return _do()

