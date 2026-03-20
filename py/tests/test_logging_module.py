from __future__ import annotations

import lg_orch.logging as _logging_mod
from lg_orch.logging import (
    _level_to_int,
    _otel_trace_context_processor,
    _redact_event_dict,
    configure_logging,
    get_logger,
    init_telemetry,
)


def test_level_to_int_critical() -> None:
    assert _level_to_int("CRITICAL") == 50
    assert _level_to_int("critical") == 50


def test_level_to_int_error() -> None:
    assert _level_to_int("ERROR") == 40
    assert _level_to_int("error") == 40


def test_level_to_int_warning() -> None:
    assert _level_to_int("WARNING") == 30
    assert _level_to_int("WARN") == 30
    assert _level_to_int("warning") == 30
    assert _level_to_int("warn") == 30


def test_level_to_int_info() -> None:
    assert _level_to_int("INFO") == 20
    assert _level_to_int("info") == 20


def test_level_to_int_debug() -> None:
    assert _level_to_int("DEBUG") == 10
    assert _level_to_int("debug") == 10


def test_level_to_int_unknown_defaults_to_info() -> None:
    assert _level_to_int("UNKNOWN") == 20
    assert _level_to_int("") == 20


def test_configure_logging_does_not_raise() -> None:
    configure_logging()


def test_get_logger_returns_bound_logger() -> None:
    configure_logging()
    log = get_logger()
    assert log is not None


def test_logging_redacts_api_key() -> None:
    redacted = _redact_event_dict({"api_key": "supersecret", "authorization": "Bearer abc"})
    assert redacted["api_key"] == "[REDACTED]"
    assert "abc" not in str(redacted["authorization"])


# ---------------------------------------------------------------------------
# OTel tests
# ---------------------------------------------------------------------------


def _reset_telemetry_init_flag() -> None:
    """Reset the idempotency guard so tests can call init_telemetry independently."""
    _logging_mod._TELEMETRY_INITIALIZED = False  # type: ignore[attr-defined]


def test_init_telemetry_noop_when_no_endpoint() -> None:
    """init_telemetry with otlp_endpoint=None must not raise and must install
    a provider (falling back to NoOp) without connecting to any network."""
    _reset_telemetry_init_flag()
    # Should complete without raising, even with no reachable OTLP endpoint.
    init_telemetry(service_name="test-service", otlp_endpoint=None)

    # The global tracer provider must now be set (either real SDK or NoOp).
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    assert provider is not None


def test_init_telemetry_is_idempotent() -> None:
    """Calling init_telemetry twice must not raise or install a second provider."""
    _reset_telemetry_init_flag()
    init_telemetry(service_name="svc-a", otlp_endpoint=None)
    init_telemetry(service_name="svc-b", otlp_endpoint=None)
    # Still alive — no exception.


def test_trace_id_in_structlog_context_no_active_span() -> None:
    """When there is no active span the processor must not raise and must not
    inject trace_id / span_id (or inject zeros — both are acceptable)."""
    event: dict[str, object] = {"event": "hello"}
    result = _otel_trace_context_processor(None, "info", event)
    # Must still return a dict with the original key.
    assert "event" in result


def test_trace_id_in_structlog_context_with_mock_span() -> None:
    """When a valid span is active the processor must append trace_id and
    span_id formatted as lowercase hex strings."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    # Install a simple in-process SDK provider so we get real span contexts.
    _reset_telemetry_init_flag()
    provider = TracerProvider()
    otel_trace.set_tracer_provider(provider)
    _logging_mod._TELEMETRY_INITIALIZED = True  # type: ignore[attr-defined]

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        ctx = span.get_span_context()
        assert ctx is not None and ctx.is_valid, "expected a valid span context from SDK provider"

        event: dict[str, object] = {"event": "inside span"}
        result = _otel_trace_context_processor(None, "info", event)

        assert "trace_id" in result, "trace_id must be injected by the processor"
        assert "span_id" in result, "span_id must be injected by the processor"

        expected_trace_id = format(ctx.trace_id, "032x")
        expected_span_id = format(ctx.span_id, "016x")
        assert result["trace_id"] == expected_trace_id
        assert result["span_id"] == expected_span_id
