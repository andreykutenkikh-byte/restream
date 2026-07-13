"""Thread-safe bitrate sampling for the cumulative MediaMTX ingest counter."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from app.services.mediamtx import IngestStatus


class IngestBitrateSampler:
    """Derive a smoothed bitrate from cumulative bytes and monotonic time.

    The sampler deliberately keeps no history beyond the current stream
    baseline and EMA. A changed path or ready timestamp identifies a new
    publisher session and therefore starts with an unknown bitrate.
    """

    def __init__(
        self,
        *,
        ema_alpha: float = 0.35,
        stale_after_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(ema_alpha) or not 0 < ema_alpha <= 1:
            raise ValueError("ema_alpha must be between zero and one")
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._ema_alpha = ema_alpha
        self._stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._lock = Lock()
        self._stream_identity: tuple[str, datetime | None] | None = None
        self._bytes_received: int | None = None
        self._observed_at: float | None = None
        self._ema_bps: float | None = None

    def _reset_unlocked(self) -> None:
        self._stream_identity = None
        self._bytes_received = None
        self._observed_at = None
        self._ema_bps = None

    def _set_baseline_unlocked(
        self,
        identity: tuple[str, datetime | None],
        bytes_received: int,
        observed_at: float,
    ) -> None:
        self._stream_identity = identity
        self._bytes_received = bytes_received
        self._observed_at = observed_at
        self._ema_bps = None

    def reset(self) -> None:
        """Forget the active publisher and any smoothed value."""

        with self._lock:
            self._reset_unlocked()

    def sample(self, *, stream_id: str, status: IngestStatus) -> int | None:
        """Return the current smoothed bitrate or ``None`` without a baseline."""

        with self._lock:
            observed_at = self._clock()
            bytes_received = status.bytes_received
            if (
                not math.isfinite(observed_at)
                or not status.is_available
                or isinstance(bytes_received, bool)
                or bytes_received is None
                or bytes_received < 0
            ):
                self._reset_unlocked()
                return None

            identity = (stream_id, status.since)
            if (
                identity != self._stream_identity
                or self._bytes_received is None
                or self._observed_at is None
            ):
                self._set_baseline_unlocked(identity, bytes_received, observed_at)
                return None

            elapsed = observed_at - self._observed_at
            if elapsed <= 0:
                return round(self._ema_bps) if self._ema_bps is not None else None

            if elapsed >= self._stale_after_seconds or bytes_received < self._bytes_received:
                self._set_baseline_unlocked(identity, bytes_received, observed_at)
                return None

            raw_bps = ((bytes_received - self._bytes_received) * 8) / elapsed
            if self._ema_bps is None:
                self._ema_bps = raw_bps
            else:
                self._ema_bps = self._ema_alpha * raw_bps + (1 - self._ema_alpha) * self._ema_bps
            self._bytes_received = bytes_received
            self._observed_at = observed_at
            return round(self._ema_bps)


__all__ = ["IngestBitrateSampler"]
