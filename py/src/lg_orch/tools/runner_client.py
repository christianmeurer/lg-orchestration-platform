from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import trace as _otel_trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Optional Prometheus metrics — guarded so runner_client works in unit
# tests that do not set up the full app (prometheus_client not registered).
# ---------------------------------------------------------------------------
try:
    from lg_orch.api.metrics import LULA_TOOL_CALLS_TOTAL as _TOOL_CALLS_TOTAL
except ImportError:
    _TOOL_CALLS_TOTAL = None  # type: ignore[assignment]

_W3C_PROPAGATOR = TraceContextTextMapPropagator()


def _traceparent_headers() -> dict[str, str]:
    """Return a ``traceparent`` carrier dict for the active span, or empty dict."""
    carrier: dict[str, str] = {}
    try:
        _W3C_PROPAGATOR.inject(carrier)
    except Exception:  # noqa: BLE001
        pass
    return carrier


@dataclass(frozen=True)
class RunnerClient:
    base_url: str
    api_key: str | None = None
    request_id: str | None = None
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            headers: dict[str, str] = {}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            if self.request_id and self.request_id.strip():
                headers["x-request-id"] = self.request_id.strip()
            object.__setattr__(
                self,
                "_client",
                httpx.Client(base_url=self.base_url, timeout=60.0, headers=headers),
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @staticmethod
    def _checkpoint_payload(input_payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = input_payload.get("_checkpoint")
        if not isinstance(raw, dict):
            return None

        thread_id = raw.get("thread_id")
        checkpoint_ns = raw.get("checkpoint_ns", "")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None

        payload: dict[str, Any] = {
            "thread_id": thread_id.strip(),
            "checkpoint_ns": str(checkpoint_ns),
        }

        checkpoint_id = raw.get("latest_checkpoint_id") or raw.get("resume_checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id.strip():
            payload["checkpoint_id"] = checkpoint_id.strip()

        run_id = raw.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            payload["run_id"] = run_id.strip()

        return payload

    @staticmethod
    def _route_payload(input_payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = input_payload.get("_route")
        if not isinstance(raw, dict):
            return None
        return dict(raw)

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
            payload: dict[str, Any] = {"tool": tool, "input": input}
            checkpoint_payload = self._checkpoint_payload(input)
            if checkpoint_payload is not None:
                payload["checkpoint"] = checkpoint_payload
            route_payload = self._route_payload(input)
            if route_payload is not None:
                payload["route"] = route_payload
            resp = self._client.post(
                "/v1/tools/execute", json=payload, headers=_traceparent_headers()
            )
            resp.raise_for_status()
            return dict(resp.json())

        try:
            result = _do()
            if _TOOL_CALLS_TOTAL is not None:
                _TOOL_CALLS_TOTAL.labels(tool_name=tool, status="ok").inc()
            return result
        except httpx.HTTPStatusError as e:
            if _TOOL_CALLS_TOTAL is not None:
                _TOOL_CALLS_TOTAL.labels(tool_name=tool, status="error").inc()
            status = e.response.status_code if e.response is not None else 0
            route_payload = self._route_payload(input)
            approval_payload: dict[str, Any] | None = None
            if status == 428 and e.response is not None:
                try:
                    body = e.response.json()
                    if isinstance(body, dict) and isinstance(body.get("approval"), dict):
                        approval_payload = dict(body["approval"])
                except Exception:
                    approval_payload = None
            artifacts: dict[str, Any] = {"error": "runner_http_error", "status": status}
            if approval_payload is not None:
                artifacts["error"] = "approval_required"
                artifacts["approval"] = approval_payload
            return {
                "tool": tool,
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"http_status={status} {e}",
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": artifacts,
                **({"route": route_payload} if route_payload is not None else {}),
            }
        except httpx.HTTPError as e:
            if _TOOL_CALLS_TOTAL is not None:
                _TOOL_CALLS_TOTAL.labels(tool_name=tool, status="error").inc()
            route_payload = self._route_payload(input)
            return {
                "tool": tool,
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
                "diagnostics": [],
                "timing_ms": 0,
                "artifacts": {"error": "runner_unavailable"},
                **({"route": route_payload} if route_payload is not None else {}),
            }

    def get_ast_index_summary(
        self,
        *,
        max_files: int = 200,
        path_prefix: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"max_files": max(1, min(int(max_files), 2000))}
        if path_prefix is not None and path_prefix.strip():
            payload["path_prefix"] = path_prefix.strip()

        env = self.execute_tool(tool="ast_index_summary", input=payload)
        if bool(env.get("ok", False)) is not True:
            return {}
        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return {}
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    def search_codebase(
        self,
        *,
        query: str,
        limit: int = 10,
        path_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []

        payload: dict[str, Any] = {"query": q, "limit": max(1, min(int(limit), 50))}
        if path_prefix is not None and path_prefix.strip():
            payload["path_prefix"] = path_prefix.strip()

        env = self.execute_tool(tool="search_codebase", input=payload)
        if bool(env.get("ok", False)) is not True:
            return []
        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return []
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [row for row in parsed if isinstance(row, dict)]

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
            calls_payload: list[dict[str, Any]] = []
            for call in calls:
                input_payload = call.get("input", {})
                request_call: dict[str, Any] = {
                    "tool": str(call.get("tool", "")),
                    "input": dict(input_payload) if isinstance(input_payload, dict) else {},
                }
                checkpoint_payload = self._checkpoint_payload(request_call["input"])
                if checkpoint_payload is not None:
                    request_call["checkpoint"] = checkpoint_payload
                route_payload = self._route_payload(request_call["input"])
                if route_payload is not None:
                    request_call["route"] = route_payload
                calls_payload.append(request_call)

            resp = self._client.post(
                "/v1/tools/batch_execute",
                json={"calls": calls_payload},
                headers=_traceparent_headers(),
            )
            resp.raise_for_status()
            data = dict(resp.json())
            results = data.get("results")
            if not isinstance(results, list):
                raise RuntimeError("invalid batch response")
            return [dict(x) for x in results]

        try:
            results = _do()
            if _TOOL_CALLS_TOTAL is not None:
                for c in calls:
                    _TOOL_CALLS_TOTAL.labels(
                        tool_name=str(c.get("tool", "")), status="ok"
                    ).inc()
            return results
        except httpx.HTTPStatusError as e:
            if _TOOL_CALLS_TOTAL is not None:
                for c in calls:
                    _TOOL_CALLS_TOTAL.labels(
                        tool_name=str(c.get("tool", "")), status="error"
                    ).inc()
            status = e.response.status_code if e.response is not None else 0
            approval_payload: dict[str, Any] | None = None
            if status == 428 and e.response is not None:
                try:
                    body = e.response.json()
                    if isinstance(body, dict) and isinstance(body.get("approval"), dict):
                        approval_payload = dict(body["approval"])
                except Exception:
                    approval_payload = None
            return [
                {
                    "tool": str(c.get("tool", "")),
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"http_status={status} {e}",
                    "diagnostics": [],
                    "timing_ms": 0,
                    "artifacts": {
                        "error": "approval_required"
                        if approval_payload is not None
                        else "runner_http_error",
                        "status": status,
                        **({"approval": approval_payload} if approval_payload is not None else {}),
                    },
                    **(
                        {"route": self._route_payload(c.get("input", {}))}
                        if self._route_payload(c.get("input", {})) is not None
                        else {}
                    ),
                }
                for c in calls
            ]
        except httpx.HTTPError as e:
            if _TOOL_CALLS_TOTAL is not None:
                for c in calls:
                    _TOOL_CALLS_TOTAL.labels(
                        tool_name=str(c.get("tool", "")), status="error"
                    ).inc()
            return [
                {
                    "tool": str(c.get("tool", "")),
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": str(e),
                    "diagnostics": [],
                    "timing_ms": 0,
                    "artifacts": {"error": "runner_unavailable"},
                    **(
                        {"route": self._route_payload(c.get("input", {}))}
                        if self._route_payload(c.get("input", {})) is not None
                        else {}
                    ),
                }
                for c in calls
            ]
