"""Tests for logging.py uncovered lines and commands/heal.py."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import lg_orch.logging as _logging_mod
from lg_orch.logging import (
    _level_to_int,
    _redact_event_dict,
    configure_logging,
    init_telemetry,
)


# ---------------------------------------------------------------------------
# _redact_event_dict: additional edge cases
# ---------------------------------------------------------------------------


def test_redact_event_dict_bearer_in_string_value() -> None:
    result = _redact_event_dict({"msg": "Got Bearer sk-abc123 from request"})
    assert "sk-abc123" not in str(result["msg"])
    assert "Bearer [REDACTED]" in str(result["msg"])


def test_redact_event_dict_non_string_passthrough() -> None:
    result = _redact_event_dict({"count": 42, "data": [1, 2, 3]})
    assert result["count"] == 42
    assert result["data"] == [1, 2, 3]


def test_redact_event_dict_all_sensitive_keys() -> None:
    from lg_orch.logging import _SENSITIVE_KEYS

    event = {k: f"value-{k}" for k in _SENSITIVE_KEYS}
    result = _redact_event_dict(event)
    for k in _SENSITIVE_KEYS:
        assert result[k] == "[REDACTED]"


def test_redact_event_dict_case_insensitive_keys() -> None:
    result = _redact_event_dict({"API_KEY": "secret", "Token": "abc"})
    assert result["API_KEY"] == "[REDACTED]"
    assert result["Token"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# configure_logging with env var
# ---------------------------------------------------------------------------


def test_configure_logging_with_debug_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_LOG_LEVEL", "DEBUG")
    configure_logging()


def test_configure_logging_with_error_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LG_LOG_LEVEL", "ERROR")
    configure_logging()


# ---------------------------------------------------------------------------
# init_telemetry with unreachable endpoint
# ---------------------------------------------------------------------------


def test_init_telemetry_with_unreachable_endpoint() -> None:
    """init_telemetry should not crash even with an unreachable endpoint."""
    _logging_mod._TELEMETRY_INITIALIZED = False
    try:
        init_telemetry(service_name="test", otlp_endpoint="http://127.0.0.1:1")
    except Exception:
        pytest.fail("init_telemetry should not raise for unreachable endpoint")
    finally:
        _logging_mod._TELEMETRY_INITIALIZED = False


def test_init_telemetry_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_telemetry reads OTEL_EXPORTER_OTLP_ENDPOINT from env when no arg given."""
    _logging_mod._TELEMETRY_INITIALIZED = False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    try:
        init_telemetry(service_name="test", otlp_endpoint=None)
    except Exception:
        pytest.fail("init_telemetry should not raise")
    finally:
        _logging_mod._TELEMETRY_INITIALIZED = False


# ---------------------------------------------------------------------------
# commands/heal.py
# ---------------------------------------------------------------------------


def test_heal_command_invalid_poll_interval() -> None:
    from lg_orch.commands.heal import heal_command

    args = SimpleNamespace(repo_path=None, poll_interval=0.5)
    result = heal_command(args, repo_root=Path("."))
    assert result == 2


def test_heal_command_bad_poll_interval_string() -> None:
    from lg_orch.commands.heal import heal_command

    args = SimpleNamespace(repo_path=None, poll_interval="not_a_number")
    # Should default to 60.0 which is valid, so it will try to run
    # We mock HealingLoop at the source module level
    with patch("lg_orch.healing_loop.HealingLoop") as mock_hl:
        mock_instance = mock_hl.return_value
        mock_instance.run_until_cancelled.side_effect = KeyboardInterrupt
        result = heal_command(args, repo_root=Path("."))
    assert result == 0


def test_heal_command_defaults(tmp_path: Path) -> None:
    from lg_orch.commands.heal import heal_command

    args = SimpleNamespace(repo_path=None, poll_interval=None)
    with patch("lg_orch.healing_loop.HealingLoop") as mock_hl:
        mock_instance = mock_hl.return_value
        mock_instance.run_until_cancelled.side_effect = KeyboardInterrupt
        result = heal_command(args, repo_root=tmp_path)
    assert result == 0
    mock_hl.assert_called_once_with(repo_path=str(tmp_path), poll_interval_seconds=60.0)


def test_heal_command_custom_repo_path(tmp_path: Path) -> None:
    from lg_orch.commands.heal import heal_command

    custom = str(tmp_path / "custom")
    args = SimpleNamespace(repo_path=custom, poll_interval=5.0)
    with patch("lg_orch.healing_loop.HealingLoop") as mock_hl:
        mock_instance = mock_hl.return_value
        mock_instance.run_until_cancelled.side_effect = KeyboardInterrupt
        result = heal_command(args, repo_root=tmp_path)
    assert result == 0
    mock_hl.assert_called_once_with(repo_path=custom, poll_interval_seconds=5.0)
