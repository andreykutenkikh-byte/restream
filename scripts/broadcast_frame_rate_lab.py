"""Check real FLV frame-rate metadata and packet identity at production probe limits."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from app.broadcast.media_runtime import MediaRuntime
from app.broadcast.selector import Selector, exact, read_tag

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert result.returncode == 0, "synthetic_frame_rate_command_failed"
    return result.stdout


def inspect(path: Path) -> dict[str, Any]:
    counts = {8: 0, 9: 0}
    digests = {8: hashlib.sha256(), 9: hashlib.sha256()}
    rates = []
    with path.open("rb") as stream:
        exact(stream, 13)
        while True:
            try:
                tag = read_tag(stream)
            except EOFError:
                break
            if tag.kind == 18:
                marker = b"\x00\x09framerate\x00"
                index = tag.data.find(marker)
                if index >= 0:
                    rates.append(struct.unpack(">d", tag.data[index + 12 : index + 20])[0])
            elif tag.kind in counts and tag.data[1] == 1:
                counts[tag.kind] += 1
                digests[tag.kind].update(len(tag.data).to_bytes(4, "big") + tag.data)
    return {
        "rates": rates,
        "counts": counts,
        "payloads": {k: v.hexdigest() for k, v in digests.items()},
    }


def copy(directory: Path, fixture: Path, fps: float, slot: str) -> dict[str, Any]:
    output = directory / f"{fixture.stem}-{slot}.flv"
    runtime = MediaRuntime.__new__(MediaRuntime)
    runtime.ffmpeg = FFMPEG
    publisher = subprocess.Popen(  # noqa: S603 - synthetic file, production copy command
        runtime._selector_publisher(str(output)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert publisher.stdin
    selector = Selector(publisher.stdin, fps=fps)
    try:
        selector.select("synthetic")
        selector.prepare(
            "synthetic",
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(fixture),
                "-c",
                "copy",
                "-flvflags",
                "no_duration_filesize",
                "-f",
                "flv",
                "pipe:1",
            ],
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            reader = selector.inputs["synthetic"]
            if reader.error == "input_ended" and not selector.queue and not selector.inflight:
                break
            assert selector.error is None and publisher.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("synthetic_frame_rate_copy_timeout")
        selector.close()
        publisher.stdin.close()
        assert publisher.wait(timeout=5) == 0
        result = inspect(output)
        assert len(result["rates"]) == 1 and math.isclose(result["rates"][0], fps, abs_tol=1e-9)
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
        assert int(video["nb_read_frames"]) == result["counts"][9]
        result.update({"slot": slot, "decoded_frames": int(video["nb_read_frames"])})
        return result
    finally:
        if publisher.poll() is None:
            publisher.kill()
            publisher.wait(timeout=5)
        selector.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    directory: Path = parser.parse_args().directory
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    for numerator, denominator in [(30, 1), (60, 1), (30000, 1001), (60000, 1001)]:
        fps = numerator / denominator
        fixture = directory / f"source-{numerator}-{denominator}.mp4"
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
                f"testsrc2=size=384x640:rate={numerator}/{denominator}",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "4",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-b:v",
                "3000k",
                "-g",
                str(round(fps * 2)),
                "-bf",
                "2",
                "-c:a",
                "aac",
                str(fixture),
            ]
        )
        reference = directory / f"reference-{numerator}-{denominator}.flv"
        run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(fixture),
                "-c",
                "copy",
                "-f",
                "flv",
                str(reference),
            ]
        )
        original = inspect(reference)
        copies = [copy(directory, fixture, fps, slot) for slot in ["PRIMARY", "BACKUP"]]
        for result in copies:
            assert result["counts"] == original["counts"]
            assert result["payloads"] == original["payloads"], "compressed_media_changed"
        cases.append({"source_fps": fps, "slots": copies, "compressed_media_identical": True})
    report = {"status": "PASS", "youtube_contacted": False, "cases": cases}
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
