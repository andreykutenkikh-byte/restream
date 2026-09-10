#!/usr/bin/python3
"""CI-only source-clock regression and unchanged strict-reader media acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import runpy
import socket
import stat
import subprocess
import threading
import time
from collections import deque
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

CLOCK_HELPER = Path("/tmp/adojapan-ci-clock-helper.py")  # noqa: S108 - verified CI stage
MEDIAMTX = Path("/usr/local/lib/adojapan-ci/reader/mediamtx")
MEDIAMTX_VERSION = "v1.20.1"
FFMPEG = "/usr/bin/ffmpeg"
JITTER_SECONDS = 0.022
# Two extra GOP intervals per gate add <=4/.25 + 4/.95 =20.211s
# across the paired clocks; 22s covers that bounded added measurement work.
# The strict reader still has 15s, with at most ten further seconds for cleanup.
WORK_SECONDS = 132.0
PHASE_AGE_SECONDS = 0.2
RATE_BOUNDS = {"single": (0.25, 0.31), "fixed": (0.95, 1.05)}
PHASE_GOPS = 4  # One complete eight-second source loop; each GOP is two seconds.
ANNEX_B_FILTERS = "h264_mp4toannexb,dump_extra=freq=keyframe"
VIDEO_CONTRACT = {
    "codec_name": "h264",
    "profile": "Main",
    "level": 40,
    "width": 1080,
    "height": 1920,
}
FRAME = re.compile(
    rb"^\[Parsed_showinfo_[0-9]+ @ 0x[0-9a-fA-F]+\] n:\s*([0-9]+) "
    rb"pts:\s*-?[0-9]+ pts_time:([0-9]+(?:\.[0-9]+)?) .* "
    rb"iskey:([01]) type:([IPB])(?: |$)"
)


class ProbeFailure(Exception):
    """Only fixed reasons and bounded numeric evidence leave this fixture."""


class CaseFailure(ProbeFailure):
    def __init__(self, outcome, reason, evidence=None):
        super().__init__(reason)
        self.outcome = outcome
        self.evidence = evidence or {}


def require(condition, reason):
    if not condition:
        raise ProbeFailure(reason)


def verify_reader_binary():
    """Trust only the fixed, root-owned CI-image artifact, never install state."""
    directory = MEDIAMTX.parent.lstat()
    require(
        stat.S_ISDIR(directory.st_mode)
        and directory.st_uid == directory.st_gid == 0
        and stat.S_IMODE(directory.st_mode) == 0o555,
        "reader artifact directory invalid",
    )
    manifest = None
    digest = hashlib.sha256()
    for path, mode, limit in (
        (MEDIAMTX.parent / "manifest.json", 0o444, 1024),
        (MEDIAMTX, 0o555, 128 * 1024**2),
    ):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == metadata.st_gid == 0
                and stat.S_IMODE(metadata.st_mode) == mode
                and metadata.st_nlink == 1
                and 0 < metadata.st_size <= limit,
                "reader artifact file invalid",
            )
            if path == MEDIAMTX:
                remaining = metadata.st_size
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    require(bool(chunk), "reader artifact changed while hashing")
                    digest.update(chunk)
                    remaining -= len(chunk)
                require(not source.read(1), "reader artifact changed while hashing")
            else:
                manifest = json.loads(source.read(limit + 1))
    require(
        isinstance(manifest, dict)
        and manifest.keys() == {"version", "archive", "archive_sha256", "binary_sha256"}
        and manifest["version"] == MEDIAMTX_VERSION
        and manifest["archive"] == f"mediamtx_{MEDIAMTX_VERSION}_linux_amd64.tar.gz"
        and all(
            isinstance(manifest[key], str) and re.fullmatch("[0-9a-f]{64}", manifest[key])
            for key in ("archive_sha256", "binary_sha256")
        )
        and manifest["binary_sha256"] == digest.hexdigest(),
        "reader artifact manifest or digest invalid",
    )


def parse_frame(line, observed):
    match = FRAME.search(line) if len(line) <= 2048 else None
    if match is None:
        return None
    frame, pts, key = int(match[1]), float(match[2]), match[3] == b"1"
    require(
        frame <= 10000 and math.isfinite(pts) and 0 <= pts <= 600,
        "observer frame bounds failed",
    )
    require(not key or match[4] == b"I", "observer keyframe type changed")
    return (frame, pts, observed, key)


class PhaseObserver(threading.Thread):
    def __init__(self, pipe):
        super().__init__(daemon=True)
        self.pipe, self.lock = pipe, threading.Lock()
        self.frames = deque(maxlen=512)
        self.failure = None

    def run(self):
        reason, line_bytes, frame_delta, pts_delta = "eof", 0, None, None
        try:
            while line := self.pipe.readline(2049):
                line_bytes = len(line)
                if len(line) > 2048 or not line.endswith(b"\n"):
                    reason = "line_bound"
                    raise ProbeFailure(reason)
                reason = "frame_parse"
                frame = parse_frame(line, time.monotonic())
                if frame is not None:
                    with self.lock:
                        if self.frames:
                            previous = self.frames[-1]
                            frame_delta, pts_delta = frame[0] - previous[0], frame[1] - previous[1]
                            reason = "frame_continuity"
                            require(
                                frame_delta == 1 and abs(pts_delta - 1 / 30) < 0.002,
                                "observer frame continuity failed",
                            )
                        self.frames.append(frame)
                reason = "eof"
        except OSError:
            reason = "pipe_io"
        except (ValueError, ProbeFailure):
            pass
        finally:
            with self.lock:
                last = self.frames[-1] if self.frames else None
                self.failure = {
                    "reason": reason,
                    "line_bytes": line_bytes,
                    "frame_count": len(self.frames),
                    "last_frame": last[0] if last else None,
                    "last_pts": round(last[1], 6) if last else None,
                    "frame_delta": frame_delta,
                    "pts_delta": round(pts_delta, 6) if pts_delta is not None else None,
                }

    def snapshot(self):
        with self.lock:
            if self.failure:
                print(json.dumps({"observer_failure": self.failure}), flush=True)
            require(not self.failure and self.is_alive(), "observer failed or stopped")
            return list(self.frames)


def stable_gops(frames, case):
    keys = [frame for frame in frames if frame[3]]
    if len(keys) < PHASE_GOPS + 1:
        return None
    keys = keys[-(PHASE_GOPS + 1) :]
    for left, right in pairwise(keys):
        require(right[0] - left[0] == 60, "observer GOP length changed")
        require(abs(right[1] - left[1] - 2) < 0.002, "observer GOP timeline changed")
        require(right[2] > left[2], "observer wall clock did not advance")
    rate = (2 * PHASE_GOPS) / (keys[-1][2] - keys[0][2])
    low, high = RATE_BOUNDS[case]
    # Startup analysis may release old frames together. Never use that burst
    # as a live phase gate. Individual GOP arrival intervals vary with encoded
    # packet/VBV and read buffering, even at an exact transport clock. Compare
    # a complete source period, while retaining every GOP's media-time checks.
    return (keys[-1], [rate]) if low <= rate <= high else None


def phase_evidence(frames, case, started, observed, measure, byte_rate, chunk_bytes):
    """Project only bounded numbers; diagnostics never alter phase acceptance."""

    def number(value, maximum):
        return (
            round(value, 6)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum
            else None
        )

    def difference(right, left, maximum=WORK_SECONDS):
        if all(type(value) in (int, float) and math.isfinite(value) for value in (right, left)):
            return number(right - left, maximum)
        return None

    valid = len(frames) <= 512 and all(
        len(frame) == 4
        and type(frame[0]) is int
        and number(frame[0], 10000) is not None
        and number(frame[1], 600) is not None
        and type(frame[2]) in (int, float)
        and math.isfinite(frame[2])
        and type(frame[3]) is bool
        for frame in frames
    )
    keys = [frame for frame in frames if frame[3]] if valid else []
    intervals = []
    for left, right in pairwise(keys[-3:]):
        media = difference(right[1], left[1], 600)
        wall = difference(right[2], left[2])
        intervals.append(
            {
                "media_seconds": media,
                "wall_seconds": wall,
                "rate": number(media / wall, 10000) if media is not None and wall else None,
            }
        )
    sent_seconds = difference(measure.get("last"), measure.get("first"))
    sent_bytes = difference(measure.get("bytes"), chunk_bytes, 2**31)
    rate = None
    if sent_seconds and sent_bytes is not None and number(byte_rate, 10**7):
        rate = number(sent_bytes / sent_seconds / byte_rate, 10000)
    period_seconds = (
        difference(keys[-1][2], keys[-(PHASE_GOPS + 1)][2]) if len(keys) >= PHASE_GOPS + 1 else None
    )
    return {
        "case": case if case in RATE_BOUNDS else "unknown",
        "phase_age_seconds": difference(observed, started),
        "samples_valid": valid,
        "retained_frames": len(frames) if valid else None,
        "retained_keys": len(keys) if valid else None,
        "first_retained_frame_seconds": difference(frames[0][2], started)
        if valid and frames
        else None,
        "last_idr_age_seconds": difference(observed, keys[-1][2]) if keys else None,
        "idr_intervals": intervals,
        "period_rate": number(2 * PHASE_GOPS / period_seconds, 10000) if period_seconds else None,
        "transport_rate": rate,
        "transport_seconds": sent_seconds,
    }


def validate_phase(frames, gate, spawned, case, prior_frames):
    require(0 <= spawned - gate[2] <= PHASE_AGE_SECONDS, "strict reader missed IDR phase")
    following = [frame for frame in frames if frame[3] and frame[0] > gate[0]]
    require(len(following) >= 2, "reader interval lacks two subsequent IDRs")
    prior = [frame for frame in prior_frames if frame[3]]
    require(len(prior) >= 3 and prior[-1] == gate, "capture prior IDR context changed")
    # Keep the pre-gate evidence immutable: it can leave the observer's bounded
    # deque during capture. Two GOPs on each side cover the full source period
    # without waiting for more than the existing two post-reader IDRs.
    interval = [*prior[-3:], *following]
    for left, right in pairwise(interval):
        require(right[0] - left[0] == 60, "capture GOP continuity failed")
        require(abs(right[1] - left[1] - 2) < 0.002, "capture PTS continuity failed")
        require(right[2] > left[2], "capture wall clock did not advance")
    rate = (2 * PHASE_GOPS) / (interval[PHASE_GOPS][2] - interval[0][2])
    require(RATE_BOUNDS[case][0] <= rate <= RATE_BOUNDS[case][1], "capture rate changed")
    return round(following[0][2] - spawned, 6)


def finish_phase(observer, gate, case, deadline, healthy):
    # This finishes observation only: the one strict reader has already exited
    # or been killed/reaped at its unchanged 15s deadline. At rate .25, its
    # second following IDR naturally arrives at 16s, after that deadline.
    phase_deadline = min(deadline, time.monotonic() + 2 / RATE_BOUNDS[case][0] + 0.25)
    while True:
        healthy()
        frames = observer.snapshot()
        if sum(frame[3] and frame[0] > gate[0] for frame in frames) >= 2:
            return frames
        require(time.monotonic() < phase_deadline, "post-capture IDR observation timed out")
        time.sleep(0.005)


def expected_old_timeout(error, failure_class, progress):
    cause = error.__cause__
    return (
        type(error) is failure_class
        and str(error) == "strict RTMP sink media read timed out"
        and isinstance(cause, subprocess.TimeoutExpired)
        and cause.timeout == 15
        and progress.get("reader_input") is True
        and progress.get("reader_output") is True
        and type(progress.get("reader_frames")) is int
        and 0 < progress["reader_frames"] < 90
        and all(
            type(progress.get(key)) in (int, float)
            and math.isfinite(progress[key])
            # Pipe-observer timestamps can be recorded while reaping/draining
            # the timed-out reader. Its actual deadline is cause.timeout above.
            and 0 <= progress[key] <= WORK_SECONDS
            for key in ("reader_first_frame_seconds", "reader_last_frame_seconds")
        )
        and progress["reader_last_frame_seconds"] > progress["reader_first_frame_seconds"]
    )


def source_clock_passes(rate):
    """The same wall-time predicate for both historical and fixed pacing."""
    return (
        type(rate) in (int, float)
        and math.isfinite(rate)
        and RATE_BOUNDS["fixed"][0] <= rate <= RATE_BOUNDS["fixed"][1]
    )


def validate_clock_evidence(case, measure, byte_rate, chunk_bytes):
    """Use every successful send from first to last, not one buffered burst."""
    valid = (
        case in RATE_BOUNDS
        and isinstance(measure, dict)
        and type(byte_rate) in (int, float)
        and math.isfinite(byte_rate)
        and 0 < byte_rate <= 10**7
        and type(chunk_bytes) is int
        and 0 < chunk_bytes <= 65535
        and type(measure.get("bytes")) is int
        and chunk_bytes < measure["bytes"] <= 2**31
        and type(measure.get("waits")) is int
        and 0 < measure["waits"] <= 10**6
        and all(
            type(measure.get(key)) in (int, float)
            and math.isfinite(measure[key])
            and measure[key] >= 0
            for key in ("first", "last")
        )
    )
    if not valid:
        raise CaseFailure("FIXTURE_PRECONDITION_FAILURE", "source clock evidence is invalid")
    elapsed = measure["last"] - measure["first"]
    media_seconds = (measure["bytes"] - chunk_bytes) / byte_rate
    if not (0 < elapsed <= WORK_SECONDS and 2 * PHASE_GOPS <= media_seconds <= WORK_SECONDS):
        raise CaseFailure("FIXTURE_PRECONDITION_FAILURE", "source clock interval is incomplete")
    rate = media_seconds / elapsed
    passed = source_clock_passes(rate)
    evidence = {
        "clock_result": "PASS" if passed else "FAIL",
        "transport_rate": round(rate, 6),
        "transport_seconds": round(elapsed, 6),
        "media_seconds": round(media_seconds, 6),
        "predicate_low": RATE_BOUNDS["fixed"][0],
        "predicate_high": RATE_BOUNDS["fixed"][1],
    }
    if passed != (case == "fixed") or not RATE_BOUNDS[case][0] <= rate <= RATE_BOUNDS[case][1]:
        raise CaseFailure(
            "FIXTURE_PRECONDITION_FAILURE", "source clock regression failed", evidence
        )
    return evidence


def classify_capture_failure(case, error, failure_class, progress):
    if case == "single" and expected_old_timeout(error, failure_class, progress):
        return {
            "reader_outcome": "EXPECTED_READER_TIMEOUT",
            "decoded_frames": None,
            "full_validation": False,
        }
    if isinstance(error.__cause__, subprocess.TimeoutExpired):
        reason = (
            "fixed strict reader deadline failed"
            if case == "fixed"
            else "reader timeout preconditions failed"
        )
        raise CaseFailure("FIXTURE_PRECONDITION_FAILURE", reason) from error
    raise CaseFailure("DECODE_OR_FORMAT_FAILURE", "strict reader capture failed") from error


def validate_reader_segment(
    namespace, capture, size, expected_video, *, process_metadata_guard=lambda _command: None
):
    """Observe the real validator's decoded count; never replace its checks."""
    validator = namespace["validate_final_sink_media_segment"]
    globals_ = validator.__globals__
    original_analyze = globals_.get("analyze_decoded_video_frames")
    if not callable(original_analyze) or globals_.get("STRICT_SINK_REQUIRED_VIDEO_FRAMES") != 90:
        raise CaseFailure(
            "FIXTURE_PRECONDITION_FAILURE", "production decoded validator is unavailable"
        )
    observed = []

    def observe(path):
        result = original_analyze(path)
        observed.append(result)
        return result

    globals_["analyze_decoded_video_frames"] = observe
    try:
        result = validator(capture, size, expected_video, process_metadata_guard, segment_index=1)
    except namespace["TestFailure"] as error:
        decoded = observed[0] if len(observed) == 1 and isinstance(observed[0], dict) else {}
        count = decoded.get("frame_count")
        evidence = {
            "decoded_frames": count if type(count) is int and 0 <= count <= 10000 else None,
            "full_validation": False,
        }
        reason = str(error)
        outcome = "DECODE_OR_FORMAT_FAILURE"
        if reason == "strict RTMP sink media cleanup failed":
            outcome = "CLEANUP_FAILURE"
        elif reason.startswith("strict RTMP sink timestamp validation failed ("):
            outcome = "TIMESTAMP_OR_AV_SYNC_FAILURE"
        elif (
            reason == "strict RTMP sink video frame validation failed"
            and type(count) is int
            and 0 <= count < globals_["STRICT_SINK_REQUIRED_VIDEO_FRAMES"]
            and decoded.get("ffprobe_exit") == 0
            and decoded.get("unexpected_dimension_frames") == 0
            and decoded.get("unexpected_pixel_format_frames") == 0
            and (
                decoded.get("decode_error_flags") == {}
                or decoded.get("decode_error_flags") is False
            )
            and decoded.get("stderr_empty") is True
        ):
            outcome = "SHORT_EOF"
        raise CaseFailure(
            outcome, "strict reader full media validation failed", evidence
        ) from error
    finally:
        globals_["analyze_decoded_video_frames"] = original_analyze
    count = result.get("video_frames") if isinstance(result, dict) else None
    # Even an accidentally bypassed validator must not invent decoded evidence.
    if not (
        len(observed) == 1
        and isinstance(observed[0], dict)
        and type(count) is int
        and globals_["STRICT_SINK_REQUIRED_VIDEO_FRAMES"] <= count <= 10000
        and type(observed[0].get("frame_count")) is int
        and observed[0].get("frame_count") == count
    ):
        raise CaseFailure(
            "FIXTURE_PRECONDITION_FAILURE", "full decoded validation evidence missing"
        )
    return {
        "reader_outcome": "VALID_SEGMENT_COMPLETED",
        "decoded_frames": count,
        "full_validation": True,
    }


