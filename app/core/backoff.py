"""Bounded exponential retry scheduling for restream workers."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field

RandomSource = Callable[[], float]


class BackoffExhausted(RuntimeError):
    """Raised when the configured number of fast retry attempts is exhausted."""


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return converted


def calculate_backoff(
    attempt: int,
    *,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    multiplier: float = 2.0,
) -> float:
    """Calculate a capped exponential delay for a zero-based ``attempt``.

    The implementation avoids exponentiating once the cap is reached, so even a
    very large attempt number cannot overflow.
    """

    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise TypeError("attempt must be an integer")
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    initial = _positive_finite(initial_delay, "initial_delay")
    maximum = _positive_finite(max_delay, "max_delay")
    factor = _positive_finite(multiplier, "multiplier")
    if initial > maximum:
        raise ValueError("initial_delay must not exceed max_delay")
    if factor <= 1:
        raise ValueError("multiplier must be greater than 1")
    if initial == maximum:
        return maximum

    attempts_until_cap = math.ceil(math.log(maximum / initial, factor))
    if attempt >= attempts_until_cap:
        return maximum
    return min(maximum, initial * factor**attempt)


# A descriptive alias is useful at call sites and backwards-compatible with
# code that names the calculation after the policy rather than the operation.
exponential_backoff = calculate_backoff


@dataclass(slots=True)
class ExponentialBackoff:
    """Stateful bounded retry policy with optional jitter and exhaustion.

    ``next_delay`` records one failed attempt.  ``record_success`` only resets
    the sequence when the connection was alive for at least
    ``stable_reset_after`` seconds; brief reconnects therefore cannot create a
    tight infinite loop.  Once ``max_attempts`` is reached, the caller should
    place the worker in ``failed`` until a manual restart calls ``reset``.
    """

    initial_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    max_attempts: int | None = 8
    stable_reset_after: float = 30.0
    jitter_ratio: float = 0.0
    random_source: RandomSource = field(default=random.random, repr=False, compare=False)
    _attempts: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.initial_delay = _positive_finite(self.initial_delay, "initial_delay")
        self.max_delay = _positive_finite(self.max_delay, "max_delay")
        self.multiplier = _positive_finite(self.multiplier, "multiplier")
        if self.initial_delay > self.max_delay:
            raise ValueError("initial_delay must not exceed max_delay")
        if self.multiplier <= 1:
            raise ValueError("multiplier must be greater than 1")
        if self.max_attempts is not None:
            if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
                raise TypeError("max_attempts must be an integer or None")
            if self.max_attempts < 1:
                raise ValueError("max_attempts must be at least 1")
        if isinstance(self.stable_reset_after, bool) or not isinstance(
            self.stable_reset_after, (int, float)
        ):
            raise TypeError("stable_reset_after must be a number")
        self.stable_reset_after = float(self.stable_reset_after)
        if not math.isfinite(self.stable_reset_after) or self.stable_reset_after < 0:
            raise ValueError("stable_reset_after must be a non-negative finite number")
        if isinstance(self.jitter_ratio, bool) or not isinstance(self.jitter_ratio, (int, float)):
            raise TypeError("jitter_ratio must be a number")
        self.jitter_ratio = float(self.jitter_ratio)
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        if not callable(self.random_source):
            raise TypeError("random_source must be callable")

    @property
    def attempts(self) -> int:
        """Number of failures recorded since the last stable reset."""

        return self._attempts

    @property
    def exhausted(self) -> bool:
        """Whether another automatic retry is forbidden by ``max_attempts``."""

        return self.max_attempts is not None and self._attempts >= self.max_attempts

    @property
    def remaining_attempts(self) -> int | None:
        """Automatic attempts remaining, or ``None`` for an unlimited policy."""

        if self.max_attempts is None:
            return None
        return max(0, self.max_attempts - self._attempts)

    def next_delay(self) -> float:
        """Record a failure and return the delay before the next retry."""

        if self.exhausted:
            raise BackoffExhausted(f"retry limit reached after {self._attempts} failed attempts")
        delay = calculate_backoff(
            self._attempts,
            initial_delay=self.initial_delay,
            max_delay=self.max_delay,
            multiplier=self.multiplier,
        )
        self._attempts += 1
        if not self.jitter_ratio:
            return delay

        sample = self.random_source()
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise TypeError("random_source must return a number between 0 and 1")
        sample = float(sample)
        if not math.isfinite(sample) or not 0 <= sample <= 1:
            raise ValueError("random_source must return a number between 0 and 1")
        low = delay * (1 - self.jitter_ratio)
        high = delay * (1 + self.jitter_ratio)
        return min(self.max_delay, low + (high - low) * sample)

    # A worker naturally describes this event as registering a failure.
    record_failure = next_delay

    def record_success(self, connected_for: float) -> bool:
        """Reset after a stable connection and report whether a reset occurred."""

        if isinstance(connected_for, bool) or not isinstance(connected_for, (int, float)):
            raise TypeError("connected_for must be a number")
        duration = float(connected_for)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("connected_for must be a non-negative finite number")
        if duration < self.stable_reset_after:
            return False
        self.reset()
        return True

    def reset(self) -> None:
        """Clear retry history, normally after stability or a manual restart."""

        self._attempts = 0


__all__ = [
    "BackoffExhausted",
    "ExponentialBackoff",
    "calculate_backoff",
    "exponential_backoff",
]
