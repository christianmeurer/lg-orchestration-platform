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
    return 200, _PROMETHEUS_CONTENT_TYPE, body