def sink_config(port, metrics_port):
    return {
        "logLevel": "error",
        "logDestinations": ["stdout"],
        "dumpPackets": False,
        "authMethod": "internal",
        "authInternalUsers": [
            {
                "user": "any",
                "pass": "",
                "ips": ["127.0.0.1"],
                "permissions": [{"action": "read"}, {"action": "publish"}, {"action": "metrics"}],
            }
        ],
        "rtmp": True,
        "rtmpAddress": f"127.0.0.1:{port}",
        "rtmpEncryption": "no",
        "api": False,
        "metrics": True,
        "metricsAddress": f"127.0.0.1:{metrics_port}",
        "rtsp": False,
        "srt": False,
        "hls": False,
        "webrtc": False,
        "moq": False,
        "pprof": False,
        "playback": False,
        "paths": {"live/sink": {"source": "publisher", "overridePublisher": False}},
    }


def media_commands(namespace, live, udp_port, sink_port):
    # The input is already Annex-B. FFmpeg 5.1 drains an explicit output BSF
    # at each input EOF but does not reset it when -stream_loop seeks back.
    remux = namespace["local_mpegts_remux_command"](live)
    remux[remux.index("-loglevel") + 1] = "error"
    remux.insert(1, "-xerror")
    source = (
        f"udp://127.0.0.1:{udp_port}?fifo_size={namespace['LIVE_FEED_FIFO_UNITS']}"
        f"&buffer_size={namespace['LIVE_FEED_SOCKET_BUFFER_BYTES']}"
    )
    destination = f"rtmp://127.0.0.1:{sink_port}/live/sink"
    publisher = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror"]
    publisher += ["-fflags", "+genpts", "-i", source, "-map", "0:v:0", "-map", "0:a:0"]
    publisher += ["-c:v", "copy", "-c:a", "aac", "-profile:a", "aac_low"]
    publisher += ["-af", "aresample=48000:async=1:first_pts=0", "-ar", "48000", "-ac", "2"]
    publisher += ["-b:a", "128k", "-max_muxing_queue_size", "2048", "-flush_packets", "1"]
    publisher += ["-f", "flv", destination]
    observer = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats", "-xerror"]
    observer += ["-fflags", "+nobuffer", "-flags", "+low_delay", "-threads", "1", "-i", destination]
    observer += ["-map", "0:v:0", "-an", "-filter_threads", "1", "-vf", "showinfo"]
    observer += ["-vsync", "0", "-f", "null", "-"]
    return remux, publisher, observer


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def validate_loop_packets(payload, duration, *, bsf_eof=False):
    require(len(payload) <= 131072, "source seam probe output bound failed")
    data = json.loads(payload)
    streams, packets = data["streams"], data["packets"]
    require(
        len(streams) == 1
        and all(streams[0].get(key) == value for key, value in VIDEO_CONTRACT.items()),
        "source seam video contract changed",
    )
    # The deliberately discontinuous four-second loop can make ffprobe guess
    # 120/1. Its original MP4 is checked at 30/1 before conversion; packet PTS
    # below remain the actual clock oracle. The valid eight-second loop and
    # the original single-cycle EOF counterexample must still report 30/1.
    require(
        duration == 4 and not bsf_eof or streams[0].get("r_frame_rate") == "30/1",
        "source seam nominal frame rate changed",
    )
    require(duration in (4, 8) and isinstance(packets, list), "source seam input bounds failed")
    pts = [float(packet["pts_time"]) for packet in packets]
    require(
        all(math.isfinite(value) and 0 <= value <= 20 for value in pts),
        "source seam PTS bounds failed",
    )
    print(
        json.dumps(
            {
                "source_loop_probe": {
                    "duration_seconds": duration,
                    "video_packets": len(packets),
                    "first_pts_seconds": round(pts[0], 6) if pts else None,
                    "last_pts_seconds": round(pts[-1], 6) if pts else None,
                }
            }
        ),
        flush=True,
    )
    seam = duration * 30
    require(
        duration == 4 and len(packets) == seam if bsf_eof else seam < len(packets) <= seam + 8,
        "source seam packet count failed",
    )
    require(
        all(packet["flags"] in ("K_", "__") for packet in packets),
        "source seam packet flags changed",
    )
    keys = [index for index, packet in enumerate(packets) if packet["flags"] == "K_"]
    require(keys == list(range(0, len(packets), 60)), "source seam GOP length changed")
    deltas = [right - left for left, right in pairwise(pts)]
    require(
        all(abs(delta - 1 / 30) < 0.002 for index, delta in enumerate(deltas, 1) if index != seam),
        "source continuity failed outside loop seam",
    )
    if bsf_eof:
        return {"video_packets": len(packets), "reason": "explicit_bsf_after_eof"}
    seam_delta = deltas[seam - 1]
    evidence = {
        "duration_seconds": duration,
        "video_packets": len(packets),
        "seam_frame": seam,
        "seam_delta_seconds": round(seam_delta, 6),
        "seam_error_seconds": round(seam_delta - 1 / 30, 6),
    }
    print(json.dumps({"source_loop": evidence}), flush=True)
    # The negative case must exhibit only the AAC-padding seam defect; an
    # arbitrary probe/decode/continuity failure is never accepted as evidence.
    require(
        0.002 < seam_delta - 1 / 30 < 0.022 if duration == 4 else abs(seam_delta - 1 / 30) < 0.002,
        "source seam counterfactual not established",
    )
    return evidence


