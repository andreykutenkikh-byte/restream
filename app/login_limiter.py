"""Small in-memory login throttle for the single-process Stage 1 service."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, identity: str, now: float) -> deque[float]:
        failures = self._failures[identity]
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def allowed(self, identity: str) -> bool:
        return len(self._prune(identity, time.monotonic())) < self.attempts

    def fail(self, identity: str) -> None:
        now = time.monotonic()
        self._prune(identity, now).append(now)

    def success(self, identity: str) -> None:
        self._failures.pop(identity, None)
