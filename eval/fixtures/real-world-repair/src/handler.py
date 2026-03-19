from __future__ import annotations


def compute_rate(events: int, duration_seconds: float) -> float:
    """Return events per second."""
    return events / duration_seconds  # BUG: ZeroDivisionError when duration_seconds == 0


def format_report(events: int, duration_seconds: float) -> str:
    rate = compute_rate(events, duration_seconds)
    return f"{rate:.2f} events/sec"