def run_transport(namespace, command, output, *, bsf_eof=False):
    globals_ = namespace["capture_final_sink_media_segment"].__globals__
    command = list(command)
    command[command.index("-loglevel") + 1] = "error"
    if not bsf_eof and "-xerror" not in command:
        command.insert(1, "-xerror")
    command[-1:] = ["-fs", str(11 * 1024**2), str(output)]
    stderr = output.with_suffix(".stderr")
    with stderr.open("xb") as err:
        stderr.chmod(0o600)
        completed = globals_["run"](
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            timeout=10,
        )
    require(
        completed.returncode == 0
        and output.is_file()
        and 0 < output.stat().st_size <= 12 * 1024**2,
        "source transport remux failed",
    )
    output.chmod(0o600)
    require(stderr.stat().st_size <= 65536, "source transport stderr bound failed")
    errors = stderr.read_bytes()
    if bsf_eof:
        # Exact FFmpeg 5.1 error pair, never a generic failed-command oracle.
        pair = (
            rb"\[bsf_list @ (?:0x)?[0-9a-fA-F]{6,32}\] A non-NULL packet sent after an EOF\.\r?\n"
            rb"Error applying bitstream filters to an output packet for stream #0:0\.\r?\n"
        )
        require(
            re.fullmatch(rb"(?:" + pair + rb"){1,8}", errors) is not None,
            "original explicit BSF EOF failure not established",
        )
    else:
        require(not errors, "source transport remux emitted an error")


