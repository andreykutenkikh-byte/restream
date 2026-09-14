#!/usr/bin/env python3
"""Bounded offline evidence after strict reader failure; never a media oracle."""

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

STAGE = Path("/tmp/adojapan-ci-reader-evidence")  # noqa: S108 - fixed root-private CI stage
FILES = (
    "partial.flv",
    "report.pending",
    "report.json",
    "helper.py",
    "postmortem.py",
    "marker.json",
)
PARTIAL_LIMIT = 32 * 1024 * 1024
REPORT_LIMIT = 512 * 1024
PIPE_LIMIT = 256 * 1024
TOTAL_SECONDS = 25
TTL_SECONDS = 1800
NUMBER_LIMIT = 2**63 - 1


def check_metadata(info, *, directory=False, limit=REPORT_LIMIT):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and (info.st_nlink != 1 or not 0 <= info.st_size <= limit))
    ):
        raise ValueError("UNSAFE_PATH")


def owned(path, *, directory=False, limit=REPORT_LIMIT):
    if path.resolve() != path.absolute():
        raise ValueError("UNSAFE_PATH")
    info = path.lstat()
    check_metadata(info, directory=directory, limit=limit)
    return info


def identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
        info.st_mode,
        info.st_uid,
    )


def open_private(path, limit):
    owned(STAGE, directory=True)
    before = owned(path, limit=limit)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        check_metadata(opened, limit=limit)
        if identity(before) != identity(opened):
            raise ValueError("UNSAFE_PATH")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def read_json(name, limit):
    descriptor, before = open_private(STAGE / name, limit)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(limit + 1)
        if len(raw) > limit or identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError("INVALID_JSON")
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("INVALID_JSON")
    return value


def number(value, *, minimum=0):
    return type(value) is int and minimum <= value <= NUMBER_LIMIT


def rows(value, width, *, codes=False):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("INVALID_REPORT")
    if any(
        not isinstance(row, list)
        or len(row) != width
        or any(not number(item, minimum=-(2**31) if codes else 0) for item in row)
        or (codes and (row[0] < 0 or row[1] not in range(1, 13)))
        for row in value
    ):
        raise ValueError("INVALID_REPORT")
    return value


