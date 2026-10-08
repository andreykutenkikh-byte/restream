"""Classify process output into fixed codes. Raw lines never leave the reader."""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, Literal, get_args

from pydantic import Field

from app.broadcast.models import Input

Code = Literal[
    "io_timeout",
    "io_error",
    "connection_reset",
    "connection_refused",
    "broken_pipe",
    "rtp_packets_lost",
    "too_many_reordered_frames",
    "non_monotonic_dts",
    "queue_full",
    "invalid_media",
    "process_exit",
    "publisher_stalled",
    "publisher_retry",
    "input_ended",
    "selector_timestamp_range",
    "selector_invalid_tag",
    "selector_invalid_size",
    "selector_invalid_header",
    "selector_unsupported_codec",
    "selector_invalid_avc",
    "selector_aac_lc_required",
    "selector_aac_rate_unsupported",
    "selector_configuration_changed",
    "selector_nonadvancing_dts",
    "selector_timestamp_overlap",
    "active_queue_overflow",
    "mapped_timestamp_regression",
    "selector_sink_failed",
    "input_retry_exhausted",
    "direct_codec_configuration_incompatible",
    "direct_alignment_unavailable",
    "direct_av_alignment_rejected",
    "collector_overflow",
    "diagnostic_disk_error",
    "control_unreachable",
    "control_recovered",
    "agent_started",
]
Component = Literal["agent", "mediamtx", "publisher", "forwarder", "selector", "probe"]
SAFE_CODES = frozenset(get_args(Code))
Emit = Callable[[str, int | None], None]


class DiagnosticEvent(Input):
    sequence: int = Field(ge=1, le=10**15)
    at: float = Field(ge=0, le=253402300799, allow_inf_nan=False)
    component: Component
    code: Code
    route_id: str | None = Field(default=None, max_length=64)
    value: int | None = Field(default=None, ge=-255, le=10**12)


def classify(line: str) -> tuple[str, int | None] | None:
    """Only a fixed phrase and a bounded integer survive; never a URL or free text."""
    text = line.lower()
    for phrase, code in (
        ("too many reordered frames", "too_many_reordered_frames"),
        ("rtp packets lost", "rtp_packets_lost"),
        ("non-monoton", "non_monotonic_dts"),
        ("not monotonically", "non_monotonic_dts"),
        ("dts is greater than pts", "non_monotonic_dts"),
        ("queue is full", "queue_full"),
        ("connection reset", "connection_reset"),
        ("connection refused", "connection_refused"),
        ("broken pipe", "broken_pipe"),
        ("timed out", "io_timeout"),
        ("i/o timeout", "io_timeout"),
        ("input/output error", "io_error"),
        ("invalid data found", "invalid_media"),
        ("packet corrupt", "invalid_media"),
        ("error while decoding", "invalid_media"),
    ):
        if phrase in text:
            match = (
                re.search(r"\b(\d{1,10}) rtp packets lost\b", text)
                if code == "rtp_packets_lost"
                else None
            )
            return code, int(match[1]) if match else None
    return None


def read_diagnostics(stream: IO[Any], emit: Emit) -> None:
    def read() -> None:
        try:
            with stream:
                while chunk := stream.readline(4096):
                    line = (
                        chunk.decode("utf-8", errors="replace")
                        if isinstance(chunk, bytes)
                        else chunk
                    )
                    result = classify(line)
                    if result:
                        emit(*result)
        except (OSError, ValueError):
            pass

    threading.Thread(target=read, daemon=True, name="safe-media-diagnostics").start()


class QuietRotatingHandler(RotatingFileHandler):
    failed = False

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        self.failed = True  # Disk errors must neither stop media nor print a raw traceback.


class DiagnosticCollector:
    """Bounded memory plus 3 x 2 MiB local files, independent of controller availability."""

    def __init__(self, directory: Path) -> None:
        self.boot_id = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.pending: deque[DiagnosticEvent] = deque(maxlen=256)
        self.sequence = 0
        self.dropped = 0
        self.last: dict[tuple[str, str | None, str], float] = {}
        self.handler: QuietRotatingHandler | None = None
        with suppress(OSError):
            self.handler = QuietRotatingHandler(
                directory / "diagnostics.jsonl",
                maxBytes=2 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
        self.disk_queue: queue.Queue[DiagnosticEvent] = queue.Queue(maxsize=256)
        self.ending = threading.Event()
        self.disk_dropped = 0
        self.writer = threading.Thread(target=self._write, daemon=True, name="diagnostic-disk")
        self.writer.start()
        self.emit("agent", None, "agent_started")
        if self.handler is None:
            self.emit("agent", None, "diagnostic_disk_error")

    def callback(self, component: Component, route_id: str | None = None) -> Emit:
        def emit(code: str, value: int | None = None) -> None:
            self.emit(component, route_id, code, value)

        return emit

    def emit(
        self, component: Component, route_id: str | None, code: str, value: int | None = None
    ) -> None:
        if code not in SAFE_CODES:
            return
        with self.lock:
            now = time.monotonic()
            key = (component, route_id, code)
            if now - self.last.get(key, -10) < 5:
                return
            if len(self.last) >= 1024:
                self.last.clear()
            self.last[key] = now
            self.sequence += 1
            event = DiagnosticEvent.model_validate(
                dict(
                    sequence=self.sequence,
                    at=time.time(),
                    component=component,
                    route_id=route_id,
                    code=code,
                    value=value,
                )
            )
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            self.pending.append(event)
            try:
                self.disk_queue.put_nowait(event)
            except queue.Full:
                self.disk_dropped += 1

    def _write(self) -> None:
        while not self.ending.is_set() or not self.disk_queue.empty():
            try:
                event = self.disk_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.handler:
                record = logging.LogRecord(
                    "stream.diagnostic",
                    logging.WARNING,
                    "",
                    0,
                    json.dumps({"boot_id": self.boot_id, **event.model_dump()}),
                    (),
                    None,
                )
                self.handler.handle(record)
        if self.handler:
            self.handler.close()

    def snapshot(self) -> list[DiagnosticEvent]:
        if self.handler and self.handler.failed:
            self.emit("agent", None, "diagnostic_disk_error")
        if self.dropped or self.disk_dropped:
            self.emit("agent", None, "collector_overflow", self.dropped + self.disk_dropped)
        with self.lock:
            return list(self.pending)[:64]

    def acknowledge(self, sequence: int) -> None:
        with self.lock:
            while self.pending and self.pending[0].sequence <= sequence:
                self.pending.popleft()

    def close(self) -> None:
        self.ending.set()
        self.writer.join(timeout=2)
