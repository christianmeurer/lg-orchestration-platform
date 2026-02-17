from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class RunnerClient:
    base_url: str
    api_key: str | None = None
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            headers: dict[str, str] = {}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            object.__setattr__(
                self,
                "_client",
                httpx.Client(base_url=self.base_url, timeout=60.0, headers=headers),
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def execute_tool(self, *, tool: str, input: dict[str, Any]) -> dict[str, Any]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            retry=retry_if_exception_type(httpx.TransportError),
        )
        def _do() -> dict[str, Any]:
            if self._client is None:
                raise RuntimeError("client not initialized")
            resp = self._client.post("/v1/tools/execute", json={"tool": tool, "input": input})
            resp.raise_for_status()
            return dict(resp.json())

        try:
            return _do()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            return {
                "tool": tool,
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"http_status={status} {e}",
                "timing_ms": 0,
                "artifacts": {"error": "runner_http_error", "status": status},
            }
        except httpx.HTTPError as e:
            return {
                "tool": tool,
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
                "timing_ms": 0,
                "artifacts": {"error": "runner_unavailable"},
            }

    def batch_execute_tools(self, *, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            retry=retry_if_exception_type(httpx.TransportError),
        )
        def _do() -> list[dict[str, Any]]:
            if self._client is None:
                raise RuntimeError("client not initialized")
            resp = self._client.post("/v1/tools/batch_execute", json={"calls": calls})
            resp.raise_for_status()
            data = dict(resp.json())
            results = data.get("results")
            if not isinstance(results, list):
                raise RuntimeError("invalid batch response")
            return [dict(x) for x in results]

        try:
            return _do()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            return [
                {
                    "tool": str(c.get("tool", "")),
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"http_status={status} {e}",
                    "timing_ms": 0,
                    "artifacts": {"error": "runner_http_error", "status": status},
                }
                for c in calls
            ]
        except httpx.HTTPError as e:
            return [
                {
                    "tool": str(c.get("tool", "")),
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": str(e),
                    "timing_ms": 0,
                    "artifacts": {"error": "runner_unavailable"},
                }
                for c in calls
            ]
