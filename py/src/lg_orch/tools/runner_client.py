from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class RunnerClient:
    base_url: str
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            object.__setattr__(
                self,
                "_client",
                httpx.Client(base_url=self.base_url, timeout=60.0),
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def execute_tool(self, *, tool: str, input: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is None:
                raise RuntimeError("client not initialized")
            resp = self._client.post("/v1/tools/execute", json={"tool": tool, "input": input})
            resp.raise_for_status()
            return dict(resp.json())
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
        try:
            if self._client is None:
                raise RuntimeError("client not initialized")
            resp = self._client.post("/v1/tools/batch_execute", json={"calls": calls})
            resp.raise_for_status()
            data = dict(resp.json())
            results = data.get("results")
            if not isinstance(results, list):
                raise RuntimeError("invalid batch response")
            return [dict(x) for x in results]
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
