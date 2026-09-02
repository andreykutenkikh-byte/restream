"""Bounded, in-memory MPEG-TS preview cache for the remote HK relay.

The relay agent supplies only opaque MPEG-TS bytes plus a generation and sequence
number.  This service owns every browser-visible URL and playlist line, and never
persists media or agent-provided metadata.
"""

from __future__ import annotations

import hmac
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic

MAX_SEGMENT_BYTES = 3 * 1024 * 1024
MAX_SEGMENTS_PER_NODE = 4
MAX_NODE_BYTES = 12 * 1024 * 1024
MAX_GLOBAL_BYTES = 24 * 1024 * 1024
MAX_INFLIGHT_UPLOADS = 4
MAX_INFLIGHT_BYTES = 12 * 1024 * 1024
LEASE_SECONDS = 15.0
MEDIA_TTL_SECONDS = 20.0
UPLOAD_WINDOW_SECONDS = 10.0
MAX_UPLOADS_PER_WINDOW = 12
MAX_UPLOAD_BYTES_PER_WINDOW = 20 * 1024 * 1024
HLS_TARGET_DURATION_SECONDS = 2


class RelayPreviewError(RuntimeError):
    """Base class for safe preview failures."""


class RelayPreviewUnavailable(RelayPreviewError):
    """No active viewer lease or no usable media exists."""


class RelayPreviewInvalidSegment(RelayPreviewError):
    """The uploaded segment is malformed or violates ordering."""


class RelayPreviewRateLimited(RelayPreviewError):
    """The node exceeded its bounded media upload budget."""


class RelayPreviewCapacityExceeded(RelayPreviewError):
    """The process-wide in-memory cache has no safe remaining capacity."""


@dataclass(frozen=True, slots=True)
class PreviewSegment:
    generation: str
    sequence: int
    body: bytes = field(repr=False)
    stored_at: float


@dataclass(slots=True)
class _NodePreview:
    lease_until: float
    generation: str | None = None
    segments: OrderedDict[int, PreviewSegment] = field(default_factory=OrderedDict)
    stored_bytes: int = 0
    recent_uploads: deque[tuple[float, int]] = field(default_factory=deque)


def validate_mpegts(payload: bytes) -> None:
    """Require a non-empty sequence of complete 188-byte MPEG-TS packets."""

    if not payload or len(payload) > MAX_SEGMENT_BYTES or len(payload) % 188:
        raise RelayPreviewInvalidSegment("invalid MPEG-TS length")
    if any(payload[offset] != 0x47 for offset in range(0, len(payload), 188)):
        raise RelayPreviewInvalidSegment("invalid MPEG-TS synchronization")


