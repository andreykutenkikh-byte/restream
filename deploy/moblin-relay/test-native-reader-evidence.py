#!/usr/bin/env python3
"""Opt-in numeric evidence for synthetic strict reader #4; never an oracle."""

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

LIMIT = 256
PARTIAL_LIMIT = 32 * 1024 * 1024
REPORT_LIMIT = 128 * 1024
# Event codes: capture, spawn call/return, wait call/return/timeout/error,
# poll return, kill call/return, reader completed, retain requested.
EVENTS = tuple(range(1, 13))
OBSERVER_FIELDS = (
    "t_ns",
    "finished_ns",
    "flags",
    "ingest_generation",
    "normalized_generation",
    "sink_generation",
    "ingest_bytes",
    "normalized_bytes",
    "sink_bytes",
    "capture_size",
)
# flags bits: DUT/sink metrics valid, live, normalized, ingest_live, path_ready,
# DUT/sink/continuous-reader alive. capture_size belongs to the EXISTING RTSP reader.
OBSERVER_FLAGS = (
    "dut_metrics_ok",
    "sink_metrics_ok",
    "live",
    "normalized",
    "ingest_live",
    "path_ready",
    "dut_alive",
    "sink_alive",
    "reader_alive",
)


def owned(path, *, directory=False, source=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode)
        not in ({0o700} if directory else {0o600, 0o644} if source else {0o600})
        or (not directory and info.st_nlink != 1)
        or path.resolve() != path.absolute()
    ):
        raise ValueError("invalid private evidence path")
    return info


def stable(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
        info.st_mode,
        info.st_uid,
    )


def observer_rows(observer, anchor):
    """Reuse completed samples after capture; never poll or export raw identities."""
    if observer is None:
        return []
    with observer.lock:
        samples = list(observer.samples[-128:])
    result, generations = [], [{}, {}, {}]
    for sample in samples:
        times = [sample.get(key) for key in ("t", "finished")]
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in times):
            continue
        times = [round(value * 1e9) - anchor for value in times]
        if not -30_000_000_000 <= times[0] <= times[1] <= 660_000_000_000:
            continue
        flags = sum(
            1 << index for index, name in enumerate(OBSERVER_FLAGS) if sample.get(name) is True
        )
        identities = []
        for index, role in enumerate(("ingest", "normalized", "sink")):
            ids = sample.get(role + "_ids")
            generation = None
            if ids == []:
                generation = 0
            elif (
                isinstance(ids, list)
                and len(ids) == 1
                and isinstance(ids[0], str)
                and 0 < len(ids[0]) <= 128
            ):
                mapping = generations[index]
                generation = mapping.setdefault(ids[0], len(mapping) + 1)
            identities.append(generation)
        counts = [sample.get(name) for name in OBSERVER_FIELDS[6:]]
        counts = [value if type(value) is int and 0 <= value < 2**63 else None for value in counts]
        result.append([*times, flags, *identities, *counts])
    return result


def capture_rows(observer, anchor):
    if observer is None:
        return []
    with observer.lock:
        samples = list(observer.samples[-512:])
    result = []
    for sample in samples:
        when, size = sample.get("t"), sample.get("capture_size")
        if (
            type(when) in {int, float}
            and math.isfinite(when)
            and type(size) is int
            and 0 <= size < 2**63
        ):
            relative = round(when * 1e9) - anchor
            if -30_000_000_000 <= relative <= 660_000_000_000:
                result.append([relative, sample.get("capture_ok") is True, size])
    return result


def reader_snapshot(reader):
    values, result = reader.snapshot(), {}
    for key in ("reader_input", "reader_output", "reader_inspection_limited"):
        if type(values.get(key)) is bool:
            result[key] = values[key]
    frames = values.get("reader_frames")
    if type(frames) is int and 0 <= frames <= 10000:
        result["reader_frames"] = frames
    for label in (
        "input",
        "output",
        "probe_start",
        "probe_end",
        "first_frame",
        "last_frame",
        "media",
    ):
        key = "reader_" + label + "_seconds"
        value = values.get(key)
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 660:
            result[key] = value
    return result


