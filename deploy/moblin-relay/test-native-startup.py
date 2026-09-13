#!/usr/bin/python3
"""One CI-only diagnostic prefix of the real native self-test, not acceptance."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

STAGED = {
    "self-test": Path("/tmp/adojapan-ci-clock-self-test.py"),  # noqa: S108
    "normalizer.py": Path("/tmp/adojapan-ci-startup-normalizer.py"),  # noqa: S108
    "wrapper.py": Path("/tmp/adojapan-ci-startup-wrapper.py"),  # noqa: S108
    "renderer.py": Path("/tmp/adojapan-ci-startup-renderer.py"),  # noqa: S108
    "reader.py": Path("/tmp/adojapan-ci-startup-reader.py"),  # noqa: S108
    "slate.txt": Path("/tmp/adojapan-ci-startup-slate.txt"),  # noqa: S108
}
PURPOSE = "adojapan-ci-native-startup-diagnostic"
TARGET = "crash-first-child"
STOP = "CI_DIAGNOSTIC_CRASH_PREFIX_FINISHED"
WORK_SECONDS = 480
PACKET_TRACE_BYTES = 32 * 1024
PACKET_TRACE_INSPECTION_BYTES = 512 * 1024
PACKET_PHASES = ("demux_receive", "parser_output")
PACKET_FLAGS = (
    "sampled",
    "streams_limited",
    "inspection_limited",
    "window_limited",
    "clock_invalid",
)
PACKET_PATTERN = re.compile(
    rb"\[flv @ (?:0x)?[0-9a-fA-F]{1,16}\] "
    rb"(ff_read_packet|read_frame_internal) stream=([0-9]{1,3}), "
    rb"pts=(NOPTS|-?[0-9]{1,19}), dts=(NOPTS|-?[0-9]{1,19}), "
    rb"size=([0-9]{1,10}), duration=(-?[0-9]{1,19}), flags=([0-9]{1,10})"
)


class DiagnosticFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise DiagnosticFailure(code)


def read_private(path, *, maximum=512 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == 0
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600
            and 0 < before.st_size <= maximum,
            "UNSAFE_STAGED_FILE",
        )
        value = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        require(
            len(value) == before.st_size
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            "STAGED_FILE_CHANGED",
        )
    return value


def save_new(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def renderer_for_stage(data, stage):
    """Change only the private renderer's asset location, retaining source bytes."""
    require(
        re.fullmatch(r"/tmp/adojapan-ci-startup-[A-Za-z0-9_-]{8,64}", str(stage)),  # noqa: S108
        "PRIVATE_RENDERER_STAGE_PATH",
    )
    original = b'SLATE_FILE = "/var/lib/moblin-relay/slate.mp4"'
    require(data.count(original) == 1, "PRIVATE_RENDERER_SLATE_ANCHOR_CHANGED")
    replacement = ("SLATE_FILE = " + repr(str(stage / "slate.mp4"))).encode("ascii")
    return data.replace(original, replacement, 1)


def stage_sources(stage):
    hashes = {}
    for name, source in STAGED.items():
        data = read_private(source)
        hashes[name] = hashlib.sha256(data).hexdigest()
        if name == "renderer.py":
            save_new(stage / "renderer-source.py", data)
            data = renderer_for_stage(data, stage)
            hashes["renderer-staged.py"] = hashlib.sha256(data).hexdigest()
        save_new(stage / name, data, 0o755 if name == "wrapper.py" else 0o600)
    manifest = {
        "version": 1,
        "purpose": PURPOSE,
        "normalizer_sha256": hashes["normalizer.py"],
        "wrapper_sha256": hashes["wrapper.py"],
    }
    save_new(stage / "manifest.json", json.dumps(manifest).encode("ascii"))
    return hashes


def slate_command(stage):
    # Same installer profile and source text, directed only into our private stage.
    return [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x111827:s=1080x1920:r=30:d=12",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-vf",
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"textfile={stage}/slate.txt:fontcolor=white:fontsize=72:line_spacing=28:"
        "x=(w-text_w)/2:y=(h-text_h)/2",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-profile:v",
        "main",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-b:v",
        "8M",
        "-minrate",
        "8M",
        "-maxrate",
        "8M",
        "-bufsize",
        "16M",
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1:filler=1:bframes=0",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-t",
        "12",
        "-shortest",
        "-movflags",
        "+faststart",
        str(stage / "slate.mp4"),
    ]


def safe_reader_packet_trace(value):
    """Strict numeric-only projection; collector observations are not wire arrival."""

    def integer(item, lower, upper):
        return type(item) is int and lower <= item <= upper

    if not isinstance(value, dict) or set(value) != {
        "version",
        "scope",
        "timebase",
        "flags",
        "groups",
    }:
        return None
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["scope"] != "strict-reader-collector-not-network-or-decode"
        or value["timebase"] != "flv-milliseconds"
    ):
        return None
    flags, groups = value["flags"], value["groups"]
    if (
        not isinstance(flags, dict)
        or set(flags) != set(PACKET_FLAGS)
        or any(type(item) is not bool for item in flags.values())
        or not isinstance(groups, list)
        or len(groups) > 8
    ):
        return None
    seen, streams, total_rows = set(), set(), 0
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "phase",
            "stream",
            "count",
            "first_ms",
            "last_ms",
            "rows",
        }:
            return None
        phase, stream, rows = group["phase"], group["stream"], group["rows"]
        if (
            not isinstance(phase, str)
            or phase not in PACKET_PHASES
            or not integer(stream, 0, 255)
            or (phase, stream) in seen
            or not integer(group["count"], 1, 65535)
            or not integer(group["first_ms"], 0, 20000)
            or not integer(group["last_ms"], group["first_ms"], 20000)
            or not isinstance(rows, list)
            or not 1 <= len(rows) <= 16
            or len(rows) != min(group["count"], 16)
        ):
            return None
        seen.add((phase, stream))
        streams.add(stream)
        total_rows += len(rows)
        previous = group["first_ms"]
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "wall_ms",
                "pts",
                "dts",
                "duration",
                "size",
                "flags",
            }:
                return None
            if (
                not integer(row["wall_ms"], previous, group["last_ms"])
                or any(
                    item is not None and not integer(item, -(2**63), 2**63 - 1)
                    for item in (row["pts"], row["dts"])
                )
                or not integer(row["duration"], -(2**63), 2**63 - 1)
                or not integer(row["size"], 1, 2**31 - 1)
                or not integer(row["flags"], 0, 2**31 - 1)
            ):
                return None
            previous = row["wall_ms"]
        if rows[0]["wall_ms"] != group["first_ms"] or rows[-1]["wall_ms"] != group["last_ms"]:
            return None
        if group["count"] > 16 and not flags["sampled"]:
            return None
    if len(streams) > 4 or total_rows > 128:
        return None
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    return json.loads(encoded) if len(encoded.encode("ascii")) <= PACKET_TRACE_BYTES else None


