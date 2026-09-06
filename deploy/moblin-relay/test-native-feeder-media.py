#!/usr/bin/python3
"""Compare old/fixed fixture clocks using real, isolated portrait media on CI."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

SELF_TEST = Path("/opt/moblin-relay/libexec/self-test")
SLATE = Path("/var/lib/moblin-relay/slate.mp4")
FFPROBE = "/usr/bin/ffprobe"
FIXED_CONDITION = "if now >= deadline + len(pending) / self._bytes_per_second:"
PREVIOUS_CONDITION = "if now > deadline:"
WINDOW_SECONDS = 6.0
WAKEUP_JITTER_SECONDS = 0.003


class ProbeFailure(Exception):
    """Only fixed, credential-free diagnostics leave the disposable fixture."""


def load_feeder(case):
    if case not in {"old", "fixed"}:
        raise ProbeFailure("invalid feeder case")
    source = SELF_TEST.read_text(encoding="utf-8")
    if source.count(FIXED_CONDITION) != 1:
        raise ProbeFailure("installed feeder does not contain the tested clock repair")
    if case == "fixed":
        return runpy.run_path(str(SELF_TEST), run_name="_native_feeder_media")
    # Replay exactly the removed comparison, changing no other source behavior.
    source = source.replace(FIXED_CONDITION, PREVIOUS_CONDITION)
    namespace = {"__name__": "_native_feeder_media", "__file__": str(SELF_TEST)}
    exec(compile(source, str(SELF_TEST), "exec"), namespace)  # noqa: S102 - installed test code
    return namespace


def media_clock_rate(case, directory):
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
        feeder = cls(namespace["local_mpegts_remux_command"](SLATE), receiver.getsockname()[1])
        feeder._condition = DelayedCondition()
        feeder.run.__globals__["socket"] = SimpleNamespace(
            AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM, socket=MeasuredSender
        )
        started = time.monotonic()
        deadline = started + WINDOW_SECONDS
        stopped = False
        feeder.start()
        try:
            with capture.open("xb") as output:
                while True:
                    if time.monotonic() >= deadline and not stopped:
                        if not feeder.finish():
                            raise ProbeFailure("real media feeder did not stop")
                        stopped = True
                    try:
                        datagram = receiver.recv(65535)
                    except TimeoutError:
                        if stopped:
                            break
                        if time.monotonic() - started > 1 and not feeder.healthy():
                            raise ProbeFailure("real media feeder did not become healthy") from None
                        continue
                    output.write(datagram)
                    received.update(datagram)
                    received_size += len(datagram)
                    if received_size > 12 * 1024 * 1024:
                        raise ProbeFailure("real media capture exceeded its byte bound")
        finally:
            if not stopped and not feeder.finish():
                raise ProbeFailure("real media feeder cleanup failed")
    if (
        feeder.failure_kind is not None
        or received_size != sent.size
        or received.digest() != sent.digest.digest()
        or sent.first is None
        or sent.last is None
        or sent.last - sent.first < WINDOW_SECONDS - 1
    ):
        raise ProbeFailure("real media transport was incomplete or reordered")
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
    return (max(timestamps) - min(timestamps)) / (sent.last - sent.first)


def main():
    if os.environ.get("CI_NATIVE_FEEDER_CLOCK") != "isolated-fixture":
        raise ProbeFailure("real media clock probe requires the isolated CI fixture")
    with tempfile.TemporaryDirectory(prefix="native-feeder-clock-") as temporary:
        directory = Path(temporary)
        old_rate = media_clock_rate("old", directory)
        fixed_rate = media_clock_rate("fixed", directory)
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
