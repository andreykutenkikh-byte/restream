#!/usr/bin/python3
"""Compare old/fixed fixture clocks using real, isolated portrait media on CI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

SELF_TEST = Path("/tmp/adojapan-ci-clock-self-test.py")  # noqa: S108 - explicit disposable CI stage
FFPROBE = "/usr/bin/ffprobe"
FIXED_CONDITION = "if now >= deadline + len(pending) / self._bytes_per_second:"
PREVIOUS_CONDITION = "if now > deadline:"
SOURCE_DURATION_SECONDS = 8
CAPTURE_TIMEOUT_SECONDS = 20.0
MAX_CAPTURE_BYTES = 12 * 1024 * 1024
WAKEUP_JITTER_SECONDS = 0.003


class ProbeFailure(Exception):
    """Only fixed, credential-free diagnostics leave the disposable fixture."""


def load_feeder(case):
    if case not in {"old", "fixed"}:
        raise ProbeFailure("invalid feeder case")
    source = SELF_TEST.read_text(encoding="utf-8")
    if source.count(FIXED_CONDITION) != 1:
        raise ProbeFailure("staged feeder does not contain the tested clock repair")
    if case == "fixed":
        return runpy.run_path(str(SELF_TEST), run_name="_native_feeder_media")
    # Replay exactly the removed comparison, changing no other source behavior.
    source = source.replace(FIXED_CONDITION, PREVIOUS_CONDITION)
    namespace = {"__name__": "_native_feeder_media", "__file__": str(SELF_TEST)}
    exec(compile(source, str(SELF_TEST), "exec"), namespace)  # noqa: S102 - staged test code
    return namespace


def make_transport(directory):
    namespace = load_feeder("fixed")
    generate = namespace["generate_live"]
    generate.__globals__["LIVE_FIXTURE_DURATION_SECONDS"] = SOURCE_DURATION_SECONDS
    original_run = generate.__globals__["run"]

    def bounded_generate(command, **kwargs):
        return original_run(command, timeout=30, **kwargs)

    generate.__globals__["run"] = bounded_generate
    live = generate(directory)
    transport = directory / "source.ts"
    command = namespace["local_mpegts_remux_command"](live)
    loop_index = command.index("-stream_loop")
    del command[loop_index : loop_index + 2]
    command[-1] = str(transport)
    result = subprocess.run(  # noqa: S603 - fixed ffmpeg with generated local portrait media
        command, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
    )
    if result.returncode or result.stderr or not transport.is_file():
        raise ProbeFailure("finite portrait transport generation failed")
    payload = transport.read_bytes()
    if not payload or len(payload) > MAX_CAPTURE_BYTES or len(payload) % 188:
        raise ProbeFailure("finite portrait transport has an invalid size")
    # Complete MPEG-TS files can end partway through a feeder datagram. Null
    # packets add no media and allow the real feeder to send the entire final
    # video/audio PES. A wall-time cutoff can manufacture a truncated terminal
    # PES that ffprobe's strict stderr gate correctly rejects.
    padding = (-len(payload)) % namespace["LIVE_FEED_CHUNK_BYTES"]
    payload += (b"\x47\x1f\xff\x10" + b"\xff" * 184) * (padding // 188)
    transport.write_bytes(payload)
    probe_packets(transport, "source")
    return transport, payload


def media_clock_rate(case, directory, transport, source_payload):
    namespace = load_feeder(case)
    cls = namespace["PacedMPEGTSFeeder"]
    sent = SimpleNamespace(digest=hashlib.sha256(), size=0, first=None, last=None)
    received = hashlib.sha256()
    received_size = 0
    original_socket = socket.socket

    class MeasuredSender(original_socket):
        def sendto(self, data, address):
            size = super().sendto(data, address)
            observed = time.monotonic()
            if sent.first is None:
                sent.first = observed
            sent.last = observed
            sent.digest.update(data[:size])
            sent.size += size
            return size

    class DelayedCondition(threading.Condition):
        def wait(self, timeout=None):
            result = super().wait(timeout)
            if timeout is not None and timeout > 0:
                # Inject bounded scheduler lateness into this feeder only.
                time.sleep(WAKEUP_JITTER_SECONDS)
            return result

    capture = directory / f"{case}.ts"
    with original_socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        receiver.settimeout(0.25)
        # FFmpeg finalized source.ts before this process starts. Retain its
        # bytes exactly, with pipe backpressure and no producer-side pacing.
        # Keep the producer alive until finish() so expected finite EOF cannot
        # become the live feeder's remux-failure signal.
        producer = [
            sys.executable,
            "-c",
            "import pathlib,sys,time; "
            "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
            "sys.stdout.buffer.flush(); time.sleep(60)",
            str(transport),
        ]
        feeder = cls(producer, receiver.getsockname()[1])
        feeder._condition = DelayedCondition()
        feeder.run.__globals__["socket"] = SimpleNamespace(
            AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM, socket=MeasuredSender
        )
        started = time.monotonic()
        deadline = started + CAPTURE_TIMEOUT_SECONDS
        stopped = False
        feeder.start()
        try:
            with capture.open("xb") as output:
                while True:
                    if time.monotonic() >= deadline:
                        raise ProbeFailure("complete real media capture exceeded its deadline")
                    try:
                        datagram = receiver.recv(65535)
                    except TimeoutError:
                        if time.monotonic() - started > 1 and not feeder.healthy():
                            raise ProbeFailure("real media feeder did not become healthy") from None
                        continue
                    output.write(datagram)
                    received.update(datagram)
                    received_size += len(datagram)
                    if received_size > len(source_payload):
                        raise ProbeFailure("real media capture exceeded its source byte bound")
                    if received_size == len(source_payload):
                        if not feeder.finish():
                            raise ProbeFailure("real media feeder did not stop")
                        stopped = True
                        break
        finally:
            if not stopped and not feeder.finish():
                raise ProbeFailure("real media feeder cleanup failed")
    if (
        feeder.failure_kind is not None
        or received_size != sent.size
        or received.digest() != sent.digest.digest()
        or received.digest() != hashlib.sha256(source_payload).digest()
        or sent.first is None
        or sent.last is None
        or sent.last - sent.first < SOURCE_DURATION_SECONDS - 1
    ):
        raise ProbeFailure("real media transport was incomplete or reordered")
    timestamps = probe_packets(capture, case)
    return (max(timestamps) - min(timestamps)) / (sent.last - sent.first)


def probe_packets(capture, case):
    result = subprocess.run(  # noqa: S603 - fixed ffprobe and generated local capture
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time:stream=codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(capture),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode or result.stderr or len(result.stdout) > 512 * 1024:
        lowered = result.stderr.lower()
        kind = "other" if lowered else "none"
        for marker, category in (
            (b"pes packet size mismatch", "pes-size"),
            (b"packet corrupt", "packet-corrupt"),
            (b"error while decoding", "decode"),
            (b"invalid", "invalid-data"),
        ):
            if marker in lowered:
                kind = category
                break
        print(
            f"Packet probe diagnostic: case={case} exit={result.returncode} "
            f"stdout_bytes={len(result.stdout)} stderr_bytes={len(result.stderr)} "
            f"stderr_kind={kind}",
            flush=True,
        )
        # This subprocess reads only our newly generated finite test fixture,
        # never runtime media, configuration, URLs or credentials. Expose its
        # bounded diagnostic so source errors cannot hide behind 'other'.
        diagnostic = result.stderr[:512].decode("ascii", errors="replace")
        diagnostic = diagnostic.replace(str(capture.parent), "<fixture>")
        diagnostic = re.sub(r"0x[0-9a-fA-F]+", "<address>", diagnostic)
        print(f"Synthetic fixture probe stderr: {diagnostic!r}", flush=True)
        raise ProbeFailure("real media packet probe failed")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    if len(streams) != 1 or any(
        streams[0].get(field) != expected
        for field, expected in {
            "codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "r_frame_rate": "30/1",
        }.items()
    ):
        raise ProbeFailure("real media portrait profile changed")
    timestamps = [float(packet["pts_time"]) for packet in payload["packets"]]
    if len(timestamps) < 60:
        raise ProbeFailure("real media capture had too few video packets")
    return timestamps


def main():
    if os.environ.get("CI_NATIVE_FEEDER_CLOCK") != "isolated-fixture":
        raise ProbeFailure("real media clock probe requires the isolated CI fixture")
    for path, name in (
        (SELF_TEST, "staged self-test"),
        (Path("/usr/bin/ffmpeg"), "FFmpeg"),
        (Path(FFPROBE), "FFprobe"),
    ):
        if not path.is_file():
            raise ProbeFailure(f"isolated fixture prerequisite missing: {name}")
    with tempfile.TemporaryDirectory(prefix="native-feeder-clock-") as temporary:
        directory = Path(temporary)
        transport, payload = make_transport(directory)
        old_rate = media_clock_rate("old", directory, transport, payload)
        fixed_rate = media_clock_rate("fixed", directory, transport, payload)
    if not 0 < old_rate < 0.90:
        raise ProbeFailure("old fixture media clock slowdown was not reproduced")
    if not 0.95 <= fixed_rate <= 1.05:
        raise ProbeFailure("fixed fixture media clock does not follow wall time")
    print(
        "Real portrait media clock verified: "
        f"old_rate={old_rate:.3f} fixed_rate={fixed_rate:.3f}; ordered delivery and cleanup passed",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
        print(f"Real media clock probe failed: {reason}", flush=True)
        raise SystemExit(1) from None
