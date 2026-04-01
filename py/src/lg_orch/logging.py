# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import os
import re
import sys
from typing import Any

import structlog

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "token",
    "password",
    "secret",
    "key",
}

_BEARER_RE = re.compile(r"\bBearer\s+[^\s]+", re.IGNORECASE)

# Guard so init_telemetry is idempotent across multiple calls (e.g. in tests).
_TELEMETRY_INITIALIZED = False


def _redact_event_dict(event_dict: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in event_dict.items():
        lk = str(k).lower()
        if lk in _SENSITIVE_KEYS:
            out[k] = "[REDACTED]"
            continue
        if isinstance(v, str):
            out[k] = _BEARER_RE.sub("Bearer [REDACTED]", v)
        else:
            out[k] = v
    return out


def _level_to_int(level: str) -> int:
    match level.upper():
        case "CRITICAL":
            return 50
        case "ERROR":
            return 40
        case "WARNING" | "WARN":
            return 30
        case "INFO":
            return 20
        case "DEBUG":
            return 10
        case _:
            return 20


def _otel_trace_context_processor(
    _logger: object, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Inject trace_id and span_id from the active OTel span into every log record."""
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is not None and ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:
        pass
    return event_dict


def init_telemetry(
    service_name: str,
    otlp_endpoint: str | None = None,
) -> None:
    """Initialize OpenTelemetry tracing.

    Creates an OTLP gRPC exporter, a BatchSpanProcessor and registers a
    global TracerProvider.  Falls back to a NoOpTracerProvider when
    *otlp_endpoint* is ``None`` or when the exporter fails to initialise.

    The function is idempotent — calling it more than once (e.g. in tests)
    is safe and will not install a second provider.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return
    _TELEMETRY_INITIALIZED = True

    from lg_orch import __version__

    resolved_endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    if not resolved_endpoint:
        # Fall back gracefully: install a no-op provider so downstream code
        # can still call opentelemetry.trace.get_tracer() without crashing.
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.trace import NoOpTracerProvider

            otel_trace.set_tracer_provider(NoOpTracerProvider())
        except Exception:
            pass
        return

    lula_env = os.environ.get("LULA_ENV", "dev")

    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": __version__,
                "deployment.environment": lula_env,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=resolved_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
    except Exception:
        # Fallback on initialization error
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.trace import NoOpTracerProvider

            otel_trace.set_tracer_provider(NoOpTracerProvider())
        except Exception:
            pass


def configure_logging() -> None:
    level = os.environ.get("LG_LOG_LEVEL", "INFO").upper()
    level_int = _level_to_int(level)

    def redact_processor(
        _logger: object, _method: str, event_dict: dict[str, object]
    ) -> dict[str, object]:
        return _redact_event_dict(event_dict)

    structlog.configure(
        processors=[
            redact_processor,  # type: ignore[list-item]
            _otel_trace_context_processor,  # type: ignore[list-item]
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.BoundLogger:
    return structlog.get_logger()  # type: ignore[no-any-return]
