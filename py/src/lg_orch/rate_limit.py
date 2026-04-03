# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Token bucket rate limiter for the Remote API."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Token bucket rate limiter.

    Allows `capacity` requests, refilling at `refill_rate` tokens/second.
    """

    capacity: float
    refill_rate: float  # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def acquire(self) -> bool:
        """Try to acquire a token. Returns True if allowed, False if rate-limited."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class RateLimiter:
    """Per-client rate limiter using token buckets."""

    def __init__(self, capacity: float = 60.0, refill_rate: float = 1.0) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._capacity = capacity
        self._refill_rate = refill_rate
        self.total_requests: int = 0
        self.total_rejections: int = 0

    def check(self, client_id: str) -> bool:
        """Check if request from client_id is allowed."""
        self.total_requests += 1
        with self._lock:
            if client_id not in self._buckets:
                self._buckets[client_id] = TokenBucket(self._capacity, self._refill_rate)
        allowed = self._buckets[client_id].acquire()
        if not allowed:
            self.total_rejections += 1
        return allowed

    def metrics(self) -> dict[str, int]:
        """Return current metrics as a dict suitable for Prometheus exposition."""
        with self._lock:
            active = len(self._buckets)
        return {
            "total_requests": self.total_requests,
            "total_rejections": self.total_rejections,
            "active_buckets": active,
        }

    def cleanup(self, max_idle_seconds: float = 3600.0) -> int:
        """Remove idle buckets. Returns number removed."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, b in self._buckets.items() if now - b._last_refill > max_idle_seconds]
            for k in stale:
                del self._buckets[k]
            return len(stale)


__all__ = [
    "RateLimiter",
    "TokenBucket",
]
