"""Small in-memory sliding-window rate limiter."""

from __future__ import annotations

import collections
import threading
import time


class SlidingWindowRateLimiter:
    """Simple per-key sliding-window limiter for single-process services."""

    def __init__(self):
        self._events: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        if limit <= 0:
            return True, 0

        now = time.monotonic()
        cutoff = now - window_s

        with self._lock:
            bucket = self._events.setdefault(key, collections.deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_s - now))
                return False, retry_after
            bucket.append(now)
            return True, 0