def safe_report(value):
    """Project only numeric receipts and checked hashes; discard all unknown text."""
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or type(value.get("segment")) is not int
        or value["segment"] != 4
        or type(value.get("outcome")) is not int
        or value["outcome"] not in {1, 2, 3, 4}
        or value.get("reaped") is not True
    ):
        raise ValueError("READER_FAILURE_NOT_REAPED")
    result = {"version": 1, "segment": 4, "outcome": value["outcome"], "reaped": True}
    for key in ("returncode", "events_dropped", "progress_dropped"):
        item = value.get(key)
        if not number(item, minimum=-(2**31) if key == "returncode" else 0):
            raise ValueError("INVALID_REPORT")
        result[key] = item
    result["events"] = rows(value.get("events"), 3, codes=True)
    result["progress"] = rows(value.get("progress"), 3)
    if value["outcome"] != 1 and not any(row[1] == 12 for row in result["events"]):
        raise ValueError("ORIGINAL_FAILURE_NOT_RECORDED")
    hashes = {"source_sha": 40, "source_tree": 40, "self_test_sha256": 64, "helper_sha256": 64}
    source = value.get("source", {})
    result["source"] = {
        key: item
        for key, length in hashes.items()
        if isinstance(item := source.get(key), str)
        and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", item)
    }
    pipes = value.get("pipes")
    if not isinstance(pipes, list) or len(pipes) != 2:
        raise ValueError("INVALID_REPORT")
    result["pipes"] = []
    for pipe in pipes:
        clean = {}
        for key in ("bytes", "reads", "last_ns", "handler_max_ns"):
            item = pipe.get(key)
            if not number(item) and not (key == "last_ns" and item is None):
                raise ValueError("INVALID_REPORT")
            clean[key] = item
        if type(pipe.get("eof")) is not bool:
            raise ValueError("INVALID_REPORT")
        result["pipes"].append(clean | {"eof": pipe["eof"]})
    for key in ("cgroup_before", "cgroup_after"):
        result[key] = {}
        for name, item in value.get(key, {}).items():
            if (
                name
                in {
                    "usage_usec",
                    "user_usec",
                    "system_usec",
                    "nr_periods",
                    "nr_throttled",
                    "throttled_usec",
                }
                and number(item)
            ) or (
                name in {"cpu_max", "memory_max", "memory_current", "pids_max"}
                and isinstance(item, list)
                and len(item) == (2 if name == "cpu_max" else 1)
                and all(part is None or number(part) for part in item)
            ):
                result[key][name] = item
    observer = value.get("observer", [])
    if not isinstance(observer, list) or len(observer) > 128:
        raise ValueError("INVALID_REPORT")
    for row in observer:
        if (
            not isinstance(row, list)
            or len(row) != 10
            or not all(number(item, minimum=-30_000_000_000) for item in row[:2])
            or not -30_000_000_000 <= row[0] <= row[1] <= 660_000_000_000
            or not number(row[2])
            or row[2] >= 512
            or any(item is not None and not number(item) for item in row[3:])
        ):
            raise ValueError("INVALID_REPORT")
    result["observer"] = observer
    capture = value.get("capture_observer", [])
    if (
        not isinstance(capture, list)
        or len(capture) > 512
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or not number(row[0], minimum=-30_000_000_000)
            or row[0] > 660_000_000_000
            or type(row[1]) is not bool
            or (row[2] is not None and not number(row[2]))
            for row in capture
        )
    ):
        raise ValueError("INVALID_REPORT")
    result["capture_observer"] = capture
    reader = value.get("reader", {})
    result["reader"] = {}
    for key in ("reader_input", "reader_output", "reader_inspection_limited"):
        if type(reader.get(key)) is bool:
            result["reader"][key] = reader[key]
    for key in (
        "reader_frames",
        "reader_input_seconds",
        "reader_output_seconds",
        "reader_probe_start_seconds",
        "reader_probe_end_seconds",
        "reader_first_frame_seconds",
        "reader_last_frame_seconds",
        "reader_media_seconds",
    ):
        item = reader.get(key)
        if (
            type(item) in {int, float}
            and math.isfinite(item)
            and 0 <= item <= (10000 if key == "reader_frames" else 660)
        ):
            result["reader"][key] = item
    result["binaries"] = {}
    for name in ("ffmpeg", "mediamtx"):
        item = value.get("binaries", {}).get(name)
        if item is None:
            result["binaries"][name] = None
        elif (
            isinstance(item, dict)
            and number(item.get("bytes"))
            and item["bytes"] <= 128 * 1024 * 1024
            and isinstance(item.get("sha256"), str)
            and re.fullmatch("[0-9a-f]{64}", item["sha256"])
        ):
            result["binaries"][name] = {"bytes": item["bytes"], "sha256": item["sha256"]}
    artifact = value.get("artifact", {})
    if (
        type(artifact.get("state")) is not int
        or artifact["state"] not in {0, 1, 2}
        or not number(artifact.get("bytes"))
        or type(artifact.get("forced")) is not bool
        or artifact.get("incomplete") is not True
        or type(artifact.get("validator")) is not int
        or artifact["validator"] != 0
    ):
        raise ValueError("INVALID_REPORT")
    digest = artifact.get("sha256")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
    ):
        raise ValueError("INVALID_REPORT")
    result["artifact"] = {
        key: artifact[key]
        for key in ("state", "bytes", "sha256", "forced", "incomplete", "validator")
    }
    return result


def unknown(reason):
    return {"state": "UNKNOWN", "reason": reason}


def stop_child(child):
    """Only signal the session created by this analyzer, then reap its leader."""
    for sig, timeout in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 1.0)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, sig)
        try:
            child.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass


def probe_count(descriptor, field, deadline):
    option = "-count_packets" if field == "nb_read_packets" else "-count_frames"
    command = [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "flv",
        "-select_streams",
        "v:0",
        option,
        "-show_entries",
        "stream=" + field,
        "-of",
        "json",
        "/proc/self/fd/" + str(descriptor),
    ]
    output, overflow, child = bytearray(), threading.Event(), None
    reader = None
    try:
        if time.monotonic() >= deadline:
            return unknown("TIME_LIMIT")
        os.lseek(descriptor, 0, os.SEEK_SET)
        child = subprocess.Popen(  # noqa: S603 - fixed offline command
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(descriptor,),
            start_new_session=True,
        )

        def drain():
            while chunk := child.stdout.read(8192):
                if len(output) + len(chunk) > PIPE_LIMIT:
                    overflow.set()
                    return
                output.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        while child.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                return unknown("OUTPUT_LIMIT" if overflow.is_set() else "TIME_LIMIT")
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
        reader.join(timeout=max(0, min(0.25, deadline - time.monotonic())))
        if overflow.is_set() or reader.is_alive():
            return unknown("OUTPUT_LIMIT")
        if child.returncode:
            return unknown("PROBE_FAILED")
        streams = json.loads(output).get("streams")
        if not isinstance(streams, list) or len(streams) != 1:
            return unknown("COUNT_UNAVAILABLE")
        count = streams[0].get(field)
        if not isinstance(count, str) or not re.fullmatch(r"[0-9]{1,10}", count):
            return unknown("COUNT_UNAVAILABLE")
        return {"state": "KNOWN", "value": int(count)}
    except (OSError, ValueError, TypeError, AttributeError):
        return unknown("PROBE_FAILED")
    finally:
        if child is not None:
            stop_child(child)
            if reader is not None:
                reader.join(timeout=0.25)
            if child.stdout is not None:
                child.stdout.close()