def probe_transport(namespace, output):
    globals_ = namespace["capture_final_sink_media_segment"].__globals__
    stdout = output.with_suffix(".probe.json")
    stderr = output.with_suffix(".probe.stderr")
    with stdout.open("xb") as out, stderr.open("xb") as err:
        stdout.chmod(0o600)
        stderr.chmod(0o600)
        probe = globals_["run"](
            [
                str(globals_["FFPROBE"]),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_streams",
                "-show_entries",
                "stream=codec_name,profile,level,width,height,r_frame_rate:packet=pts_time,flags",
                "-of",
                "json",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            timeout=10,
        )
    require(probe.returncode == 0 and stderr.stat().st_size == 0, "source seam probe failed")
    require(stdout.stat().st_size <= 131072, "source seam probe output bound failed")
    return stdout.read_text(encoding="utf-8")


def prepare_loop_source(namespace, live, work):
    require(0 < live.stat().st_size <= 12 * 1024**2, "source clip size bound failed")
    signature = namespace["stream_signature"](live, include_gop=False)
    video = signature["streams"][0]
    require(
        all(video.get(key) == value for key, value in VIDEO_CONTRACT.items())
        and video.get("r_frame_rate") == "30/1",
        "original source video contract changed",
    )
    command = namespace["local_mpegts_remux_command"](live)
    loop = command.index("-stream_loop")
    del command[loop : loop + 2]
    command[-1:] = ["-bsf:v", ANNEX_B_FILTERS, "pipe:1"]
    output = work / "prepared-source.ts"
    # Apply SPS/PPS-before-SEI conversion exactly once, keeping encoded media.
    run_transport(namespace, command, output)
    return output


def verify_original_bsf_loop(namespace, live, work):
    command = namespace["local_mpegts_remux_command"](live)
    command[-1:] = ["-bsf:v", ANNEX_B_FILTERS, "-t", "4.2", "pipe:1"]
    output = work / "original-bsf-loop.ts"
    run_transport(namespace, command, output, bsf_eof=True)
    evidence = validate_loop_packets(probe_transport(namespace, output), 4, bsf_eof=True)
    print(json.dumps({"source_bsf_eof": evidence}), flush=True)


def verify_source_loop(namespace, prepared, work, duration):
    command = media_commands(namespace, prepared, 1, 1)[0]
    command[-1:] = ["-t", str(duration + 0.2), "pipe:1"]
    output = work / "loop-seam.ts"
    run_transport(namespace, command, output)
    return validate_loop_packets(probe_transport(namespace, output), duration)


def configure(namespace, work, sink_port, deadline):
    globals_ = namespace["capture_final_sink_media_segment"].__globals__
    globals_.update(
        SELF_TEST_STAGE_FILE="",
        SELF_TEST_PROGRESS_FILE=work / "progress.json",
        SINK_RTMP_PORT=sink_port,
        # Eight seconds aligns 240 video frames / 375 complete AAC frames.
        # Four seconds contains a half AAC frame; FFmpeg 5.1 stream_loop uses
        # its padded audio endpoint for every stream, disturbing video PTS.
        LIVE_FIXTURE_DURATION_SECONDS=8,
    )
    original_run, original_probe = globals_["run"], globals_["run_probe"]

    def bounded_run(command, **kwargs):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "reader clock outer deadline reached")
        kwargs["timeout"] = min(kwargs.get("timeout", 25), remaining, 25)
        return original_run(command, **kwargs)

    def bounded_probe(command, *, timeout):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "reader clock outer deadline reached")
        # Delegate to the original probe, retaining its error/output controls.
        return original_probe(command, timeout=min(timeout, remaining))

    globals_["run"] = bounded_run
    globals_["run_probe"] = bounded_probe
    return globals_