def binary_snapshot(api):
    result = {}
    for name in ("FFMPEG", "MEDIAMTX"):
        result[name.lower()] = None
        try:
            path = Path(api[name])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024 * 1024:
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if stable(os.fstat(stream.fileno())) != stable(info):
                    continue
                digest, count = hashlib.sha256(), 0
                deadline = time.monotonic_ns() + 2_000_000_000
                while chunk := stream.read(65536):
                    count += len(chunk)
                    if count > info.st_size or time.monotonic_ns() >= deadline:
                        raise ValueError("executable hash bound exceeded")
                    digest.update(chunk)
                if count == info.st_size and stable(os.fstat(stream.fileno())) == stable(info):
                    result[name.lower()] = {"bytes": count, "sha256": digest.hexdigest()}
        except (OSError, ValueError, KeyError):
            pass
    return result


def cgroup_snapshot():
    """Fixed cgroup-v2 paths only; absent/unsupported values remain unknown."""
    result = {}
    try:
        for name in ("cpu.stat", "cpu.max", "memory.max", "memory.current", "pids.max"):
            with (Path("/sys/fs/cgroup") / name).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                continue
            if name == "cpu.stat":
                allowed = {
                    "usage_usec",
                    "user_usec",
                    "system_usec",
                    "nr_periods",
                    "nr_throttled",
                    "throttled_usec",
                }
                for line in raw.splitlines():
                    parts = line.split()
                    if (
                        len(parts) == 2
                        and parts[0].decode("ascii") in allowed
                        and re.fullmatch(rb"[0-9]{1,18}", parts[1])
                    ):
                        result[parts[0].decode("ascii")] = int(parts[1])
            else:
                values = raw.split()
                if all(
                    value == b"max" or re.fullmatch(rb"[0-9]{1,18}", value) for value in values
                ) and len(values) == (2 if name == "cpu.max" else 1):
                    result[name.replace(".", "_")] = [
                        None if value == b"max" else int(value) for value in values
                    ]
    except (OSError, ValueError, UnicodeError):
        pass
    return result


def atomic_report(stage, value):
    owned(stage, directory=True)
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > REPORT_LIMIT:
        raise ValueError("evidence report exceeds bound")
    temporary = stage / "report.pending"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, stage / "report.json")
    finally:
        temporary.unlink(missing_ok=True)


