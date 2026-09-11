#!/usr/bin/python3
"""Private CI diagnostic delegate; never installed as the production normalizer.

The only media-argv difference is first-child ``-loglevel error`` -> ``debug``.
Raw stderr exists only in a bounded private pipe/parser buffer and is discarded.
Milestone times are collector receipt times, not packet, decode or scheduling proof.
The normalizer's admission, stdout progress, six-second gate and retries are unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

PURPOSE = "adojapan-ci-native-startup-diagnostic"
# Exact private CI namespace, additionally checked for root ownership and mode 0700.
STAGE_PATTERN = re.compile(r"/tmp/adojapan-ci-startup-[A-Za-z0-9_-]{8,64}\Z")  # noqa: S108
MAX_SOURCE_BYTES = 256 * 1024
MAX_LINE_BYTES = 2048
MAX_PARSE_BYTES = 256 * 1024
MAX_DRAIN_BYTES = 64 * 1024
MAX_EVENT_COUNT = 4096
MAX_RECORD_BYTES = 4096
WINDOW_MS = 20_000
EVENTS = {
    "input_open": re.compile(rb"Opening an input file:"),
    "input_info": re.compile(rb"Input #0, rtsp,"),
    "output_open": re.compile(rb"Opening an output file:"),
    "output_info": re.compile(rb"Output #0, flv,"),
    "rtmp_handshake": re.compile(rb"Handshaking\.\.\."),
    "rtmp_create": re.compile(rb"Creating stream\.\.\."),
    "rtmp_publish": re.compile(rb"Sending publish command for"),
    "info_complete": re.compile(rb"All info found"),
    "info_before_optional": re.compile(rb"Before avformat_find_stream_info\(\)"),
    "info_after_optional": re.compile(rb"After avformat_find_stream_info\(\)"),
    "analyze_limit": re.compile(
        rb"max_analyze_duration [0-9]+ reached at [0-9]+ microseconds st:[0-9]+"
    ),
    "probe_limit": re.compile(rb"Probe buffer size limit of [0-9]+ bytes reached"),
    "missing_codec": re.compile(rb"Could not find codec parameters for stream"),
    "missing_pps": re.compile(rb"non-existing PPS [0-9]+ referenced"),
    "missing_picture": re.compile(rb"missing picture in access unit with size [0-9]+"),
    "nal_sps": re.compile(rb"nal_unit_type:\s*7\b"),
    "nal_pps": re.compile(rb"nal_unit_type:\s*8\b"),
    "nal_idr": re.compile(rb"nal_unit_type:\s*5\b"),
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def trusted_file(path, mode, maximum):
    """Read a single root-owned no-follow file without pathname/race substitution."""
    before = path.lstat()
    require(
        stat.S_ISREG(before.st_mode)
        and before.st_uid == 0
        and before.st_nlink == 1
        and stat.S_IMODE(before.st_mode) == mode
        and 0 <= before.st_size <= maximum,
        "CI_DIAGNOSTIC_FILE_INVALID",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        require(
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
                opened.st_nlink,
                opened.st_size,
            )
            == (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
                before.st_size,
            ),
            "CI_DIAGNOSTIC_FILE_CHANGED",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
        require(len(raw) == opened.st_size, "CI_DIAGNOSTIC_FILE_SIZE_CHANGED")
        return raw
    finally:
        os.close(descriptor)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "CI_DIAGNOSTIC_DUPLICATE_KEY")
        result[key] = value
    return result


def load_stage(wrapper):
    stage = wrapper.parent
    require(
        os.geteuid() == 0 and STAGE_PATTERN.fullmatch(str(stage)), "CI_DIAGNOSTIC_STAGE_REQUIRED"
    )
    metadata = stage.lstat()
    require(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and stage.resolve(strict=True) == stage
        and wrapper.name == "wrapper.py",
        "CI_DIAGNOSTIC_STAGE_INVALID",
    )
    manifest = json.loads(
        trusted_file(stage / "manifest.json", 0o600, 4096), object_pairs_hook=unique_object
    )
    require(
        type(manifest) is dict
        and set(manifest) == {"version", "purpose", "normalizer_sha256", "wrapper_sha256"}
        and type(manifest["version"]) is int
        and manifest["version"] == 1
        and manifest["purpose"] == PURPOSE,
        "CI_DIAGNOSTIC_MANIFEST_INVALID",
    )
    for key in ("normalizer_sha256", "wrapper_sha256"):
        require(
            type(manifest[key]) is str and re.fullmatch(r"[0-9a-f]{64}", manifest[key]),
            "CI_DIAGNOSTIC_PIN_INVALID",
        )
    source = trusted_file(stage / "normalizer.py", 0o600, MAX_SOURCE_BYTES)
    own_source = trusted_file(wrapper, 0o755, MAX_SOURCE_BYTES)
    require(
        hashlib.sha256(source).hexdigest() == manifest["normalizer_sha256"]
        and hashlib.sha256(own_source).hexdigest() == manifest["wrapper_sha256"],
        "CI_DIAGNOSTIC_PIN_CHANGED",
    )
    return stage, source


def claim_capture(stage):
    """Arm before the crash; only a new supervisor may claim its first child."""
    try:
        armed = trusted_file(stage / "normalizer-capture.arm", 0o600, 64)
    except FileNotFoundError:
        return False
    match = re.fullmatch(rb"crash-first-child\n([1-9][0-9]{0,9})\n", armed)
    require(match is not None and int(match[1]) <= 2**31 - 1, "CI_DIAGNOSTIC_ARM_INVALID")
    if os.getpid() == int(match[1]):
        return False
    path = stage / "normalizer-capture.claim"
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except FileExistsError:
        trusted_file(path, 0o600, 0)
        return False
    os.close(descriptor)
    return True


def atomic_record(stage, record):
    safe = validated_report(record)
    require(safe is not None, "CI_DIAGNOSTIC_RECORD_INVALID")
    payload = json.dumps(safe, allow_nan=False, separators=(",", ":")).encode("ascii")
    require(len(payload) <= MAX_RECORD_BYTES, "CI_DIAGNOSTIC_RECORD_TOO_LARGE")
    descriptor, temporary = tempfile.mkstemp(prefix=".normalizer-phases-", dir=stage)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, stage / "normalizer-phases.json")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


class Milestones:
    """Fixed-schema parser; parse limits never stop draining the child's pipe."""

    def __init__(self, started, clock=time.monotonic):
        self.started = started
        self.clock = clock
        self.pending = bytearray()
        self.discard_line = False
        self.record = {
            "version": 1,
            "scope": "first-child-collector-receipt-not-decode-proof",
            "parse_bytes": 0,
            "byte_cap_exceeded": False,
            "line_cap_exceeded": False,
            "event_cap_exceeded": False,
            "clock_invalid": False,
            "outside_window": False,
            "reader_error": False,
            "drain_cap_reached": False,
            "eof": False,
            "child_stopped": False,
            "first_child_spawned": False,
            "spawn_failed": False,
            "first_child_timeout": False,
            "first_child_bridge_active": False,
            "first_video_progress_ms": None,
            "max_video_frames": 0,
            "events": {name: {"count": 0, "first_ms": None, "last_ms": None} for name in EVENTS},
        }

    def feed(self, chunk):
        self.record["parse_bytes"] = min(
            MAX_PARSE_BYTES + 1, self.record["parse_bytes"] + len(chunk)
        )
        if self.record["parse_bytes"] > MAX_PARSE_BYTES:
            self.record["byte_cap_exceeded"] = True
            self.pending.clear()
            return
        for part in chunk.splitlines(keepends=True):
            # FFmpeg records use LF. A CR inside a record cannot create a new marker.
            complete = part.endswith(b"\n")
            if self.discard_line:
                if complete:
                    self.discard_line = False
                continue
            if len(self.pending) + len(part) > MAX_LINE_BYTES:
                self.record["line_cap_exceeded"] = True
                self.pending.clear()
                self.discard_line = not complete
                continue
            self.pending.extend(part)
            if complete:
                self.line(bytes(self.pending))
                self.pending.clear()

    def line(self, raw):
        elapsed = (self.clock() - self.started) * 1000
        if not math.isfinite(elapsed) or elapsed < 0:
            self.record["clock_invalid"] = True
            return
        if elapsed > WINDOW_MS:
            self.record["outside_window"] = True
            return
        stamp = round(elapsed)
        for name, pattern in EVENTS.items():
            if not pattern.search(raw):
                continue
            event = self.record["events"][name]
            if event["last_ms"] is not None and stamp < event["last_ms"]:
                self.record["clock_invalid"] = True
                continue
            if event["count"] >= MAX_EVENT_COUNT:
                self.record["event_cap_exceeded"] = True
                continue
            event["count"] += 1
            if event["first_ms"] is None:
                event["first_ms"] = stamp
            event["last_ms"] = stamp

    def drain(self, pipe):
        consumed = 0
        try:
            while consumed < MAX_DRAIN_BYTES:
                try:
                    chunk = os.read(pipe.fileno(), min(8192, MAX_DRAIN_BYTES - consumed))
                except BlockingIOError:
                    return
                if not chunk:
                    self.record["eof"] = True
                    self.pending.clear()
                    return
                consumed += len(chunk)
                self.feed(chunk)
            self.record["drain_cap_reached"] = True
        except (OSError, ValueError, OverflowError, TypeError):
            self.record["reader_error"] = True