def cleanup():
    """No globbing or recursive deletion; unsafe paths and unrelated files remain."""
    try:
        owned(STAGE, directory=True)
        for name in FILES:
            path = STAGE / name
            with contextlib.suppress(OSError, ValueError):
                owned(STAGE, directory=True)
                # Oversized private files are rejected for reads but still safely removed.
                owned(path, limit=NUMBER_LIMIT)
                path.unlink()
        STAGE.rmdir()
        return True
    except (OSError, ValueError):
        return False


def analyze():
    deadline = time.monotonic() + TOTAL_SECONDS
    result = {
        "version": 1,
        "scope": "offline_reader_evidence",
        "original_strict_result": "UNKNOWN",
        "full_original_validator": unknown("MAIN_RESULT_SEPARATE"),
        "last_progress_counter": unknown("REPORT_UNAVAILABLE"),
        "actual_video_packets": unknown("PARTIAL_UNAVAILABLE"),
        "actually_decoded_frames": unknown("PARTIAL_UNAVAILABLE"),
        "timestamps": unknown("NOT_MEASURED"),
        "keyframes": unknown("NOT_MEASURED"),
    }
    descriptor = None
    try:
        marker = read_json("marker.json", 8192)
        created, expires = marker.get("created_epoch"), marker.get("expires_epoch")
        if (
            marker.get("version") != 1
            or marker.get("purpose") != "synthetic-stuck-live-reader"
            or not number(created)
            or not number(expires)
            or expires - created != TTL_SECONDS
            or not created <= time.time() <= expires
        ):
            raise ValueError("INVALID_MARKER")
        report = safe_report(read_json("report.json", REPORT_LIMIT))
        result["reader_evidence"] = report
        result["original_strict_result"] = "READER_COMPLETED" if report["outcome"] == 1 else "FAIL"
        if report["outcome"] != 1:
            result["full_original_validator"] = {"state": "NOT_RUN", "reason": "reader_failed"}
        if report["progress"]:
            result["last_progress_counter"] = {"state": "KNOWN", "value": report["progress"][-1][1]}
        if report["outcome"] == 1 or report["artifact"]["state"] != 1:
            return result
        descriptor, before = open_private(STAGE / "partial.flv", PARTIAL_LIMIT)
        if before.st_size != report["artifact"]["bytes"] or before.st_size < 13:
            raise ValueError("INVALID_PARTIAL")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 65536):
            total += len(chunk)
            if total > PARTIAL_LIMIT or time.monotonic() >= deadline - 3:
                raise ValueError("INVALID_PARTIAL")
            digest.update(chunk)
        if digest.hexdigest() != report["artifact"]["sha256"] or identity(
            os.fstat(descriptor)
        ) != identity(before):
            raise ValueError("INVALID_PARTIAL")
        result["actual_video_packets"] = probe_count(
            descriptor, "nb_read_packets", min(deadline - 5, time.monotonic() + 8)
        )
        result["actually_decoded_frames"] = probe_count(descriptor, "nb_read_frames", deadline - 3)
        if identity(os.fstat(descriptor)) != identity(before):
            raise ValueError("INVALID_PARTIAL")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        result["actual_video_packets"] = unknown("EVIDENCE_UNAVAILABLE")
        result["actually_decoded_frames"] = unknown("EVIDENCE_UNAVAILABLE")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        result["private_stage_removed"] = cleanup()
    return result


if __name__ == "__main__":
    print(json.dumps(analyze(), separators=(",", ":"), allow_nan=False))
