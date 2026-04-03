# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Prometheus metrics definitions and /metrics route registration."""

from __future__ import annotations

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Metric objects — defined at module level (single-process, no multiprocess).
# ---------------------------------------------------------------------------
LULA_RUNS_TOTAL: Counter = Counter(
    "lula_runs_total",
    "Total number of completed runs",
    ["lane", "status"],
)
LULA_RUN_DURATION_SECONDS: Histogram = Histogram(
    "lula_run_duration_seconds",
    "Wall-clock duration of runs in seconds",
    ["lane"],
)
LULA_ACTIVE_RUNS: Gauge = Gauge(
    "lula_active_runs",
    "Number of currently active runs",
)
LULA_LLM_REQUESTS_TOTAL: Counter = Counter(
    "lula_llm_requests_total",
    "Total number of LLM requests",
    ["provider", "model", "status"],
)
LULA_LLM_DURATION_SECONDS: Histogram = Histogram(
    "lula_llm_duration_seconds",
    "Wall-clock duration of LLM inference calls in seconds",
    ["model"],
)
LULA_TOOL_CALLS_TOTAL: Counter = Counter(
    "lula_tool_calls_total",
    "Total number of tool calls dispatched to the runner",
    ["tool_name", "status"],
)

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _rate_limiter_metrics_lines() -> str:
    """Return Prometheus text-format lines for the per-client rate limiter.

    Imports the module-level ``_per_client_rate_limiter`` from ``remote_api``
    lazily to avoid circular imports.
    """
    try:
        from lg_orch.remote_api import _per_client_rate_limiter
    except ImportError:
        return ""
    if _per_client_rate_limiter is None:
        return ""
    m = _per_client_rate_limiter.metrics()
    lines = [
        "# HELP rate_limit_requests_total Total requests checked by per-client rate limiter",
        "# TYPE rate_limit_requests_total counter",
        f"rate_limit_requests_total {m['total_requests']}",
        "# HELP rate_limit_rejections_total Total requests rejected by per-client rate limiter",
        "# TYPE rate_limit_rejections_total counter",
        f"rate_limit_rejections_total {m['total_rejections']}",
    ]
    return "\n".join(lines) + "\n"


def handle_metrics(method: str) -> tuple[int, str, bytes]:
    """Return the Prometheus metrics page.

    Returns a (status, content_type, body) triple compatible with the
    ``_api_http_dispatch`` contract.  Only GET is permitted.
    """
    if method != "GET":
        import json

        body = json.dumps({"error": "method_not_allowed"}).encode("utf-8")
        return 405, "application/json; charset=utf-8", body
    body = prometheus_client.generate_latest()
    # Append per-client rate limiter metrics
    rl_lines = _rate_limiter_metrics_lines()
    if rl_lines:
        body = body + rl_lines.encode("utf-8")
    return 200, _PROMETHEUS_CONTENT_TYPE, body