def cleanup_case(feeder, feeder_started, processes, observer, port, metrics_port, udp):
    errors = []

    def attempt(operation):
        try:
            operation()
        except Exception as error:
            errors.append(error)

    if feeder_started:
        attempt(lambda: require(feeder.finish(timeout=2), "feeder cleanup failed"))

    def stop(process):
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        except ProcessLookupError:
            # Reap even when a process exited immediately before termination.
            process.wait(timeout=1)

    for process in reversed(processes):
        attempt(lambda process=process: stop(process))
    if observer is not None:
        attempt(lambda: observer.join(timeout=1))
        attempt(lambda: require(not observer.is_alive(), "observer cleanup failed"))
        attempt(observer.pipe.close)

    def check_tcp(tcp_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            check.settimeout(0.25)
            require(check.connect_ex(("127.0.0.1", tcp_port)) != 0, "TCP port cleanup failed")

    for tcp_port in (port, metrics_port):
        attempt(lambda tcp_port=tcp_port: check_tcp(tcp_port))

    def check_udp():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as check:
            check.bind(("127.0.0.1", udp))

    attempt(check_udp)
    if errors:
        raise CaseFailure("CLEANUP_FAILURE", "reader clock fixture cleanup failed") from errors[0]


def run_case(case, namespace, live, prepared, work, deadline):
    port, metrics_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM)
    udp = free_port(socket.SOCK_DGRAM)
    require(port != metrics_port, "fixture ports collided")
    globals_ = configure(namespace, work, port, deadline)
    remux, publisher_command, observer_command = media_commands(namespace, prepared, udp, port)
    byte_rate = namespace["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] / 8
    measure = {
        "bytes": 0,
        "first": None,
        "last": None,
        "max_gap": 0.0,
        "waits": 0,
        "max_overrun": 0.0,
    }
    lock = threading.Lock()

    class MeasuredSocket(socket.socket):
        def sendto(self, payload, destination):
            sent = super().sendto(payload, destination)
            now = time.monotonic()
            with lock:
                if measure["first"] is None:
                    measure["first"] = now
                if measure["last"] is not None:
                    measure["max_gap"] = max(measure["max_gap"], now - measure["last"])
                measure["last"], measure["bytes"] = now, measure["bytes"] + sent
            return sent

    class DelayedCondition(threading.Condition):
        def wait(self, timeout=None):
            started = time.monotonic()
            result = super().wait(timeout)
            if timeout is not None and timeout > 0:
                time.sleep(JITTER_SECONDS)
                with lock:
                    measure["waits"] += 1
                    measure["max_overrun"] = max(
                        measure["max_overrun"], time.monotonic() - started - timeout
                    )
            return result

    globals_["socket"] = SimpleNamespace(**(vars(socket) | {"socket": MeasuredSocket}))
    feeder = namespace["PacedMPEGTSFeeder"](remux, udp)
    feeder._condition = DelayedCondition()
    processes, observer, feeder_started = [], None, False
    config = work / "sink.json"
    config.write_text(json.dumps(sink_config(port, metrics_port)), encoding="utf-8")
    config.chmod(0o600)

    def start(command, stderr=subprocess.DEVNULL):
        process = subprocess.Popen(  # noqa: S603 - fixed loopback fixture argv
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, bufsize=0
        )
        processes.append(process)
        return process

    def healthy():
        require(all(process.poll() is None for process in processes), "fixture process exited")
        require(feeder.healthy(), "fixture feeder stopped")

    result = {"case": case, "decoded_frames": None, "full_validation": False}
    try:
        start([str(MEDIAMTX), str(config)])
        namespace["wait_tcp"](port, timeout=5)
        start(publisher_command)
        feeder.start()
        feeder_started = True
        require(feeder.wait_ready(5), "fixture feeder did not start")
        # Wait for the existing publisher to make the fresh path readable.
        # Poll fixed loopback metrics, not repeated media-reader attempts.
        ready_deadline = min(deadline - 20, time.monotonic() + 20)
        ready = False
        while time.monotonic() < ready_deadline:
            healthy()
            try:
                samples = namespace["parse_metrics"](
                    namespace["fetch_metrics"](metrics_port), "paths"
                )
                ready = any(
                    labels.get("name") == "live/sink"
                    and labels.get("state") == "ready"
                    and value == 1
                    for labels, value in samples
                )
            except OSError:
                ready = False
            if ready:
                break
            time.sleep(0.025)
        require(ready, "fresh sink path did not become ready")
        phase_started = time.monotonic()
        phase_process = start(observer_command, subprocess.PIPE)
        observer = PhaseObserver(phase_process.stderr)
        observer.start()
        phase_deadline = min(deadline - 20, time.monotonic() + 45)
        gate = None
        frames = []
        prior_frames = ()
        while time.monotonic() < phase_deadline:
            healthy()
            frames = observer.snapshot()
            phase = stable_gops(frames, case)
            if phase and time.monotonic() - phase[0][2] <= PHASE_AGE_SECONDS:
                gate = phase[0]
                prior_frames = tuple(frames)
                break
            time.sleep(0.005)
        with lock:
            phase_measure = dict(measure)
        evidence = phase_evidence(
            frames,
            case,
            phase_started,
            time.monotonic(),
            phase_measure,
            byte_rate,
            namespace["LIVE_FEED_CHUNK_BYTES"],
        )
        print(json.dumps({"phase_gate": evidence | {"established": gate is not None}}), flush=True)
        require(gate is not None, "stable live IDR phase not established")
        require(deadline - time.monotonic() >= 18, "insufficient strict reader budget")
        with lock:
            before = dict(measure)
        diagnostic = namespace["CaptureReaderProgress"]()
        spawned = []
        real_popen = subprocess.Popen

        def reader_popen(command, **kwargs):
            process = real_popen(command, **kwargs)  # noqa: S603 - unchanged strict reader
            spawned.append(time.monotonic())
            diagnostic.started = spawned[-1]
            return process

        original_subprocess = globals_["subprocess"]
        globals_["subprocess"] = SimpleNamespace(**(vars(subprocess) | {"Popen": reader_popen}))
        capture_error = None
        try:
            capture, size = namespace["capture_final_sink_media_segment"](
                work, 1, lambda _command: None, reader_diagnostic=diagnostic
            )
        except namespace["TestFailure"] as error:
            capture_error = error
        finally:
            # Later signature/validation probes must not mutate reader timing.
            globals_["subprocess"] = original_subprocess
        healthy()
        require(len(spawned) == 1, "strict reader was not launched exactly once")
        with lock:
            after = dict(measure)
        result["reader"] = diagnostic.snapshot()
        result["clock"] = validate_clock_evidence(
            case, after, byte_rate, namespace["LIVE_FEED_CHUNK_BYTES"]
        )
        frames = finish_phase(observer, gate, case, deadline, healthy)
        next_idr = validate_phase(frames, gate, spawned[0], case, prior_frames)
        rate = (after["bytes"] - before["bytes"]) / (after["last"] - before["last"]) / byte_rate
        require(RATE_BOUNDS[case][0] <= rate <= RATE_BOUNDS[case][1], "feeder rate changed")
        require(after["waits"] > before["waits"], "scheduler injection was not exercised")
        result.update(
            transport_rate=round(rate, 6),
            launch_age_seconds=round(spawned[0] - gate[2], 6),
            next_idr_seconds=next_idr,
            max_send_gap=round(after["max_gap"], 6),
            max_wait_overrun=round(after["max_overrun"], 6),
        )
        if capture_error is not None:
            result.update(
                classify_capture_failure(
                    case, capture_error, namespace["TestFailure"], diagnostic.snapshot()
                )
            )
        else:
            expected = namespace["stream_signature"](live, include_gop=False)["streams"][0]
            result.update(validate_reader_segment(namespace, capture, size, expected))
    except Exception as error:
        failure = (
            error
            if isinstance(error, CaseFailure)
            else CaseFailure("FIXTURE_PRECONDITION_FAILURE", "reader fixture precondition failed")
        )
        print(
            json.dumps(
                {"case_result": result | failure.evidence | {"reader_outcome": failure.outcome}}
            ),
            flush=True,
        )
        if failure is error:
            raise
        raise failure from error
    finally:
        try:
            cleanup_case(feeder, feeder_started, processes, observer, port, metrics_port, udp)
        except CaseFailure:
            print(
                json.dumps({"case_result": result | {"reader_outcome": "CLEANUP_FAILURE"}}),
                flush=True,
            )
            raise
    print(json.dumps({"case_result": result}), flush=True)
    return result


def run_pair(loader, live, prepared, work, deadline):
    failures = []
    results = []
    for case in ("single", "fixed"):
        directory = work / case
        directory.mkdir(mode=0o700)
        try:
            require(deadline - time.monotonic() >= 18, "insufficient paired reader budget")
            results.append(run_case(case, loader(case), live, prepared, directory, deadline))
        except ProbeFailure as error:
            # A second independent case is evidence, never a fallback/retry.
            failures.append(error)
            print(
                json.dumps(
                    {
                        "paired_case_failure": {
                            "case": case,
                            "reader_outcome": error.outcome
                            if isinstance(error, CaseFailure)
                            else "FIXTURE_PRECONDITION_FAILURE",
                        }
                    }
                ),
                flush=True,
            )
    if failures:
        raise ProbeFailure("reader clock paired acceptance failed") from failures[0]
    return results


def main():
    require(os.environ.get("CI_NATIVE_READER_CLOCK") == "isolated-fixture", "CI-only reader gate")
    deadline = time.monotonic() + WORK_SECONDS
    require(CLOCK_HELPER.is_file(), "reader fixture prerequisites missing")
    verify_reader_binary()
    version = subprocess.run(  # noqa: S603 - fixed verified CI-image binary
        [str(MEDIAMTX), "--version"], capture_output=True, timeout=5, check=False
    )
    require(
        version.returncode == 0
        and version.stdout.strip() == MEDIAMTX_VERSION.encode("ascii")
        and not version.stderr,
        "MediaMTX pin mismatch",
    )
    os.environ.pop("MOBLIN_RELAY_SELF_TEST_STAGE_FILE", None)
    loader = runpy.run_path(str(CLOCK_HELPER), run_name="_reader_clock_loader")["load_feeder"]
    temporary = TemporaryDirectory(prefix="native-reader-clock-")
    try:
        work = Path(temporary.name)
        namespace = loader("fixed")
        globals_ = configure(namespace, work, 1, deadline)
        original = work / "original-four-second-source"
        original.mkdir(mode=0o700)
        globals_["LIVE_FIXTURE_DURATION_SECONDS"] = 4
        old_live = namespace["generate_live"](original)
        verify_original_bsf_loop(namespace, old_live, original)
        old_prepared = prepare_loop_source(namespace, old_live, original)
        verify_source_loop(namespace, old_prepared, original, 4)
        globals_["LIVE_FIXTURE_DURATION_SECONDS"] = 8
        live = namespace["generate_live"](work)
        prepared = prepare_loop_source(namespace, live, work)
        verify_source_loop(namespace, prepared, work, 8)
        run_pair(loader, live, prepared, work, deadline)
    finally:
        try:
            temporary.cleanup()
        except Exception as error:
            print(json.dumps({"reader_outcome": "CLEANUP_FAILURE"}), flush=True)
            raise CaseFailure("CLEANUP_FAILURE", "reader temporary-file cleanup failed") from error
    print(
        "Strict RTMP reader clock counterfactual verified; owned-process cleanup passed", flush=True
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        reason = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
        print(f"Reader clock probe failed: {reason}", flush=True)
        raise SystemExit(1) from None
