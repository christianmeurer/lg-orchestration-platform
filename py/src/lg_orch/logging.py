from __future__ import annotations

import os
import sys

import structlog


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
    structlog.configure(
        processors=[
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
