"""Real FLV copy at 60 fps through a paused, then resumed destination pipe."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, Any, cast

from app.broadcast.selector import MAX_BYTES, MAX_PACKETS, Selector

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)  # noqa: S603
    assert result.returncode == 0, "synthetic_media_command_failed"
    return result.stdout


def case(directory: Path, fixture: Path, pause: float) -> dict[str, Any]:
    output = directory / f"paused-{int(pause)}.flv"
    publisher = subprocess.Popen(  # noqa: S603 - resolved executable, synthetic destination
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "flv",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-f",
            "flv",
            str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert publisher.stdin
    release, paused = threading.Event(), threading.Event()
    events: list[tuple[str, int | None]] = []

    class Sink:
        def __init__(self, pipe: IO[bytes]) -> None:
            self.pipe = pipe
            self.start = time.monotonic()

        def write(self, data: bytes) -> int:
            if time.monotonic() - self.start >= 2 and not release.is_set():
                paused.set()
                assert release.wait(25), "destination_not_released"
            return self.pipe.write(data)

        def flush(self) -> None:
            self.pipe.flush()

    selector = Selector(
        cast(IO[bytes], Sink(publisher.stdin)),
        fps=60,
        diagnostics=lambda c, v: events.append((c, v)),
    )
    source = "synthetic"
    selector.select(source)
    selector.prepare(
        source,
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-i",
            str(fixture),
            "-c",
            "copy",
            "-f",
            "flv",
            "pipe:1",
        ],
    )
    peak_packets = peak_bytes = 0
    try:
        assert paused.wait(10), "destination_never_paused"
        deadline = time.monotonic() + pause
        while time.monotonic() < deadline:
            assert selector.error is None and publisher.poll() is None
            peak_packets = max(peak_packets, len(selector.queue))
            peak_bytes = max(peak_bytes, selector.queued_bytes)
            time.sleep(0.02)
        assert peak_packets > 256, "old_failure_threshold_not_exercised"
        assert peak_packets <= MAX_PACKETS and peak_bytes <= MAX_BYTES
        if pause >= 12:
            assert any(code == "queue_full" for code, _ in events), "backpressure_not_exercised"
        release.set()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            reader = selector.inputs[source]
            if reader.error == "input_ended" and not selector.queue and not selector.inflight:
                break
            assert selector.error is None and publisher.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("queue_did_not_drain")
        assert selector.frames == 1200
        selector.close()
        publisher.stdin.close()
        assert publisher.wait(timeout=10) == 0
        streams = json.loads(
            run(
                [
                    FFPROBE,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_entries",
                    "stream=codec_type,r_frame_rate,nb_read_frames",
                    "-of",
                    "json",
                    str(output),
                ]
            )
        )["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        assert video["r_frame_rate"] == "60/1" and int(video["nb_read_frames"]) == 1200
        assert int(audio["nb_read_frames"]) >= 900
        return {
            "pause_seconds": pause,
            "peak_packets": peak_packets,
            "peak_bytes": peak_bytes,
            "decoded_video_frames": 1200,
            "fps": "60/1",
            "decoded_audio_frames": int(audio["nb_read_frames"]),
            "publisher_restarts": 0,
            "backpressure_observed": any(c == "queue_full" for c, _ in events),
        }
    finally:
        release.set()
        if publisher.poll() is None:
            publisher.kill()
        publisher.wait(timeout=5)
        selector.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory: Path = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    fixture = directory / "source.mp4"
    run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=540x960:rate=60",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-b:v",
            "13000k",
            "-minrate",
            "13000k",
            "-maxrate",
            "13000k",
            "-bufsize",
            "26000k",
            "-x264-params",
            "nal-hrd=cbr:force-cfr=1",
            "-g",
            "120",
            "-bf",
            "2",
            "-c:a",
            "aac",
            str(fixture),
        ]
    )
    report = {
        "status": "PASS",
        "network_isolated": True,
        "youtube_contacted": False,
        "cases": [case(directory, fixture, pause) for pause in (4.0, 12.0)],
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
