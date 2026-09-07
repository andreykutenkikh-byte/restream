#!/usr/bin/python3
"""CI-only actual-media regression for the strict sink decoded-frame minimum."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

SELF_TEST = Path("/tmp/adojapan-ci-clock-self-test.py")  # noqa: S108 - exact CI stage
FFMPEG = Path("/usr/bin/ffmpeg")
FFPROBE = Path("/usr/bin/ffprobe")
WORK_SECONDS = 90.0
MAX_MEDIA_BYTES = 8 * 1024 * 1024
FIXED_MINIMUM = 'or decoded_frames["frame_count"] < STRICT_SINK_REQUIRED_VIDEO_FRAMES'
OLD_MINIMUM = 'or decoded_frames["frame_count"] < VIDEO_GOP_FRAMES'
SHORT_REASON = "strict RTMP sink video frame validation failed"


class ProbeFailure(Exception):
    """Only fixed reasons and numeric measurements may leave this fixture."""


def require(condition, reason):
    if not condition:
        raise ProbeFailure(reason)


def load_validator(case):
    require(case in {"old", "fixed"}, "invalid short EOF comparison")
    source = SELF_TEST.read_text(encoding="utf-8")
    require(source.count(FIXED_MINIMUM) == 1, "strict decoded minimum guard is missing")
    if case == "fixed":
        return runpy.run_path(str(SELF_TEST), run_name="_native_short_eof")
    # Execute the repository validator with only its former decoded-count
    # comparison restored. Capture still requests 90; no media gate is copied.
    source = source.replace(FIXED_MINIMUM, OLD_MINIMUM)
    namespace = {"__name__": "_native_short_eof_old", "__file__": str(SELF_TEST)}
    exec(compile(source, str(SELF_TEST), "exec"), namespace)  # noqa: S102 - exact CI stage
    return namespace


def configure(namespace, deadline):
    globals_ = namespace["capture_final_sink_media_segment"].__globals__
    globals_.update(FFMPEG=FFMPEG, FFPROBE=FFPROBE, SELF_TEST_STAGE_FILE="")
    require(globals_["STRICT_SINK_REQUIRED_VIDEO_FRAMES"] == 90, "strict target changed")
    original_run, original_probe = globals_["run"], globals_["run_probe"]

    def remaining(maximum):
        left = deadline - time.monotonic()
        require(left > 0, "short EOF work deadline exceeded")
        return min(left, maximum)

    def bounded_run(command, **kwargs):
        kwargs["timeout"] = remaining(min(kwargs.get("timeout", 25), 25))
        return original_run(command, **kwargs)

    def bounded_probe(command, *, timeout):
        return original_probe(command, timeout=remaining(min(timeout, 25)))

    globals_.update(run=bounded_run, run_probe=bounded_probe)
    return globals_


def generate_sample(frames, work, deadline):
    require(frames in {61, 89, 90}, "invalid short EOF frame count")
    duration = f"{frames / 30:.9f}"
    output = work / f"finite-{frames}.flv"
    command = [
        str(FFMPEG),
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-xerror",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:s=1080x1920:r=30:d={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:sample_rate=48000:duration={duration}",
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
        "8000k",
        "-minrate",
        "8000k",
        "-maxrate",
        "8000k",
        "-bufsize",
        "16000k",
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1:filler=1:bframes=0:repeat-headers=1",
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
        duration,
        "-fs",
        str(MAX_MEDIA_BYTES),
        "-f",
        "flv",
        str(output),
    ]
    left = deadline - time.monotonic()
    require(left > 0, "short EOF work deadline exceeded")
    generated = subprocess.run(  # noqa: S603 - fixed local synthetic-media command
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=min(25, left),
        check=False,
    )
    require(
        generated.returncode == 0
        and not generated.stderr
        and output.is_file()
        and 0 < output.stat().st_size < MAX_MEDIA_BYTES,
        "short EOF sample generation failed",
    )
    output.chmod(0o600)
    return output


def run_case(case, frames, source, work, deadline):
    namespace = load_validator(case)
    globals_ = configure(namespace, deadline)
    # A finite file substitutes only for the RTMP input, causing actual clean
    # EOF without a listener or publisher. Every other capture/validator option
    # is the repository implementation, including 90 frames and 15 seconds.
    original_reader = globals_["run_capture_reader"]
    diagnostic = namespace["CaptureReaderProgress"]()
    reader_calls = []

    def finite_reader(command, progress, *, timeout):
        require(
            timeout == 15 and command[command.index("-frames:v") + 1] == "90",
            "strict reader target or deadline changed",
        )
        require(deadline - time.monotonic() >= 15, "short EOF reader budget unavailable")
        local = list(command)
        local[local.index("-i") + 1] = str(source)
        result = original_reader(local, progress, timeout=timeout)
        reader_calls.append(result.returncode)
        return result

    globals_["run_capture_reader"] = finite_reader
    signature = namespace["stream_signature"](source, include_gop=False)["streams"][0]
    require(
        signature["profile"] == "Main"
        and signature["level"] == 40
        and signature["has_b_frames"] == 0,
        "short EOF source video profile changed",
    )
    require(
        namespace["video_gop_signature"](source)["frame_count"] == frames,
        "short EOF source frame count changed",
    )
    captured, size = namespace["capture_final_sink_media_segment"](
        work,
        1,
        lambda _command: None,
        reader_diagnostic=diagnostic,
    )
    require(reader_calls == [0], "short EOF reader did not finish cleanly once")
    require(diagnostic.snapshot()["reader_frames"] == frames, "short EOF capture count changed")
    outcome = "PASS"
    try:
        result = namespace["validate_final_sink_media_segment"](
            captured,
            size,
            signature,
            lambda _command: None,
            segment_index=1,
        )
    except namespace["TestFailure"] as error:
        require(
            case == "fixed" and frames < 90 and str(error) == SHORT_REASON,
            "short EOF validator failed outside the decoded minimum",
        )
        outcome = "FAIL_MINIMUM"
    else:
        require(case == "old" or frames == 90, "short EOF was incorrectly accepted")
        require(result["video_frames"] == frames, "decoded frame count changed")
    require(not captured.exists(), "short EOF capture cleanup failed")
    print(json.dumps({"case": case, "frames": frames, "reader_exit": 0, "validator": outcome}))


def main():
    require(os.environ.get("CI_NATIVE_SHORT_EOF") == "isolated-fixture", "CI-only short EOF gate")
    deadline = time.monotonic() + WORK_SECONDS
    require(
        SELF_TEST.is_file() and FFMPEG.is_file() and FFPROBE.is_file(),
        "short EOF prerequisites missing",
    )
    os.environ.pop("MOBLIN_RELAY_SELF_TEST_STAGE_FILE", None)
    with TemporaryDirectory(prefix="native-short-eof-") as temporary:
        work = Path(temporary)
        for frames in (61, 89, 90):
            source = generate_sample(frames, work, deadline)
            for case in ("old", "fixed"):
                case_work = work / f"{case}-{frames}"
                case_work.mkdir(mode=0o700)
                run_case(case, frames, source, case_work, deadline)
    print("Strict sink decoded minimum verified; owned temporary media cleanup passed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never print FFmpeg stderr, commands, transport URLs or local paths.
        print("Strict sink decoded minimum regression failed", flush=True)
        raise SystemExit(1) from None
