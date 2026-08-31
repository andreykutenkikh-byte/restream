"""Independent in-memory throttle for administrator step-up checks."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock


class StepUpRateLimiter:
    """Bound failed password checks per opaque session-and-client identity."""

    def __init__(
        self,
        *,
        attempts: int = 5,
        window_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if attempts < 1 or window_seconds < 1:
            raise ValueError("step-up rate limits must be positive")
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _prune(self, identity: str, now: float) -> deque[float]:
        failures = self._failures[identity]
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(identity, None)
            # Return an unregistered deque; fail() registers it if necessary.
            return deque()
        return failures

    def retry_after(self, identity: str) -> int | None:
        """Return a whole-second retry delay, or ``None`` when allowed."""

        with self._lock:
            now = self._clock()
            failures = self._prune(identity, now)
            if len(failures) < self.attempts:
                return None
            return max(1, math.ceil(failures[0] + self.window_seconds - now))

    def fail(self, identity: str) -> None:
        with self._lock:
            now = self._clock()
            failures = self._prune(identity, now)
            failures.append(now)
            self._failures[identity] = failures

    def success(self, identity: str) -> None:
        """Reset only the successfully verified session-and-client identity."""

        with self._lock:
            self._failures.pop(identity, None)


__all__ = ["StepUpRateLimiter"]
