from __future__ import annotations

import runpy
from pathlib import Path

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"


@pytest.mark.parametrize("rtsp_port,rtmp_port", [(18554, 11936), (1, 65535)])
def test_fps_probe_candidate_changes_only_optional_input_estimation(rtsp_port, rtmp_port):
    api = runpy.run_path(str(NORMALIZER), run_name="_fps_probe_candidate")
    baseline = [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-fflags",
        "+genpts",
        "-rtsp_transport",
        "tcp",
        "-i",
        f"rtsp://127.0.0.1:{rtsp_port}/iphone-live",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-af",
        "aresample=48000:async=1:first_pts=0",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-max_muxing_queue_size",
        "2048",
        "-flush_packets",
        "1",
        "-f",
        "flv",
        f"rtmp://127.0.0.1:{rtmp_port}/relay-output",
    ]
    actual = api["build_ffmpeg_argv"](rtsp_port, rtmp_port)
    input_index = baseline.index("-i")
    assert actual == baseline[:input_index] + ["-fpsprobesize", "0"] + baseline[input_index:]
    assert api["OUTPUT_START_TIMEOUT_SECONDS"] == 6.0
