"""In-memory sliding-window rate limiter, per user and action.

Protects the endpoints that cost money or open outbound connections (questions,
connection tests) from bursts. State lives in the process: with several workers each one
counts separately, so the effective limit is the configured one times the worker count.
A shared store (Redis) would be needed for an exact limit across workers.
"""
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record a hit for `key` and return False if it exceeds the limit in the window."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        self._prune(now)
        return True

    def reset(self) -> None:
        """Forget all hits (tests)."""
        self._hits.clear()

    def _prune(self, now: float) -> None:
        """Drop keys with no recent hits so memory stays bounded by active users."""
        if len(self._hits) < 1_000:
            return
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - self.window_s]
        for key in stale:
            del self._hits[key]