class RelayPreviewStore:
    """Thread-safe lease and media store with hard per-node/global bounds."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        lease_seconds: float = LEASE_SECONDS,
        media_ttl_seconds: float = MEDIA_TTL_SECONDS,
    ) -> None:
        if lease_seconds <= 0 or media_ttl_seconds <= 0:
            raise ValueError("preview TTLs must be positive")
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._media_ttl_seconds = media_ttl_seconds
        self._lock = threading.RLock()
        self._nodes: dict[str, _NodePreview] = {}
        self._stored_bytes = 0
        self._inflight_nodes: set[str] = set()
        self._inflight_uploads = 0
        self._inflight_bytes = 0

    def renew(self, node_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            if state is None:
                self._nodes[node_id] = _NodePreview(lease_until=now + self._lease_seconds)
            else:
                state.lease_until = now + self._lease_seconds

    def requested(self, node_id: str) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            return state is not None and state.lease_until > now

    def purge_node(self, node_id: str) -> None:
        with self._lock:
            self._drop_node_locked(node_id)

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()
            self._stored_bytes = 0

    @contextmanager
    def reserve_upload(self, node_id: str, declared_length: int) -> Iterator[None]:
        """Bound in-flight body memory and count every authenticated upload attempt."""

        if not 0 < declared_length <= MAX_SEGMENT_BYTES:
            raise RelayPreviewInvalidSegment("invalid declared segment length")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            if state is None or state.lease_until <= now:
                raise RelayPreviewUnavailable("preview was not requested")
            self._prune_upload_window_locked(state, now)
            if (
                len(state.recent_uploads) >= MAX_UPLOADS_PER_WINDOW
                or sum(size for _, size in state.recent_uploads) + declared_length
                > MAX_UPLOAD_BYTES_PER_WINDOW
            ):
                raise RelayPreviewRateLimited("preview upload rate exceeded")
            if (
                node_id in self._inflight_nodes
                or self._inflight_uploads >= MAX_INFLIGHT_UPLOADS
                or self._inflight_bytes + declared_length > MAX_INFLIGHT_BYTES
            ):
                raise RelayPreviewRateLimited("preview upload concurrency exceeded")
            state.recent_uploads.append((now, declared_length))
            self._inflight_nodes.add(node_id)
            self._inflight_uploads += 1
            self._inflight_bytes += declared_length
        try:
            yield
        finally:
            with self._lock:
                self._inflight_nodes.discard(node_id)
                self._inflight_uploads -= 1
                self._inflight_bytes -= declared_length

    def put(self, node_id: str, generation: str, sequence: int, body: bytes) -> None:
        validate_mpegts(body)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            if state is None or state.lease_until <= now:
                raise RelayPreviewUnavailable("preview was not requested")
            if state.generation == generation:
                existing = state.segments.get(sequence)
                if existing is not None:
                    if not hmac.compare_digest(existing.body, body):
                        raise RelayPreviewInvalidSegment("segment replay differs")
                    return
            if state.generation != generation:
                self._clear_segments_locked(state)
                state.generation = generation

            if state.segments and sequence <= next(reversed(state.segments)):
                raise RelayPreviewInvalidSegment("segment sequence is stale")

            while len(state.segments) >= MAX_SEGMENTS_PER_NODE:
                _, evicted = state.segments.popitem(last=False)
                state.stored_bytes -= len(evicted.body)
                self._stored_bytes -= len(evicted.body)
            if state.stored_bytes + len(body) > MAX_NODE_BYTES:
                raise RelayPreviewCapacityExceeded("node preview capacity exceeded")
            if self._stored_bytes + len(body) > MAX_GLOBAL_BYTES:
                raise RelayPreviewCapacityExceeded("global preview capacity exceeded")

            segment = PreviewSegment(generation, sequence, bytes(body), now)
            state.segments[sequence] = segment
            state.stored_bytes += len(body)
            self._stored_bytes += len(body)

    def playlist(self, node_id: str) -> bytes:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            if state is None or state.lease_until <= now or not state.segments:
                raise RelayPreviewUnavailable("preview media is unavailable")
            sequences = list(state.segments)
            contiguous = [sequences[-1]]
            for candidate in reversed(sequences[:-1]):
                if candidate != contiguous[-1] - 1:
                    break
                contiguous.append(candidate)
            contiguous.reverse()
            generation = state.generation
            if generation is None:  # pragma: no cover - segments imply a generation
                raise RelayPreviewUnavailable("preview media is unavailable")
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{HLS_TARGET_DURATION_SECONDS}",
                f"#EXT-X-MEDIA-SEQUENCE:{contiguous[0]}",
                "#EXT-X-INDEPENDENT-SEGMENTS",
            ]
            for sequence in contiguous:
                lines.extend(
                    (
                        f"#EXTINF:{HLS_TARGET_DURATION_SECONDS:.3f},",
                        f"segment/{generation}/{sequence}.ts",
                    )
                )
            return ("\n".join(lines) + "\n").encode("ascii")

    def segment(self, node_id: str, generation: str, sequence: int) -> bytes:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            state = self._nodes.get(node_id)
            if (
                state is None
                or state.lease_until <= now
                or state.generation != generation
                or sequence not in state.segments
            ):
                raise RelayPreviewUnavailable("preview segment is unavailable")
            return state.segments[sequence].body

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            self._purge_locked(self._clock())
            return self._stored_bytes

    def _prune_upload_window_locked(self, state: _NodePreview, now: float) -> None:
        cutoff = now - UPLOAD_WINDOW_SECONDS
        while state.recent_uploads and state.recent_uploads[0][0] <= cutoff:
            state.recent_uploads.popleft()

    def _purge_locked(self, now: float) -> None:
        expired_nodes = [
            node_id for node_id, state in self._nodes.items() if state.lease_until <= now
        ]
        for node_id in expired_nodes:
            self._drop_node_locked(node_id)
        cutoff = now - self._media_ttl_seconds
        for state in self._nodes.values():
            while state.segments:
                first_sequence = next(iter(state.segments))
                segment = state.segments[first_sequence]
                if segment.stored_at > cutoff:
                    break
                state.segments.pop(first_sequence)
                state.stored_bytes -= len(segment.body)
                self._stored_bytes -= len(segment.body)
            self._prune_upload_window_locked(state, now)

    def _clear_segments_locked(self, state: _NodePreview) -> None:
        self._stored_bytes -= state.stored_bytes
        state.segments.clear()
        state.stored_bytes = 0

    def _drop_node_locked(self, node_id: str) -> None:
        state = self._nodes.pop(node_id, None)
        if state is not None:
            self._stored_bytes -= state.stored_bytes


__all__ = [
    "MAX_SEGMENT_BYTES",
    "RelayPreviewCapacityExceeded",
    "RelayPreviewInvalidSegment",
    "RelayPreviewRateLimited",
    "RelayPreviewStore",
    "RelayPreviewUnavailable",
    "validate_mpegts",
]
