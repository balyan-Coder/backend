"""Small in-process sliding-window rate limiter.

Suitable for a single Uvicorn worker.  A multi-instance deployment should use
Redis or another shared store instead.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.errors import RateLimitError


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                raise RateLimitError(
                    f"Too many requests. Try again in {retry_after} seconds.",
                    retry_after=retry_after,
                )
            bucket.append(now)
            if len(self._events) > 5000:
                empty = [k for k, v in self._events.items() if not v]
                for k in empty[:1000]:
                    self._events.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
