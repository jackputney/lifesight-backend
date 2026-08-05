"""In-memory login rate limiter (per process)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# Fail closed under abuse: 5 failures / 15 minutes per key.
MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        q = self._failures[key]
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._failures.pop(key, None)

    def is_blocked(self, key: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._lock:
            self._prune(key, current)
            q = self._failures.get(key)
            return bool(q) and len(q) >= self.max_failures

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self._lock:
            self._prune(key, current)
            self._failures[key].append(current)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


LOGIN_RATE_LIMITER = LoginRateLimiter()
