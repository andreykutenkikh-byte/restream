"""Bounded H.264/AAC packet splice before a persistent FLV copy publisher.

This is not an encoder or a frame generator. Only authenticated, profile-checked
local inputs are admitted by MediaRuntime. AVC/AAC configuration must match byte
for byte. A new input starts at an actual IDR with aligned audio. All preselection
packets and the old queued tail are counted, never called lossless continuity.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import IO

from app.broadcast.media_diagnostics import Emit, read_diagnostics

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
MAX_TAG = 2 * 1024 * 1024
MAX_BYTES = 8 * 1024 * 1024
MAX_PACKETS = 256


@dataclass(frozen=True)
class Tag:
    kind: int
    dts: int
    data: bytes

    @property
    def configuration(self) -> bool:
        return len(self.data) >= 2 and self.data[1] == 0

    @property
    def pts(self) -> int:
        return self.dts + (
            int.from_bytes(self.data[2:5], "big", signed=True) if self.kind == 9 else 0
        )

    def idr(self, length_size: int) -> bool:
        if self.kind != 9 or len(self.data) < 6 or self.data[:2] != b"\x17\x01":
            return False
        cursor, found = 5, False
        while cursor < len(self.data):
            if cursor + length_size > len(self.data):
                return False
            size = int.from_bytes(self.data[cursor : cursor + length_size], "big")
            cursor += length_size
            if size <= 0 or cursor + size > len(self.data):
                return False
            found |= self.data[cursor] & 31 == 5
            cursor += size
        return found

    def encode(self, offset: int = 0) -> bytes:
        stamp = self.dts + offset
        if not 0 <= stamp <= 0xFFFFFFFF:
            raise ValueError("selector_timestamp_range")
        size = len(self.data)
        header = bytes([self.kind]) + size.to_bytes(3, "big")
        header += (stamp & 0xFFFFFF).to_bytes(3, "big") + bytes([stamp >> 24]) + b"\0\0\0"
        return header + self.data + (11 + size).to_bytes(4, "big")


def exact(stream: IO[bytes], size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def read_tag(stream: IO[bytes]) -> Tag:
    header = exact(stream, 11)
    size = int.from_bytes(header[1:4], "big")
    if size > MAX_TAG or header[8:11] != b"\0\0\0":
        raise ValueError("selector_invalid_tag")
    data = exact(stream, size)
    if int.from_bytes(exact(stream, 4), "big") != size + 11:
        raise ValueError("selector_invalid_size")
    return Tag(header[0], int.from_bytes(header[4:7], "big") + (header[7] << 24), data)


class Input:
    def __init__(self, owner: Selector, identity: str, argv: list[str]) -> None:
        self.owner, self.identity = owner, identity
        self.config: dict[int, Tag] = {}
        self.counts = {8: 0, 9: 0}
        self.last: dict[int, int] = {}
        self.emitted_dts: dict[int, int] = {}
        self.rounding_repairs = 0
        self.arrivals: dict[int, float] = {}
        self.first_arrivals: dict[int, float] = {}
        self.length_size = 4
        self.audio_step = 0.0
        self.error: str | None = None
        self.timestamp_fault: dict[str, int] | None = None
        self.preselection_packets = 0
        self.started = time.monotonic()
        self.process = subprocess.Popen(  # noqa: S603 - fixed copy command, local verified input
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if owner.diagnostics else subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if owner.diagnostics:
            assert self.process.stderr is not None
            read_diagnostics(self.process.stderr, owner.diagnostics)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self) -> None:
        assert self.process.stdout
        try:
            header = exact(self.process.stdout, 13)
            if header[:3] != b"FLV" or header[5:9] != b"\0\0\0\x09":
                raise ValueError("selector_invalid_header")
            while True:
                tag = read_tag(self.process.stdout)
                if tag.kind not in (8, 9):
                    continue
                if (
                    len(tag.data) < 2
                    or (tag.kind == 8 and tag.data[0] >> 4 != 10)
                    or (tag.kind == 9 and (tag.data[0] & 15 != 7 or len(tag.data) < 5))
                ):
                    raise ValueError("selector_unsupported_codec")
                if tag.configuration:
                    if tag.kind == 9:
                        if len(tag.data) < 12:
                            raise ValueError("selector_invalid_avc")
                        self.length_size = (tag.data[9] & 3) + 1
                    else:
                        if len(tag.data) < 4 or tag.data[2] >> 3 != 2:
                            raise ValueError("selector_aac_lc_required")
                        rates = (
                            96000,
                            88200,
                            64000,
                            48000,
                            44100,
                            32000,
                            24000,
                            22050,
                            16000,
                            12000,
                            11025,
                            8000,
                            7350,
                        )
                        frequency = ((tag.data[2] & 7) << 1) | (tag.data[3] >> 7)
                        if frequency >= len(rates):
                            raise ValueError("selector_aac_rate_unsupported")
                        self.audio_step = (
                            (960 if tag.data[3] & 4 else 1024) * 1000 / rates[frequency]
                        )
                    if tag.kind in self.config and self.config[tag.kind].data != tag.data:
                        raise ValueError("selector_configuration_changed")
                    self.config[tag.kind] = tag
                    continue
                if tag.data[1] != 1:  # AVC end-of-sequence is not a media frame.
                    continue
                if tag.kind in self.last and tag.dts < self.last[tag.kind]:
                    self.timestamp_fault = {
                        "kind": tag.kind,
                        "previous": self.last[tag.kind],
                        "next": tag.dts,
                        "packets": self.counts[tag.kind],
                    }
                    raise ValueError("selector_nonadvancing_dts")
                self.last[tag.kind] = tag.dts
                previous = self.emitted_dts.get(tag.kind, -1)
                if tag.dts <= previous:
                    if previous + 1 - tag.dts > 1:
                        raise ValueError("selector_timestamp_overlap")
                    # FLV has integer-ms clocks. Preserve the packet on a repeated
                    # timestamp by an explicit <=1ms mapping, never by frame deletion.
                    tag = Tag(tag.kind, previous + 1, tag.data)
                    self.rounding_repairs += 1
                self.emitted_dts[tag.kind] = tag.dts
                self.arrivals[tag.kind] = time.monotonic()
                self.first_arrivals.setdefault(tag.kind, self.arrivals[tag.kind])
                self.counts[tag.kind] += 1
                self.owner.offer(self, tag)
        except ValueError as exc:
            self.error = str(exc)  # All ValueErrors above use fixed secret-free codes.
        except (EOFError, OSError):
            self.error = "input_ended"
        finally:
            if self.error and self.owner.diagnostics and not self.owner.ending:
                self.owner.diagnostics(self.error, None)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.thread.join(timeout=3)
        if self.process.stdout:
            self.process.stdout.close()


class Selector:
    def __init__(self, sink: IO[bytes], fps: float = 30, diagnostics: Emit | None = None) -> None:
        self.sink, self.fps = sink, fps
        self.diagnostics = diagnostics
        self.inputs: dict[str, Input] = {}
        self.input_restarts: dict[str, int] = {}
        self.input_faults: list[str] = []
        self.timestamp_faults: list[dict[str, int]] = []
        self.selected = ""
        self.requested = ""
        self.config: dict[int, Tag] = {}
        self.queue: deque[Tag] = deque()
        self.pending: list[Tag] = []
        self.queued_bytes = 0
        self.inflight = False
        self.offset = 0
        self.last_out: dict[int, int] = {}
        self.last_write: dict[int, float] = {}
        self.lock = threading.Condition()
        self.ending = False
        self.error: str | None = None
        self.rejection: str | None = None
        self.events: list[dict[str, float | int | str]] = []
        self.old_tail_packets = 0
        self.boundary: tuple[Tag, Tag] | None = None
        self.frames, self.audio_packets = 0, 0
        self.thread = threading.Thread(target=self.write, daemon=True)
        self.thread.start()

    def prepare(self, identity: str, argv: list[str]) -> None:
        existing = self.inputs.get(identity)
        if existing and existing.error and time.monotonic() - existing.started > 3:
            attempts = self.input_restarts.get(identity, 0)
            if attempts >= 5:
                self.rejection = "input_retry_exhausted"
                return
            existing.close()
            self.input_faults.append(existing.error)
            if existing.timestamp_fault:
                self.timestamp_faults.append(existing.timestamp_fault)
                self.timestamp_faults[:] = self.timestamp_faults[-16:]
            self.input_faults[:] = self.input_faults[-16:]
            with self.lock:
                if self.selected == identity:
                    # A reconnect is a new physical timeline, even at the same local URL.
                    self.selected = ""
                self.pending.clear()
                self.input_restarts[identity] = attempts + 1
                del self.inputs[identity]
        if identity not in self.inputs:
            if len(self.inputs) >= 2:
                raise ValueError("selector_input_limit")
            self.inputs[identity] = Input(self, identity, argv)

    def select(self, identity: str) -> None:
        with self.lock:
            if self.requested != identity:
                self.requested = identity
                self.pending.clear()
                self.rejection = None

    def offer(self, source: Input, tag: Tag) -> None:
        with self.lock:
            if self.ending or self.error:
                return
            if self.selected == source.identity:
                if len(self.queue) >= MAX_PACKETS or self.queued_bytes + len(tag.data) > MAX_BYTES:
                    self.error = "active_queue_overflow"
                    self.lock.notify_all()
                    return
                self.queue.append(tag)
                self.queued_bytes += len(tag.data)
                self.lock.notify_all()
                return
            source.preselection_packets += 1
            if self.requested != source.identity or set(source.config) != {8, 9}:
                return
            if self.config and any(self.config[k].data != source.config[k].data for k in (8, 9)):
                self.rejection = "direct_codec_configuration_incompatible"
                return
            if self.last_out and min(source.counts.values()) < 90:
                return
            if tag.idr(source.length_size):
                self.pending = [tag]
            elif self.pending:
                if tag.dts < self.pending[0].dts:
                    return  # Unselected pre-IDR audio; accounted above, not active loss.
                self.pending.append(tag)
            if not self.pending:
                return
            if len(self.pending) > 32 or sum(len(p.data) for p in self.pending) > MAX_TAG:
                self.pending.clear()
                self.rejection = "direct_alignment_unavailable"
                return
            first = {
                kind: next((p for p in self.pending if p.kind == kind), None) for kind in (8, 9)
            }
            if not first[8] or not first[9]:
                return
            if abs(first[8].pts - first[9].pts) > 100:
                self.pending.clear()
                self.rejection = "direct_av_alignment_rejected"
                return
            if self.queue or self.inflight:
                return  # Drain every active packet before committing a different timeline.
            # Commit only after the active queue AND its in-flight write have drained.
            now = time.monotonic()
            offset = float(-min(p.dts for p in self.pending))
            if self.last_out:
                offset = max(
                    self.last_out[k]
                    + max(
                        1000 / self.fps if k == 9 else source.audio_step,
                        (now - self.last_write[k]) * 1000,
                    )
                    - first[k].dts  # type: ignore[union-attr]
                    for k in (8, 9)
                )
            self.offset = int(offset + 1)
            old = self.selected
            self.config = source.config.copy()
            self.selected = source.identity
            self.boundary = (first[9], first[8])
            self.queue.extend(self.pending)
            self.queued_bytes = sum(len(p.data) for p in self.pending)
            self.pending.clear()
            if old:
                self.events.append(
                    {
                        "decision_at": now,
                        "prepared_ms": (now - source.started) * 1000,
                        "video_gap_ms": (now - self.last_write[9]) * 1000,
                        "audio_gap_ms": (now - self.last_write[8]) * 1000,
                        "timestamp_offset_ms": self.offset,
                        "old_tail_packets": self.old_tail_packets,
                    }
                )
                self.events[:] = self.events[-16:]
            self.lock.notify_all()

    def write(self) -> None:
        try:
            self.sink.write(FLV_HEADER)
            initialized = False
            while True:
                with self.lock:
                    self.lock.wait_for(lambda: self.ending or self.error or self.queue)
                    if self.ending or self.error:
                        return
                    if not initialized:
                        headers = b"".join(Tag(k, 0, self.config[k].data).encode() for k in (9, 8))
                        initialized = True
                    else:
                        headers = b""
                    tag = self.queue.popleft()
                    self.queued_bytes -= len(tag.data)
                    mapped = tag.dts + self.offset
                    if mapped <= self.last_out.get(tag.kind, -1):
                        self.error = "mapped_timestamp_regression"
                        return  # Fail closed; do not discard frames and carry on.
                    data = headers + tag.encode(self.offset)
                    self.inflight = True
                # Never hold the control lock during pipe I/O. A blocked destination
                # must not block lease expiry, queue accounting, or process termination.
                self.sink.write(data)
                self.sink.flush()
                with self.lock:
                    self.last_out[tag.kind] = mapped
                    self.last_write[tag.kind] = time.monotonic()
                    self.frames += tag.kind == 9
                    self.audio_packets += tag.kind == 8
                    self.inflight = False
                    self.lock.notify_all()
        except (OSError, ValueError):
            self.error = "selector_sink_failed"

    def close(self) -> None:
        with self.lock:
            self.ending = True
            self.lock.notify_all()
        for source in self.inputs.values():
            source.close()
        self.thread.join(timeout=3)