class ReaderPacketTrace:
    """First eight + last eight observations per phase/anonymous stream, no raw lines."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.previous = self.started
        self.lock = threading.Lock()
        self.closed = False
        self.inspected = 0
        self.groups = {}
        self.streams = set()
        self.flags = dict.fromkeys(PACKET_FLAGS, False)

    def observe_line(self, line, *, progress):
        if progress:
            return
        with self.lock:
            if (
                self.closed
                or self.flags["inspection_limited"]
                or self.flags["window_limited"]
                or self.flags["clock_invalid"]
            ):
                return
            self.inspected += len(line) + 1
            if self.inspected > PACKET_TRACE_INSPECTION_BYTES:
                self.flags["inspection_limited"] = True
                return
            now = self.clock()
            if (
                any(
                    type(item) not in (int, float) or not -(10**12) <= item <= 10**12
                    for item in (now, self.started)
                )
                or not math.isfinite(now)
                or not math.isfinite(self.started)
                or now < self.previous
            ):
                self.flags["clock_invalid"] = True
                return
            self.previous = now
            elapsed = now - self.started
            if elapsed > 20:
                self.flags["window_limited"] = True
                return
            match = PACKET_PATTERN.fullmatch(line)
            if not match:
                return
            name, stream, pts, dts, size, duration, flags = match.groups()
            stream = int(stream)
            row = {
                "wall_ms": int(elapsed * 1000),
                "pts": None if pts == b"NOPTS" else int(pts),
                "dts": None if dts == b"NOPTS" else int(dts),
                "duration": int(duration),
                "size": int(size),
                "flags": int(flags),
            }
            if (
                stream > 255
                or not 0 < row["size"] < 2**31
                or row["flags"] >= 2**31
                or any(
                    item is not None and not -(2**63) <= item < 2**63
                    for item in (row["pts"], row["dts"], row["duration"])
                )
            ):
                return
            if stream not in self.streams and len(self.streams) == 4:
                self.flags["streams_limited"] = True
                return
            self.streams.add(stream)
            phase = PACKET_PHASES[name == b"read_frame_internal"]
            group = self.groups.setdefault(
                (phase, stream),
                {
                    "phase": phase,
                    "stream": stream,
                    "count": 0,
                    "first_ms": row["wall_ms"],
                    "last_ms": row["wall_ms"],
                    "rows": [],
                },
            )
            group["count"] = min(group["count"] + 1, 65535)
            group["last_ms"] = row["wall_ms"]
            if len(group["rows"]) == 16:
                del group["rows"][8]
                self.flags["sampled"] = True
            group["rows"].append(row)

    def finish(self):
        with self.lock:
            self.closed = True
            return safe_reader_packet_trace(
                {
                    "version": 1,
                    "scope": "strict-reader-collector-not-network-or-decode",
                    "timebase": "flv-milliseconds",
                    "flags": dict(self.flags),
                    "groups": list(self.groups.values()),
                }
            )


def install_reader_packet_trace(api, state):
    """Instance-local observer adapter; original process lifecycle/deadline stays intact."""
    original = api.get("run_capture_reader")
    if not callable(original) or "CaptureReaderProgress" not in api:
        return
    if getattr(original, "_private_reader_packet_trace", False):
        return
    original_drain = api["CaptureReaderProgress"].drain

    class ObservedReader:
        def __init__(self, diagnostic, trace):
            self.diagnostic, self.trace = diagnostic, trace

        def __getattr__(self, name):
            return getattr(self.diagnostic, name)

        def observe_line(self, line, *, progress):
            self.diagnostic.observe_line(line, progress=progress)
            with contextlib.suppress(Exception):
                self.trace.observe_line(line, progress=progress)

        def drain(self, pipe, *, progress):
            # Same implementation, same pipe reads/limits and no added I/O/thread.
            return original_drain(self, pipe, progress=progress)

    def run_capture_reader(command, diagnostic, *, timeout):
        if (
            not isinstance(command, list)
            or command.count("-i") != 1
            or "-fdebug" in command
            or state.get("reader_packet_failure") is not None
        ):
            return original(command, diagnostic, timeout=timeout)
        input_index = command.index("-i")
        observed_command = command[:input_index] + ["-fdebug", "ts"] + command[input_index:]
        trace = ReaderPacketTrace()
        try:
            return original(observed_command, ObservedReader(diagnostic, trace), timeout=timeout)
        except Exception as exc:
            with contextlib.suppress(Exception):
                state["reader_packet_failure"] = (exc, state.get("last_stage"), trace.finish())
            raise
        finally:
            # Success records are discarded. A late optional callback cannot grow them.
            with contextlib.suppress(Exception):
                trace.finish()

    run_capture_reader._private_reader_packet_trace = True
    api["run_capture_reader"] = run_capture_reader


def failed_reader_packet_trace(api, state, progress):
    """Only the first caught reader exception in the final failure chain may be exported."""
    failed = state.get("reader_packet_failure")
    media = api.get("SELF_TEST_MEDIA_FAILURE")
    if not failed or not media or failed[1] != progress.get("stage"):
        return None
    current, visited = media[0], set()
    for _ in range(8):
        if not isinstance(current, BaseException) or id(current) in visited:
            return None
        if current is failed[0]:
            return safe_reader_packet_trace(failed[2])
        visited.add(id(current))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return None


PAUSE_EVENT_FIELDS = {
    "pause-request": (),
    "pause-return": ("acknowledged", "last_dispatch_ns", "packets", "bytes"),
    "resume-request": (),
    "resume-return": ("acknowledged",),
    "wait-start": ("anchor_ns", "deadline_ns"),
    "predicate": (
        "evaluated_ns",
        "sample_started_ns",
        "sample_finished_ns",
        "sample_age_ns",
        "accepted",
    ),
    "wait-return": ("succeeded",),
    "metrics": ("role", "generation", "started_ns", "finished_ns", "available"),
}
PAUSE_SAMPLE_FLAGS = (
    "dut_alive",
    "sink_alive",
    "reader_alive",
    "dut_metrics_ok",
    "sink_metrics_ok",
    "path_ready",
    "ingest_live",
    "live",
    "normalized",
    "forward",
)
PAUSE_SAMPLE_COUNTERS = (
    "ingest_bytes",
    "ingest_transport_bytes",
    "normalized_bytes",
    "normalized_media_bytes",
    "sink_bytes",
    "capture_size",
)


def pause_seconds_ns(value):
    if type(value) in (int, float) and 0 <= value <= 2**33 and math.isfinite(value):
        return round(value * 10**9)
    return None


class PauseTimeline:
    """Bounded observation receipts; no request timestamp is a media arrival time."""

    def __init__(self, api, clock=time.monotonic_ns):
        self.api, self.clock = api, clock
        self.lock = threading.Lock()
        self.events = deque(maxlen=512)
        self.t0 = self.observer = self.frozen = self.deadline_ns = None
        self.runtime_api = {}
        self.dropped = 0
        self.clock_invalid = self.invalid_data = self.closed = self.wait_seen = False
        self.last = 0

    def record(self, code, *values):
        with self.lock:
            if self.closed:
                return
            fields = PAUSE_EVENT_FIELDS.get(code)
            if (
                fields is None
                or len(fields) != len(values)
                or any(
                    item is not None
                    and type(item) is not bool
                    and (type(item) is not int or not -(2**63) < item < 2**63)
                    for item in values
                )
            ):
                self.invalid_data = True
                return
            stamp = self.clock()
            if type(stamp) is not int or not self.last <= stamp < 2**63:
                self.clock_invalid = True
                return
            self.last = stamp
            if code == "pause-request" and self.t0 is None:
                self.t0 = stamp
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.events.append([stamp, code, *values])

    def start_pause(self):
        if self.t0 is None:
            self.record("pause-request")

    def freeze(self, runtime=None):
        if self.frozen is not None:
            return self.frozen
        with self.lock:
            self.closed = True
            ended = self.clock()
            if type(ended) is not int or not self.last <= ended < 2**63:
                self.clock_invalid = True
                ended = self.last
            events = [list(row) for row in self.events]
        if self.t0 is None:
            self.frozen = {"available": False}
            return self.frozen
        origin = self.t0
        parent = []
        for row in events:
            if not origin - 2_000_000_000 <= row[0] <= ended:
                continue
            fields = PAUSE_EVENT_FIELDS[row[1]]
            parent.append(
                [
                    row[0] - origin,
                    row[1],
                    *[
                        item - origin
                        if name.endswith("_ns") and not name.endswith("age_ns") and item is not None
                        else item
                        for name, item in zip(fields, row[2:], strict=True)
                    ],
                ]
            )
        samples = []
        if self.observer is not None:
            with self.observer.lock:
                samples = [dict(sample) for sample in self.observer.samples]
        identities = {role: {} for role in ("ingest", "normalized", "sink")}
        projected = []
        for sample in samples:
            first, last = (
                pause_seconds_ns(sample.get("t")),
                pause_seconds_ns(sample.get("finished")),
            )
            if (
                first is None
                or last is None
                or not origin - 2_000_000_000 <= first <= last <= ended
            ):
                continue
            item = {
                "started_ns": first - origin,
                "finished_ns": last - origin,
                "collection_age_ns": ended - last,
            }
            item.update(
                {
                    name: sample.get(name) if type(sample.get(name)) is bool else None
                    for name in PAUSE_SAMPLE_FLAGS
                }
            )
            item.update(
                {
                    name: sample.get(name)
                    if type(sample.get(name)) is int and 0 <= sample[name] < 2**63
                    else None
                    for name in PAUSE_SAMPLE_COUNTERS
                }
            )
            for role, mapping in identities.items():
                ids = sample.get(role + "_ids")
                ordinals = None
                if (
                    type(ids) is list
                    and len(ids) <= 8
                    and all(type(value) is str and 0 < len(value) <= 256 for value in ids)
                    and len(mapping) + len(ids) <= 256
                ):
                    ordinals = [mapping.setdefault(value, len(mapping) + 1) for value in ids]
                item[role + "_generations"] = ordinals
            projected.append(item)
        runtime_rows, runtime_quality = [], {"available": False}
        validator = self.runtime_api.get("validated_timeline_report")
        trusted = validator(runtime) if callable(validator) else None
        if trusted is not None:
            runtime_rows = [
                [row[0] - origin, *row[1:]]
                for row in trusted["events"]
                if origin - 2_000_000_000 <= row[0] <= ended
            ]
            runtime_quality = {
                "available": True,
                **{
                    key: trusted[key] for key in ("dropped_events", "clock_invalid", "invalid_data")
                },
            }
        summary = self.summarize(parent, projected, runtime_rows)
        sample_dropped = max(0, len(projected) - 128)
        runtime_dropped = max(0, len(runtime_rows) - 512)
        if sample_dropped:
            projected = projected[:64] + projected[-64:]
        if runtime_dropped:
            runtime_rows = runtime_rows[:256] + runtime_rows[-256:]
        self.frozen = {
            "available": True,
            "version": 1,
            "clock": "monotonic_ns_relative_to_pause_request",
            "scope": "observation-receipts-not-wire-or-media-event-times",
            "summary": summary,
            "clock_invalid": self.clock_invalid,
            "invalid_data": self.invalid_data,
            "dropped_events": self.dropped,
            "sample_rows_omitted": sample_dropped,
            "runtime_rows_omitted": runtime_dropped,
            "runtime_quality": runtime_quality,
            "event_fields": PAUSE_EVENT_FIELDS,
            "events": parent,
            "samples": projected,
            "runtime_event_fields": self.runtime_api.get("TIMELINE_EVENT_FIELDS", {}),
            "runtime_reasons": self.runtime_api.get("TIMELINE_REASONS", ()),
            "runtime_events": runtime_rows,
        }
        return self.frozen

    def summarize(self, events, samples, runtime):
        groups = {name: [row for row in events if row[1] == name] for name in PAUSE_EVENT_FIELDS}
        ack = next((row for row in groups["pause-return"] if row[2] is True), None)
        last_evaluated = groups["predicate"][-1] if groups["predicate"] else None
        deadline = self.deadline_ns - self.t0 if self.deadline_ns is not None else None
        timely = [
            sample
            for sample in samples
            if deadline is not None and sample["finished_ns"] < deadline
        ]
        # Recovery growth must not replace the last growth observed while paused.
        pause_end = groups["resume-request"][0][0] if groups["resume-request"] else None
        paused_samples = [
            sample for sample in samples if pause_end is None or sample["finished_ns"] <= pause_end
        ]
        before_pause = [sample for sample in samples if sample["finished_ns"] < 0]
        expected_ingest = before_pause[-1]["ingest_generations"] if before_pause else None
        growth = {}
        for counter, role in (
            ("ingest_bytes", "ingest"),
            ("normalized_bytes", "normalized"),
            ("normalized_media_bytes", "normalized"),
            ("sink_bytes", "sink"),
        ):
            intervals = [
                [left["finished_ns"], right["finished_ns"]]
                for left, right in zip(paused_samples, paused_samples[1:], strict=False)
                if type(left[counter]) is int
                and type(right[counter]) is int
                and right[counter] > left[counter]
                and left[role + "_generations"]
                and left[role + "_generations"] == right[role + "_generations"]
            ]
            growth[counter] = intervals[-1] if intervals else None
        metrics = groups["metrics"]
        summaries = []
        for role in (0, 1):
            rows = [row for row in metrics if row[2] == role]
            summaries.append(
                {
                    "role": role,
                    "requests": len(rows),
                    "available": sum(row[6] is True for row in rows),
                    "last_generation": rows[-1][3] if rows else None,
                    "max_duration_ns": max((row[5] - row[4] for row in rows), default=None),
                }
            )
        positive_runtime = [
            row for row in runtime if row[0] >= 0 and (pause_end is None or row[0] <= pause_end)
        ]
        videos = [row for row in positive_runtime if row[1] == 3]
        return {
            "pause_request_ns": 0,
            "pause_ack_observed_ns": ack[0] if ack else None,
            "last_feeder_dispatch_receipt_ns": ack[3] if ack else None,
            "pause_deadline_ns": deadline,
            "paused_window_end_ns": pause_end,
            "last_observed_growth_intervals_ns": growth,
            "last_runtime_video_progress": videos[-1] if videos else None,
            "first_runtime_watchdog_reject": next(
                (row for row in positive_runtime if row[1] == 6), None
            ),
            "first_runtime_kill_request": next(
                (row for row in positive_runtime if row[1] == 8 and row[3] == 0), None
            ),
            "first_runtime_child_wait_completed": next(
                (row for row in positive_runtime if row[1] == 9 and row[3] == 1), None
            ),
            "first_normalized_absent_sample": next(
                (
                    sample
                    for sample in paused_samples
                    if sample["finished_ns"] >= 0
                    and sample["dut_metrics_ok"] is True
                    and sample["normalized"] is False
                ),
                None,
            ),
            "first_local_slate_state_sample": next(
                (
                    sample
                    for sample in paused_samples
                    if sample["finished_ns"] >= 0
                    and sample["dut_metrics_ok"] is True
                    and sample["sink_metrics_ok"] is True
                    and sample["ingest_live"] is True
                    and expected_ingest is not None
                    and len(expected_ingest) == 1
                    and sample["ingest_generations"] == expected_ingest
                    and sample["path_ready"] is True
                    and sample["live"] is False
                    and sample["normalized"] is False
                ),
                None,
            ),
            "wait_return": groups["wait-return"][-1] if groups["wait-return"] else None,
            "resume_request": groups["resume-request"][0] if groups["resume-request"] else None,
            "resume_return": groups["resume-return"][0] if groups["resume-return"] else None,
            "last_evaluated_predicate": last_evaluated,
            "latest_completed_sample_before_deadline": timely[-1] if timely else None,
            "observer_metrics": summaries,
        }

    def report(self, include_rows=True):
        value = self.frozen if self.frozen is not None else self.freeze()
        omitted = set() if include_rows else {"events", "samples", "runtime_events"}
        return json.loads(
            json.dumps(
                {key: item for key, item in value.items() if key not in omitted}, allow_nan=False
            )
        )


def install_pause_timeline(api, state, stage, runtime_api):
    """Observe only original calls; added work never supplies an acceptance sample."""
    timeline = PauseTimeline(api)
    timeline.runtime_api = runtime_api
    state["pause_timeline"] = timeline
    original_fetch = api["fetch_metrics"]
    feeder, requests = [None], [0, 0]

    class Feeder(api["PacedMPEGTSFeeder"]):
        def pause(self, *args, **kwargs):
            observed = timeline.t0 is None and state.get("last_stage") == "stall-pause"
            if observed:
                timeline.start_pause()
                feeder[0] = self
            try:
                result = super().pause(*args, **kwargs)
            except BaseException:
                if observed:
                    timeline.record("pause-return", None, None, None, None)
                raise
            if observed:
                with contextlib.suppress(Exception), self._condition:
                    timeline.record(
                        "pause-return",
                        result if type(result) is bool else None,
                        pause_seconds_ns(self._clock_last),
                        self._clock_packets,
                        self._clock_bytes,
                    )
            return result

        def resume(self, *args, **kwargs):
            observed = self is feeder[0] and not timeline.closed
            if observed:
                timeline.record("resume-request")
            try:
                result = super().resume(*args, **kwargs)
            except BaseException:
                if observed:
                    timeline.record("resume-return", None)
                raise
            if observed:
                timeline.record("resume-return", result if type(result) is bool else None)
            return result

    class Observed(api["Observer"]):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            timeline.observer = self

        def wait_sample(self, description, predicate, timeout, **kwargs):
            if (
                description != "same-session SLATE transition"
                or timeline.t0 is None
                or timeline.wait_seen
            ):
                return super().wait_sample(description, predicate, timeout, **kwargs)
            timeline.wait_seen = True
            anchor = pause_seconds_ns(kwargs.get("not_before"))
            bound = pause_seconds_ns(api.get("SRT_IDLE_LOWER_BOUND_SECONDS"))
            timeline.deadline_ns = (
                anchor + bound if anchor is not None and bound is not None else None
            )
            timeline.record("wait-start", anchor, timeline.deadline_ns)

            def observed_predicate(sample):
                accepted = None
                try:
                    accepted = predicate(sample)
                    return accepted
                finally:
                    with contextlib.suppress(Exception):
                        evaluated = timeline.clock()
                        finished = pause_seconds_ns(sample.get("finished"))
                        age = (
                            evaluated - finished
                            if finished is not None and evaluated >= finished
                            else None
                        )
                        timeline.record(
                            "predicate",
                            evaluated,
                            pause_seconds_ns(sample.get("t")),
                            finished,
                            age,
                            accepted if type(accepted) is bool else None,
                        )

            try:
                result = super().wait_sample(description, observed_predicate, timeout, **kwargs)
            except BaseException:
                timeline.record("wait-return", False)
                raise
            timeline.record("wait-return", True)
            return result

    def fetch(port):
        roles = (api["DUT_METRICS_PORT"], api["SINK_METRICS_PORT"])
        if (
            threading.current_thread() is not timeline.observer
            or port not in roles
            or timeline.closed
        ):
            return original_fetch(port)
        role, started, available = roles.index(port), timeline.clock(), False
        requests[role] += 1
        try:
            result = original_fetch(port)
            available = True
            return result
        finally:
            with contextlib.suppress(Exception):
                timeline.record(
                    "metrics", role, requests[role], started, timeline.clock(), available
                )

    def freeze():
        if timeline.frozen is not None:
            return
        runtime = None
        with contextlib.suppress(Exception):
            runtime = json.loads(
                read_private(
                    stage / runtime_api["TIMELINE_FILE"], maximum=runtime_api["TIMELINE_MAX_BYTES"]
                )
            )
        timeline.freeze(runtime)

    api.update(PacedMPEGTSFeeder=Feeder, Observer=Observed, fetch_metrics=fetch)
    state["freeze_pause_timeline"] = freeze
    return timeline


def install_prefix(api, stage, mediamtx, stage_file):
    """Keep real gates/cleanup; stop after the original supervisor-crash checks."""
    api.update(
        NORMALIZER=stage / "wrapper.py",
        RENDERER=stage / "renderer.py",
        SLATE=stage / "slate.mp4",
        MEDIAMTX=mediamtx,
        RESULT_FILE=stage / "prefix-result.json",
        SELF_TEST_PROGRESS_FILE=stage / "prefix-progress.json",
        SELF_TEST_STAGE_FILE=str(stage_file),
    )
    original_mark = api["mark_self_test_stage"]
    original_write_configs = api["write_configs"]
    original_diagnostics = api["MediaFailureDiagnostics"]
    state = {
        "initial_completed": False,
        "target_armed": False,
        "target_completed": False,
        "crash_cont_seen": False,
        "last_stage": "startup",
    }

    class CrashDiagnostics(original_diagnostics):
        def __init__(self, scope, *args, ignored_supervisor=None, **kwargs):
            if scope == "crash":
                require(
                    state["last_stage"] == "crash-death"
                    and not state["target_armed"]
                    and type(ignored_supervisor) is int
                    and 1 <= ignored_supervisor <= 2**31 - 1,
                    "PRIVATE_CRASH_ARM_PRECONDITION",
                )
                # Before original diagnostic/fault clocks and SIGKILL. Already
                # running supervisors never recheck this marker; exclude the old PID too.
                save_new(
                    stage / "normalizer-capture.arm",
                    f"{TARGET}\n{ignored_supervisor}\n".encode("ascii"),
                )
                state["target_armed"] = True
            super().__init__(scope, *args, ignored_supervisor=ignored_supervisor, **kwargs)

    def write_configs(*args, **kwargs):
        # Keep /tmp noexec: the fixed interpreter reads this private script.
        wrapper = str(stage / "wrapper.py")
        require(
            re.fullmatch(r"/tmp/adojapan-ci-startup-[A-Za-z0-9_-]{8,64}/wrapper\.py", wrapper),  # noqa: S108
            "PRIVATE_HOOK_STAGE_PATH",
        )
        sink_path, dut_path = original_write_configs(*args, **kwargs)
        config = json.loads(read_private(dut_path))
        ingest = config["paths"][api["INGEST_PATH"]]
        require(ingest.get("runOnAvailable") == wrapper, "PRIVATE_HOOK_ANCHOR_CHANGED")
        ingest["runOnAvailable"] = "/usr/bin/python3 " + wrapper
        api["atomic_json"](dut_path, config)
        return sink_path, dut_path

    def no_stale_work():
        # Never delete residues from the preceding acceptance attempt.
        api["validate_test_root"]()
        if any(api["TEST_ROOT"].glob(".run-*")):
            raise api["TestFailure"]("PRIOR_NATIVE_WORKDIR_REMAINS")
        return 0

    def mark(name, *, strict_segment_index=None):
        original_mark(name, strict_segment_index=strict_segment_index)
        # Only fixed original stage names are recorded, never exception strings.
        state["last_stage"] = name
        if name == "stuck-start" and "freeze_pause_timeline" in state:
            with contextlib.suppress(Exception):
                state["freeze_pause_timeline"]()
        if name == "auth-exclusive":
            state["initial_completed"] = True
        if name == "crash-cont":
            state["crash_cont_seen"] = True
        if name == "reset-start" and not state["target_completed"]:
            require(
                state["target_armed"] and state["crash_cont_seen"], "PRIVATE_CRASH_NOT_OBSERVED"
            )
            state["target_completed"] = True
            raise api["TestFailure"](STOP)

    api["cleanup_stale_workdirs"] = no_stale_work
    api["mark_self_test_stage"] = mark
    api["write_configs"] = write_configs
    api["MediaFailureDiagnostics"] = CrashDiagnostics
    install_reader_packet_trace(api, state)
    return state


def numeric_startup(value):
    keys = (
        "elapsed_ms",
        "spawn_ms",
        "post_spawn_ms",
        "reads",
        "failed",
        "absent",
        "present",
        "growth",
        "video_frames",
        "first_output_ms",
        "first_growth_ms",
        "max_read_ms",
    )
    source = value if isinstance(value, dict) else {}
    return {
        key: item if type(item := source.get(key)) is int and 0 <= item <= 10**9 else None
        for key in keys
    }


def prefix_summary(result, progress, state, code):
    require(isinstance(result, dict) and isinstance(progress, dict), "PREFIX_RESULT_SHAPE")
    clean = (
        result.get("workdir_removed") is True
        and not result.get("cleanup_failure")
        and type(result.get("secret_configs_wiped")) is int
        and (
            result["secret_configs_wiped"] >= 1
            or (
                result["secret_configs_wiped"] == 0
                and not state["initial_completed"]
                and state.get("last_stage") in {"startup", "assets"}
            )
        )
    )
    stopped_as_planned = (
        code == 1
        and state.get("target_armed") is True
        and state.get("target_completed") is True
        and result.get("failure") == STOP
    )
    return {
        "status": "TARGET_CRASH_PREFIX_COMPLETED"
        if stopped_as_planned and clean
        else "ORIGINAL_PREFIX_FAILURE",
        "target_phase": TARGET,
        "target_armed": state.get("target_armed") is True,
        "target_completed": state.get("target_completed") is True,
        "initial_prefix_completed": state["initial_completed"] is True,
        "acceptance": False,
        "attempts": 1,
        "original_exit": code,
        "initial_start_timeout": progress.get("failure_initial_live_reason")
        == "output-start-timeout",
        "initial_startup": numeric_startup(progress.get("failure_startup")),
        "cleanup_passed": clean,
        "media_oracle_or_deadline_changed": False,
        "diagnostic_log_level_variant": True,
    }


# Compatibility duplicate of the three pure CI projection helpers below.
# The isolated runner cannot import control-plane dependencies. AST-parity
# tests pin these bodies and marker sets to ci_node_onboarding_smoke.py.
_MEDIA_DIAGNOSTIC_MARKERS = frozenset(
    {
        "attached",
        "active",
        "detached",
        "child-exit",
        "start-timeout",
        "metrics-blind",
        "output-identity",
        "output-regression",
        "output-fallback",
        "ingest-timing",
        "ingest-missing",
        "ingest-identity",
        "ingest-regression",
        "verified-stall",
        "ingest-confirmed-stall",
        "watchdog-unknown",
        "reset-requested",
        "reset-succeeded",
    }
)
_MEDIA_FIRST_SEEN_MARKERS = frozenset({"attached", "active", "start-timeout", "child-exit"})


def _diagnostic_seconds(value: Any, maximum: float = 660) -> bool:
    # Compare the bound before isfinite so enormous JSON integers cannot overflow.
    return type(value) in {int, float} and 0 <= value <= maximum and math.isfinite(value)


def _safe_source_clock(value: Any) -> dict[str, Any] | None:
    """Project only one fixture sending episode, never transport identities."""
    if not isinstance(value, dict):
        return None
    if value == {"state": "unknown"}:
        return {"state": "unknown"}
    seconds = {"seconds", "last_age", "max_gap", "discarded"}
    if (
        value.keys() != {"state", "packets", "ratio", "rebases"} | seconds
        or value["state"] != "known"
        or type(value["packets"]) is not int
        or not 2 <= value["packets"] <= 1_000_000
        or type(value["rebases"]) is not int
        or not 0 <= value["rebases"] <= 1_000_000
        or any(not _diagnostic_seconds(value[name]) for name in seconds)
        or value["seconds"] < 1
        or value["max_gap"] > value["seconds"]
        or not _diagnostic_seconds(value["ratio"], 4)
    ):
        return None
    return {
        **{name: value[name] for name in ("state", "packets", "rebases")},
        **{name: round(value[name], 6) for name in (*sorted(seconds), "ratio")},
    }


def _safe_failure_media(value: Any) -> dict[str, Any] | None:
    """Reject the entire nested diagnostic on any non-schema value; never reflect text."""
    required = {"scope", "elapsed_seconds", "log_ok", "markers", "first_seen"}
    optional = {"supervisor_count", "child_count", "supervisor_seen_seconds", "child_seen_seconds"}
    reader = {"reader_input", "reader_output", "reader_frames"}
    reader_timings = {
        "reader_input_seconds": "reader_input",
        "reader_output_seconds": "reader_output",
        "reader_first_frame_seconds": "reader_frames",
        "reader_last_frame_seconds": "reader_frames",
    }
    probe_timings = ("reader_probe_start_seconds", "reader_probe_end_seconds")
    if not isinstance(value, dict) or not required <= value.keys():
        return None
    scope = value.get("scope")
    if not isinstance(scope, str) or scope not in {"crash", "capture"}:
        return None
    if scope == "capture":
        required |= reader
        optional |= (
            reader_timings.keys()
            | set(probe_timings)
            | {
                "reader_media_seconds",
                "reader_nal_events",
                "reader_inspection_limited",
                "source_clock",
            }
        )
    if not required <= value.keys() or not value.keys() <= required | optional:
        return None
    elapsed = value["elapsed_seconds"]
    if not _diagnostic_seconds(elapsed) or type(value["log_ok"]) is not bool:
        return None
    markers, first_seen = value["markers"], value["first_seen"]
    if (
        not isinstance(markers, dict)
        or not markers.keys() <= _MEDIA_DIAGNOSTIC_MARKERS
        or any(type(count) is not int or not 1 <= count <= 255 for count in markers.values())
        or not isinstance(first_seen, dict)
        or not first_seen.keys() <= _MEDIA_FIRST_SEEN_MARKERS & markers.keys()
        or any(not _diagnostic_seconds(seconds, elapsed) for seconds in first_seen.values())
    ):
        return None
    for name in ("supervisor_count", "child_count"):
        if name in value and (type(value[name]) is not int or not 0 <= value[name] <= 32):
            return None
    for name in ("supervisor_seen_seconds", "child_seen_seconds"):
        if name in value and not _diagnostic_seconds(value[name], elapsed):
            return None
    if scope == "capture" and (
        type(value["reader_input"]) is not bool
        or type(value["reader_output"]) is not bool
        or type(value["reader_frames"]) is not int
        or not 0 <= value["reader_frames"] <= 10000
    ):
        return None
    if scope == "capture":
        if "source_clock" in value and _safe_source_clock(value["source_clock"]) is None:
            return None
        if "reader_inspection_limited" in value and value["reader_inspection_limited"] is not True:
            return None
        if "reader_nal_events" in value:
            events = value["reader_nal_events"]
            if (
                not isinstance(events, dict)
                or not events
                or not events.keys() <= {"sps", "pps", "idr", "non_idr"}
            ):
                return None
            for event in events.values():
                if (
                    not isinstance(event, dict)
                    or event.keys() != {"count", "first_seconds", "last_seconds"}
                    or type(event["count"]) is not int
                    or not 1 <= event["count"] <= 255
                    or not _diagnostic_seconds(event["first_seconds"], elapsed)
                    or not _diagnostic_seconds(event["last_seconds"], elapsed)
                    or event["first_seconds"] > event["last_seconds"]
                    or (
                        "reader_input_seconds" in value
                        and _diagnostic_seconds(value["reader_input_seconds"], elapsed)
                        and event["last_seconds"] > value["reader_input_seconds"]
                    )
                ):
                    return None
        for name in probe_timings:
            if name in value and not _diagnostic_seconds(value[name], elapsed):
                return None
        if "reader_probe_end_seconds" in value and (
            "reader_probe_start_seconds" not in value
            or value["reader_probe_end_seconds"] < value["reader_probe_start_seconds"]
        ):
            return None
        for name, evidence in reader_timings.items():
            if name in value and (
                not value[evidence] or not _diagnostic_seconds(value[name], elapsed)
            ):
                return None
        if "reader_input_seconds" in value and any(
            value[name] > value["reader_input_seconds"] for name in probe_timings if name in value
        ):
            return None
        if (
            "reader_first_frame_seconds" in value
            and "reader_last_frame_seconds" in value
            and value["reader_first_frame_seconds"] > value["reader_last_frame_seconds"]
        ):
            return None
        # Buffered media can advance faster than the reader's wall clock.
        if "reader_media_seconds" in value and not _diagnostic_seconds(
            value["reader_media_seconds"]
        ):
            return None
    result = dict(value)
    result["elapsed_seconds"] = round(elapsed, 3)
    result["markers"] = dict(markers)
    result["first_seen"] = {name: round(seconds, 3) for name, seconds in first_seen.items()}
    if "source_clock" in value:
        result["source_clock"] = _safe_source_clock(value["source_clock"])
    if "reader_nal_events" in value:
        result["reader_nal_events"] = {
            name: {
                "count": event["count"],
                "first_seconds": round(event["first_seconds"], 3),
                "last_seconds": round(event["last_seconds"], 3),
            }
            for name, event in value["reader_nal_events"].items()
        }
    for name in (
        "supervisor_seen_seconds",
        "child_seen_seconds",
        *reader_timings,
        *probe_timings,
        "reader_media_seconds",
    ):
        if name in result:
            result[name] = round(result[name], 3)
    return result


def failure_evidence(progress):
    """Preserve already-collected fixed evidence, never diagnostic log strings."""
    result = {}
    media = _safe_failure_media(progress.get("failure_media"))
    if media is not None:
        result["failure_media"] = media
    flags = progress.get("failure_flags")
    allowed_flags = {
        "live",
        "normalized",
        "path_ready",
        "ingest_live",
        "metrics_ok",
        "core_alive",
        "ingest_one",
        "sink_one",
        "sink_growth",
        "state_ok",
        "ingest_match",
    }
    if (
        type(flags) is dict
        and flags
        and flags.keys() <= allowed_flags
        and all(type(value) is bool for value in flags.values())
    ):
        result["failure_flags"] = dict(flags)
    wait = progress.get("failure_wait_seconds")
    if _diagnostic_seconds(wait):
        result["failure_wait_seconds"] = round(wait, 3)
    return result


def failure_location(progress, allowed_stages):
    """Keep the original fixed checkpoint and source lines, never exception text."""
    source = progress if type(progress) is dict else {}
    stage = source.get("stage")
    lines = source.get("failure_lines")
    location = {
        "stage": stage if type(stage) is str and stage in allowed_stages else None,
        "failure_lines": lines
        if type(lines) is list
        and len(lines) <= 8
        and all(type(line) is int and 1 <= line <= 20000 for line in lines)
        else [],
    }
    if location["stage"] is not None:
        location.update(failure_evidence(source))
    return location


def main():
    require(
        os.environ.get("CI_NATIVE_STARTUP") == "isolated-fixture"
        and sys.platform == "linux"
        and os.geteuid() == 0
        and Path("/.dockerenv").is_file(),
        "ISOLATED_LINUX_CI_REQUIRED",
    )
    os.umask(0o077)
    os.environ.pop("MOBLIN_RELAY_SELF_TEST_STAGE_FILE", None)
    # Keep private evidence if original cleanup failed; CI container teardown owns
    # the residue. Never remove code still referenced by an unreaped child.
    with contextlib.nullcontext(tempfile.mkdtemp(prefix="adojapan-ci-startup-")) as temporary:
        stage = Path(temporary)
        hashes = stage_sources(stage)
        require(
            stage.parent == Path("/tmp")  # noqa: S108 - unique root-private mkdtemp
            and re.fullmatch(r"adojapan-ci-startup-[A-Za-z0-9_-]{8,64}", stage.name),
            "PRIVATE_STAGE_PATH",
        )
        stage_noexec = bool(os.statvfs(stage).f_flag & os.ST_NOEXEC)
        # Verified image artifact, independent of successful native installation.
        reader = runpy.run_path(str(stage / "reader.py"), run_name="_startup_reader_verifier")
        reader["verify_reader_binary"]()
        version = subprocess.run(  # noqa: S603 - fixed local binary, no credentials
            ["/usr/bin/ffmpeg", "-version"],
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout.splitlines()[0]
        match = re.match(rb"ffmpeg version ([A-Za-z0-9.:+~_-]{1,80}) ", version)
        require(match is not None, "FFMPEG_VERSION_UNAVAILABLE")
        slate = subprocess.run(  # noqa: S603 - generated private asset only
            slate_command(stage),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        require(slate.returncode == 0, "SLATE_GENERATION_FAILED")
        namespace = runpy.run_path(str(stage / "self-test"), run_name="_startup_prefix")
        api = namespace["main"].__globals__
        stage_file = Path(f"/run/moblin-relay-self-test.{uuid.uuid4()}.stage")
        save_new(stage_file, b"startup\n")
        stage_identity = stage_file.stat()
        state = install_prefix(api, stage, reader["MEDIAMTX"], stage_file)
        timeline_api = runpy.run_path(
            str(stage / "wrapper.py"), run_name="_pause_timeline_validator"
        )
        pause_timeline = install_pause_timeline(api, state, stage, timeline_api)
        sys.argv = [str(stage / "self-test")]
        # Never expose the original diagnostic log or synthetic credentials.
        try:
            with (
                open(os.devnull, "w") as silent,
                contextlib.redirect_stdout(silent),
                contextlib.redirect_stderr(silent),
            ):
                code = namespace["main"]()
        finally:
            current = stage_file.lstat()
            require(
                (current.st_dev, current.st_ino) == (stage_identity.st_dev, stage_identity.st_ino)
                and stat.S_ISREG(current.st_mode)
                and current.st_nlink == 1,
                "STAGE_IDENTITY_CHANGED",
            )
            stage_file.unlink()
        result = json.loads(read_private(stage / "prefix-result.json", maximum=2 * 1024**2))
        progress = json.loads(read_private(stage / "prefix-progress.json", maximum=2 * 1024**2))
        with contextlib.suppress(Exception):
            state["freeze_pause_timeline"]()
        report = prefix_summary(result, progress, state, code)
        with contextlib.suppress(Exception):
            report["stall_switch_timeline"] = pause_timeline.report(
                include_rows=report["status"] != "TARGET_CRASH_PREFIX_COMPLETED"
            )
        report["stage_noexec"] = stage_noexec
        report["hook_launch"] = "python-interpreter-noexec-compatible"
        report["checkpoint"] = failure_location(progress, api["SELF_TEST_STAGES"])
        report["source_hashes"] = hashes
        report["ffmpeg_version"] = match[1].decode("ascii")
        if (stage / "normalizer-phases.json").exists():
            wrapper = runpy.run_path(str(stage / "wrapper.py"), run_name="_startup_phase_validator")
            phases = json.loads(
                read_private(stage / "normalizer-phases.json", maximum=4096),
                object_pairs_hook=wrapper["unique_object"],
            )
            report["target_first_child_phases"] = wrapper["validated_report"](phases)
        else:
            report["target_first_child_phases"] = None
        report["phase_evidence_available"] = report["target_first_child_phases"] is not None
        report["startup_input"] = api["safe_startup_input_failure"](
            progress.get("failure_startup_input")
        )
        report["reader_packet_trace"] = None
        with contextlib.suppress(Exception):
            report["reader_packet_trace"] = failed_reader_packet_trace(api, state, progress)
    report["private_stage_removed"] = False
    if report["cleanup_passed"]:
        require(
            stage.resolve(strict=True) == stage and not stage.is_symlink(), "STAGE_IDENTITY_CHANGED"
        )
        shutil.rmtree(stage)
        report["private_stage_removed"] = True
    print(
        json.dumps({"native_startup_diagnostic": report}, allow_nan=False, separators=(",", ":")),
        flush=True,
    )
    return (
        0
        if report["status"] == "TARGET_CRASH_PREFIX_COMPLETED"
        and report["phase_evidence_available"]
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        code = str(error) if isinstance(error, DiagnosticFailure) else type(error).__name__
        print(
            json.dumps(
                {
                    "native_startup_diagnostic": {
                        "status": "SETUP_OR_COLLECTION_FAILURE",
                        "code": code,
                    }
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
