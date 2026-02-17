from __future__ import annotations

from lg_orch.logging import _level_to_int, _redact_event_dict, configure_logging, get_logger


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