def install(api, stage, manifest):
    """Caller verifies isolated marker and executable hashes before installation."""
    stage = Path(stage)
    owned(stage, directory=True)
    original_reader = api["CaptureReaderProgress"]
    original_run = api["run_capture_reader"]
    scope = original_run.__globals__
    original_subprocess = scope["subprocess"]
    context = threading.local()
    source = {
        key: value
        for key, value in manifest.items()
        if key in {"source_sha", "source_tree", "helper_sha256", "self_test_sha256"}
        and isinstance(value, str)
        and re.fullmatch(
            r"[0-9a-f]{40}" if key in {"source_sha", "source_tree"} else r"[0-9a-f]{64}", value
        )
    }

    class EvidenceReader(original_reader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.active = False
            self.rows = deque(maxlen=LIMIT)
            self.progress_rows = deque(maxlen=LIMIT)
            self.dropped = 0
            self.progress_dropped = 0
            self.evidence_lock = threading.Lock()
            self.pipes = [
                {"bytes": 0, "reads": 0, "last_ns": None, "eof": False, "handler_max_ns": 0}
                for _ in range(2)
            ]
            self.outcome = 0
            self.returncode = None
            self.reaped = False
            self.observed = []
            self.captured = []
            self.binaries = {}
            self.reader_values = {}
            self.artifact = {
                "state": 0,
                "bytes": 0,
                "sha256": None,
                "forced": False,
                "incomplete": True,
                "validator": 0,
            }

        def now(self):
            return max(0, min(time.monotonic_ns() - self.anchor, 660_000_000_000))

        def event(self, code, value=0):
            with contextlib.suppress(Exception), self.evidence_lock:
                self.dropped += int(len(self.rows) == LIMIT)
                self.rows.append([self.now(), code, value])

        def begin_capture(self, path, index, observer=None):
            self.active = (
                index == 4 and scope.get("SELF_TEST_LAST_PROGRESS", {}).get("stage") == "stuck-live"
            )
            if not self.active:
                return
            self.path = Path(path)
            self.observer = observer or getattr(self, "observer", None)
            self.anchor = (
                round(self.started * 1e9) if self.started is not None else time.monotonic_ns()
            )
            self.event(1)

        def drain(self, pipe, *, progress):
            if not self.active:
                return super().drain(pipe, progress=progress)
            diagnostic = self
            slot = 0 if progress else 1

            class Pipe:
                def read(self, count):
                    value = pipe.read(count)
                    with contextlib.suppress(Exception), diagnostic.evidence_lock:
                        row = diagnostic.pipes[slot]
                        row["reads"] = min(row["reads"] + 1, 2**63 - 1)
                        row["bytes"] = min(row["bytes"] + len(value), 2**63 - 1)
                        row["last_ns"] = diagnostic.now()
                        row["eof"] = not bool(value)
                    return value

            return super().drain(Pipe(), progress=progress)

        def observe_line(self, line, *, progress):
            if not self.active:
                return super().observe_line(line, progress=progress)
            started = time.monotonic_ns()
            try:
                return super().observe_line(line, progress=progress)
            finally:
                with contextlib.suppress(Exception), self.evidence_lock:
                    elapsed = time.monotonic_ns() - started
                    slot = self.pipes[0 if progress else 1]
                    slot["handler_max_ns"] = max(slot["handler_max_ns"], elapsed)
                    if progress:
                        current = [
                            self.values["reader_frames"],
                            round(self.values.get("reader_media_seconds", 0) * 1e6),
                        ]
                        if not self.progress_rows or self.progress_rows[-1][1:] != current:
                            self.progress_dropped += int(len(self.progress_rows) == LIMIT)
                            self.progress_rows.append([self.now(), *current])

        def persist(self):
            with contextlib.suppress(Exception):
                with self.evidence_lock:
                    value = {
                        "version": 1,
                        "segment": 4,
                        "source": source,
                        "outcome": self.outcome,
                        "returncode": self.returncode,
                        "reaped": self.reaped,
                        "events": list(self.rows),
                        "events_dropped": self.dropped,
                        "progress": list(self.progress_rows),
                        "progress_dropped": self.progress_dropped,
                        "pipes": [dict(row) for row in self.pipes],
                        "cgroup_before": self.before,
                        "cgroup_after": self.after,
                        "observer": self.observed,
                        "capture_observer": self.captured,
                        "reader": self.reader_values,
                        "binaries": self.binaries,
                        "artifact": dict(self.artifact),
                    }
                atomic_report(stage, value)
                return True
            return False

        def retain_failure(self, path, _exc):
            if not self.active or not self.reaped or Path(path) != self.path:
                return
            self.event(12)
            if self.outcome == 1:
                self.outcome = 4
            if not self.persist():  # Persist original failure before optional artifact I/O.
                return
            target = stage / "partial.flv"
            created = False
            try:
                owned(self.path.parent, directory=True)
                if self.path.name != "sink-proof-004.flv":
                    raise ValueError("unexpected capture filename")
                info = owned(self.path, source=True)
                if not 0 < info.st_size <= PARTIAL_LIMIT:
                    raise ValueError("invalid partial size")
                source_fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, "rb") as source_stream:
                    if stable(os.fstat(source_stream.fileno())) != stable(info):
                        raise ValueError("partial identity changed")
                    os.fchmod(source_stream.fileno(), 0o600)
                    info = os.fstat(source_stream.fileno())
                    owned(stage, directory=True)
                    target_fd = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    created = True
                    digest = hashlib.sha256()
                    total = 0
                    deadline = time.monotonic_ns() + 2_000_000_000
                    with os.fdopen(target_fd, "wb") as destination:
                        while chunk := source_stream.read(65536):
                            total += len(chunk)
                            if total > PARTIAL_LIMIT or time.monotonic_ns() >= deadline:
                                raise ValueError("partial copy bound exceeded")
                            digest.update(chunk)
                            destination.write(chunk)
                    if (
                        total != info.st_size
                        or stable(os.fstat(source_stream.fileno())) != stable(info)
                        or stable(self.path.lstat()) != stable(info)
                    ):
                        raise ValueError("partial changed during copy")
                self.artifact.update(state=1, bytes=total, sha256=digest.hexdigest())
            except Exception:
                self.artifact["state"] = 2
                if created:
                    with contextlib.suppress(OSError):
                        target.unlink()
            self.persist()

    class Child:
        def __init__(self, child, diagnostic):
            self.child, self.diagnostic = child, diagnostic

        def __getattr__(self, key):
            return getattr(self.child, key)

        def wait(self, *args, **kwargs):
            self.diagnostic.event(4, round(kwargs.get("timeout", 0) * 1e9))
            try:
                result = self.child.wait(*args, **kwargs)
            except subprocess.TimeoutExpired:
                self.diagnostic.event(6)
                raise
            except Exception:
                self.diagnostic.event(7)
                raise
            self.diagnostic.reaped = True
            self.diagnostic.returncode = result
            self.diagnostic.event(5, result)
            return result

        def poll(self):
            result = self.child.poll()
            self.diagnostic.event(8, -9999 if result is None else result)
            return result

        def kill(self):
            self.diagnostic.event(9)
            result = self.child.kill()
            self.diagnostic.artifact["forced"] = True
            self.diagnostic.event(10)
            return result

    class Subprocess:
        def __getattr__(self, key):
            return getattr(original_subprocess, key)

        def Popen(self, *args, **kwargs):
            diagnostic = getattr(context, "diagnostic", None)
            if diagnostic is None:
                return original_subprocess.Popen(*args, **kwargs)
            diagnostic.event(2)
            child = original_subprocess.Popen(*args, **kwargs)
            diagnostic.event(3)
            return Child(child, diagnostic)

    def run_reader(command, diagnostic, *, timeout):
        if not isinstance(diagnostic, EvidenceReader) or not diagnostic.active:
            return original_run(command, diagnostic, timeout=timeout)
        diagnostic.before = {}
        with contextlib.suppress(Exception):
            diagnostic.before = cgroup_snapshot()
        diagnostic.after = {}
        context.diagnostic = diagnostic
        try:
            result = original_run(command, diagnostic, timeout=timeout)
            diagnostic.outcome = 1 if result.returncode == 0 else 3
            return result
        except subprocess.TimeoutExpired:
            diagnostic.outcome = 2
            raise
        except Exception:
            diagnostic.outcome = 3
            raise
        finally:
            context.diagnostic = None
            diagnostic.event(11)
            with contextlib.suppress(Exception):
                diagnostic.after = cgroup_snapshot()
                diagnostic.reader_values = reader_snapshot(diagnostic)
            with contextlib.suppress(Exception):
                diagnostic.observed = observer_rows(
                    getattr(diagnostic, "observer", None), diagnostic.anchor
                )
                diagnostic.captured = capture_rows(
                    getattr(diagnostic, "capture_observer", None), diagnostic.anchor
                )
            with contextlib.suppress(Exception):
                diagnostic.binaries = binary_snapshot(scope)
            diagnostic.persist()

    original_run.__globals__["subprocess"] = Subprocess()
    for target in (api, scope):
        target["CaptureReaderProgress"] = EvidenceReader
        target["run_capture_reader"] = run_reader