def validated_report(value):
    """Return a new allowlisted report or None; never relay arbitrary diagnostic text."""
    shape = Milestones(0).record
    if type(value) is not dict or set(value) != set(shape):
        return None
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(value["scope"]) is not str
        or value["scope"] != shape["scope"]
        or type(value["parse_bytes"]) is not int
        or not 0 <= value["parse_bytes"] <= MAX_PARSE_BYTES + 1
    ):
        return None
    frames, first_video = value["max_video_frames"], value["first_video_progress_ms"]
    if (
        type(frames) is not int
        or not 0 <= frames <= 10**9
        or (frames == 0 and first_video is not None)
        or (frames > 0 and (type(first_video) is not int or not 0 <= first_video <= WINDOW_MS))
    ):
        return None
    flags = set(shape) - {
        "version",
        "scope",
        "parse_bytes",
        "events",
        "first_video_progress_ms",
        "max_video_frames",
    }
    if any(type(value[name]) is not bool for name in flags):
        return None
    events = value["events"]
    if type(events) is not dict or set(events) != set(EVENTS):
        return None
    safe_events = {}
    for name in EVENTS:
        event = events[name]
        if type(event) is not dict or set(event) != {"count", "first_ms", "last_ms"}:
            return None
        count, first, last = event["count"], event["first_ms"], event["last_ms"]
        if type(count) is not int or not 0 <= count <= MAX_EVENT_COUNT:
            return None
        if count == 0:
            if first is not None or last is not None:
                return None
        elif type(first) is not int or type(last) is not int or not 0 <= first <= last <= WINDOW_MS:
            return None
        safe_events[name] = {"count": count, "first_ms": first, "last_ms": last}
    safe = {name: value[name] for name in shape if name != "events"}
    safe["events"] = safe_events
    payload = json.dumps(safe, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_RECORD_BYTES:
        return None
    return safe


def install_capture(api, stage, clock=time.monotonic):
    """Namespace-only hooks: no global subprocess patch, reader threads or extra waits."""
    original_subprocess = api["subprocess"]
    original_progress = api["VideoProgress"]
    original_stop = api["stop_child"]
    original_restart = api["emit_restart_reason"]
    original_state = api["emit_state_event"]
    state = {"attempted": False, "child": None, "parser": None, "finished": False}

    def finish():
        if state["finished"] or state["parser"] is None:
            return
        parser = state["parser"]
        child = state["child"]
        if child is not None:
            parser.drain(child.stderr)
            parser.record["child_stopped"] = child.poll() is not None
            child.stderr.close()
        parser.pending.clear()
        state["finished"] = True
        # No raw stderr/exception/argv/env is ever passed to the writer.
        atomic_record(stage, parser.record)

    class SubprocessProxy:
        def __getattr__(self, name):
            return getattr(original_subprocess, name)

        def Popen(self, argv, **kwargs):  # noqa: N802 - exact subprocess API.
            if state["attempted"]:
                return original_subprocess.Popen(argv, **kwargs)
            state["attempted"] = True
            expected = api["build_ffmpeg_argv"](
                api["validated_port"](os.environ.get(api["SAFE_RTSP_PORT_ENV"], "")),
                api["validated_port"](os.environ.get(api["SAFE_RTMP_PORT_ENV"], "")),
            )
            require(
                argv == expected
                and set(kwargs) == {"stdin", "stdout", "env", "close_fds", "preexec_fn"}
                and kwargs["stdin"] == original_subprocess.DEVNULL
                and kwargs["stdout"] == original_subprocess.PIPE
                and kwargs["env"] == {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
                and kwargs["close_fds"] is True
                and callable(kwargs["preexec_fn"]),
                "CI_DIAGNOSTIC_CHILD_CHANGED",
            )
            value = list(argv)
            require(value.count("-loglevel") == 1, "CI_DIAGNOSTIC_LOG_LEVEL_COUNT")
            index = value.index("-loglevel") + 1
            require(value[index] == "error", "CI_DIAGNOSTIC_LOG_LEVEL_CHANGED")
            value[index] = "debug"
            parser = Milestones(clock(), clock)
            state["parser"] = parser
            try:
                child = original_subprocess.Popen(value, stderr=original_subprocess.PIPE, **kwargs)
            except (OSError, original_subprocess.SubprocessError):
                parser.record["spawn_failed"] = True
                raise
            state["child"] = child
            parser.record["first_child_spawned"] = True
            try:
                os.set_blocking(child.stderr.fileno(), False)
            except (OSError, ValueError):
                # Popen has not returned to the supervisor: it cannot yet own this
                # child in its finally block. Reap via its original bounded helper.
                parser.record["reader_error"] = True
                original_stop(child, force=True)
                child.stderr.close()
                raise OSError("CI_DIAGNOSTIC_PIPE_SETUP_FAILED") from None
            return child

    class Progress(original_progress):
        def sample(self, now):
            result = super().sample(now)
            if state["child"] is not None and not state["finished"]:
                parser = state["parser"]
                parser.drain(state["child"].stderr)
                # Observe the original sample only: no extra progress read/poll.
                frames = getattr(self, "frames", 0)
                elapsed = (now - parser.started) * 1000
                if type(frames) is int and 0 < frames <= 10**9 and 0 <= elapsed <= WINDOW_MS:
                    if parser.record["first_video_progress_ms"] is None:
                        parser.record["first_video_progress_ms"] = round(elapsed)
                    parser.record["max_video_frames"] = max(
                        parser.record["max_video_frames"], frames
                    )
            return result

        def close(self):
            result = super().close()
            # A natural child exit bypasses stop_child in the original supervisor.
            # Freeze this epoch before a later child's events can be misattributed.
            if state["child"] is not None and self.pipe is state["child"].stdout:
                with contextlib.suppress(OSError, ValueError):
                    finish()
            return result

    def stop(child, *, force=False):
        result = original_stop(child, force=force)
        if child is state["child"]:
            # An absent diagnostic never replaces an original DUT failure.
            with contextlib.suppress(OSError, ValueError):
                finish()
        return result

    def restart(reason):
        original_restart(reason)
        if not state["finished"] and state["parser"] is not None:
            state["parser"].record["first_child_timeout"] |= (
                reason == api["RESTART_REASON_OUTPUT_START_TIMEOUT"]
            )

    def event(value):
        original_state(value)
        if not state["finished"] and state["parser"] is not None:
            state["parser"].record["first_child_bridge_active"] |= (
                value == api["STATE_EVENT_BRIDGE_ACTIVE"]
            )

    api["subprocess"] = SubprocessProxy()
    api["VideoProgress"] = Progress
    api["stop_child"] = stop
    api["emit_restart_reason"] = restart
    api["emit_state_event"] = event
    return finish


def delegate(wrapper, stage, source):
    api = {"__name__": "_ci_original_normalizer", "__file__": str(wrapper)}
    exec(compile(source, str(stage / "normalizer.py"), "exec"), api)  # noqa: S102
    # The original unsanitized entrypoint re-execs this wrapper before hooks are installed.
    # Its real supervisor argv therefore still matches self-test's NORMALIZER identity.
    if sys.argv[1:] != [api["SUPERVISOR_ARGUMENT"]] or not claim_capture(stage):
        return api["main"]()
    finish = install_capture(api, stage)
    try:
        return api["main"]()
    finally:
        with contextlib.suppress(OSError, ValueError):
            finish()


def main():
    try:
        wrapper = Path(__file__).absolute()
        stage, source = load_stage(wrapper)
    except (OSError, ValueError, TypeError):
        print("CI startup diagnostic unavailable", file=sys.stderr)
        return 2
    return delegate(wrapper, stage, source)


if __name__ == "__main__":
    raise SystemExit(main())
