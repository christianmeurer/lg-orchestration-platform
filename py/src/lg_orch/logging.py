from __future__ import annotations

import os
import re
import sys

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
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.BoundLogger:
    return structlog.get_logger()  # type: ignore[no-any-return]
