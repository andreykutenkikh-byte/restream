"""Decoded counts, not clean EOF, transport bytes or progress, admit each segment."""

from __future__ import annotations

import runpy
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from test_moblin_relay_bundle import BUNDLE, load_self_test


def test_linux_actual_media_gate_is_mandatory_after_staging_and_before_onboarding():
    workflow = yaml.safe_load((BUNDLE.parents[1] / ".github/workflows/ci.yml").read_text())
    steps = next(
        job["steps"]
        for job in workflow["jobs"].values()
        if any(step.get("name") == "Strict sink decoded-frame minimum" for step in job["steps"])
    )
    names = [step.get("name") for step in steps]
    index = names.index("Strict sink decoded-frame minimum")
    assert index == names.index("Native fixture media clock under scheduler jitter") + 1
    assert index < names.index("SSH bootstrap and native Moblin Relay end-to-end smoke")
    step = steps[index]
    assert "if" not in step and "continue-on-error" not in step
    assert "CI_NATIVE_SHORT_EOF=isolated-fixture" in step["run"]
    assert "ci-ssh-target" in step["run"]
    assert "python3 - < deploy/moblin-relay/test-native-short-eof.py" in step["run"]
    assert "|| true" not in step["run"]
    assert "/tmp/adojapan-ci-clock-self-test.py" in steps[index - 1]["run"]  # noqa: S108 - text only


@pytest.fixture
def media_validator(monkeypatch, tmp_path):
    namespace = load_self_test()
    validate = namespace["validate_final_sink_media_segment"]
    globals_ = validate.__globals__
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "Main",
        "level": 40,
        "has_b_frames": 0,
        "width": 1080,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "30/1",
    }
    audio = {
        "codec_type": "audio",
        "codec_name": "aac",
        "profile": "LC",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    }
    decoded = {
        "ffprobe_exit": 0,
        "frame_count": 90,
        "unexpected_dimension_frames": 0,
        "unexpected_pixel_format_frames": 0,
        "decode_error_flags": False,
        "stderr_empty": True,
        "presentation_timestamp_count": 90,
        "maximum_presentation_timestamp_gap_seconds": 0.034,
        "strict_presentation_timestamps_monotonic": True,
        "presentation_frame_rate_matches": True,
    }
    decoded_audio = {
        "ffprobe_exit": 0,
        "frame_count": 140,
        "presentation_timestamp_count": 140,
        "presentation_timestamp_steps_beyond_tolerance": 0,
        "negative_presentation_timestamp_steps": 0,
        "maximum_presentation_timestamp_gap_seconds": 0.022,
        "stderr_empty": True,
    }
    timestamps = {
        "pts_present_for_every_packet": True,
        "ffprobe_exit": 0,
        "stderr_empty": True,
        "dts_within_tolerance": True,
        "negative_dts_steps": {},
        "dts_backward_events_beyond_tolerance": {},
        "max_pts_dts_offset_seconds": {0: 0, 1: 0},
        "max_dts_gap_seconds": {0: 0.034, 1: 0.022},
        "max_sorted_pts_gap_seconds": {0: 0.034, 1: 0.022},
        "audio_video_duration_difference_seconds": 0.01,
        "audio_video_end_difference_seconds": 0.01,
    }
    gop = {"keyframe_indexes": [0, 60], "interval_frames": [60]}
    child = SimpleNamespace(returncode=0)
    guarded = []

    def decode(command, **kwargs):
        assert guarded[-1] == command
        assert "-xerror" in command and command[command.index("-err_detect") + 1] == "explode"
        assert kwargs["timeout"] == 15
        return child

    monkeypatch.setitem(
        globals_, "stream_signature", lambda *_args, **_kwargs: {"streams": [video, audio]}
    )
    monkeypatch.setitem(globals_, "video_gop_signature", lambda _: gop)
    monkeypatch.setitem(globals_, "run", decode)
    monkeypatch.setitem(globals_, "analyze_decoded_video_frames", lambda _: decoded)
    monkeypatch.setitem(globals_, "analyze_decoded_audio_timestamps", lambda _: decoded_audio)
    monkeypatch.setitem(globals_, "analyze_timestamps", lambda _: timestamps)
    source = tmp_path / "bounded.flv"

    def check(frames):
        source.write_bytes(b"f" * namespace["SLATE_CAPTURE_GROWTH_BYTES"])
        decoded.update(frame_count=frames, presentation_timestamp_count=frames)
        # Other probe results are intentionally valid, even for 0/59/60, to
        # isolate the decoded minimum rather than an earlier GOP rejection.
        return validate(source, source.stat().st_size, dict(video), guarded.append, segment_index=1)

    return SimpleNamespace(
        namespace=namespace,
        globals=globals_,
        check=check,
        source=source,
        video=video,
        decoded=decoded,
        audio=decoded_audio,
        timestamps=timestamps,
        gop=gop,
        child=child,
        guard=guarded.append,
    )


@pytest.mark.parametrize("frames", [0, 59, 60, 61, 89])
def test_each_short_decoded_segment_fails_despite_other_valid_gates(media_validator, frames):
    with pytest.raises(
        media_validator.namespace["TestFailure"], match="video frame validation failed"
    ):
        media_validator.check(frames)
    assert not media_validator.source.exists()


@pytest.mark.parametrize("frames", [90, 91])
def test_valid_minimum_and_longer_segments_pass(media_validator, frames):
    assert media_validator.check(frames)["video_frames"] == frames
    assert not media_validator.source.exists()


@pytest.mark.parametrize(
    "failure", ["format", "decode", "decoded", "gop", "pts", "dts", "audio", "av_sync"]
)
def test_ninety_frames_do_not_bypass_other_media_gates(media_validator, failure):
    state = media_validator
    if failure == "format":
        state.video["width"] = 1280
    elif failure == "decode":
        state.child.returncode = 7
    elif failure == "decoded":
        state.decoded["decode_error_flags"] = True
    elif failure == "gop":
        state.gop["interval_frames"] = [59]
    elif failure == "pts":
        state.decoded["strict_presentation_timestamps_monotonic"] = False
    elif failure == "dts":
        state.timestamps["negative_dts_steps"] = {0: 1}
    elif failure == "audio":
        state.audio["ffprobe_exit"] = 7
    elif failure == "av_sync":
        state.timestamps["audio_video_end_difference_seconds"] = 1.0
    with pytest.raises(state.namespace["TestFailure"]):
        state.check(90)
    assert not state.source.exists()


def test_zero_exit_size_and_progress_cannot_replace_decoded_frames(
    media_validator, monkeypatch, tmp_path
):
    namespace = media_validator.namespace

    def reader(command, diagnostic, *, timeout):
        assert command[command.index("-frames:v") + 1] == "90" and timeout == 15
        Path(command[-1]).write_bytes(b"f" * 100000)
        diagnostic.observe_line(b"frame=90", progress=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setitem(media_validator.globals, "run_capture_reader", reader)
    captured, size = namespace["capture_final_sink_media_segment"](tmp_path, 1, lambda _: None)
    media_validator.decoded.update(frame_count=61, presentation_timestamp_count=61)
    with pytest.raises(namespace["TestFailure"], match="video frame validation failed"):
        namespace["validate_final_sink_media_segment"](
            captured,
            size,
            media_validator.video,
            media_validator.guard,
        )
    assert not captured.exists()


def test_capture_and_validator_share_one_required_count(media_validator, monkeypatch, tmp_path):
    state = media_validator
    assert state.globals["STRICT_SINK_REQUIRED_VIDEO_FRAMES"] == 90
    monkeypatch.setitem(state.globals, "STRICT_SINK_REQUIRED_VIDEO_FRAMES", 91)

    def reader(command, _diagnostic, *, timeout):
        assert command[command.index("-frames:v") + 1] == "91" and timeout == 15
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setitem(state.globals, "run_capture_reader", reader)
    with pytest.raises(state.namespace["TestFailure"], match="read timed out"):
        state.namespace["capture_final_sink_media_segment"](tmp_path, 1, lambda _: None)
    with pytest.raises(state.namespace["TestFailure"], match="video frame validation failed"):
        state.check(90)


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    namespace = runpy.run_path(str(BUNDLE / "test-native-short-eof.py"), run_name="_short_eof_test")
    monkeypatch.setitem(namespace["load_validator"].__globals__, "SELF_TEST", BUNDLE / "self-test")
    return namespace


def test_actual_media_helper_is_explicitly_ci_gated_before_execution(helper, monkeypatch):
    monkeypatch.delenv("CI_NATIVE_SHORT_EOF", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed"))
    with pytest.raises(helper["ProbeFailure"], match="CI-only"):
        helper["main"]()


def test_actual_media_entrypoint_redacts_unexpected_errors(monkeypatch, capsys):
    monkeypatch.setenv("CI_NATIVE_SHORT_EOF", "isolated-fixture")

    def error(_path):
        raise RuntimeError("PRIVATE_STDERR rtmp://private.invalid/secret")

    monkeypatch.setattr(Path, "is_file", error)
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(BUNDLE / "test-native-short-eof.py"), run_name="__main__")
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == "Strict sink decoded minimum regression failed\n"
    assert output.err == ""


@pytest.mark.parametrize("case", ["old", "fixed"])
def test_loaded_validator_dependency_closure_and_single_old_guard(helper, case):
    namespace = helper["load_validator"](case)
    globals_ = helper["configure"](namespace, time.monotonic() + 90)
    validate = namespace["validate_final_sink_media_segment"]
    assert validate.__globals__ is globals_
    assert namespace["capture_final_sink_media_segment"].__globals__ is globals_
    assert globals_["STRICT_SINK_REQUIRED_VIDEO_FRAMES"] == 90
    assert ("STRICT_SINK_REQUIRED_VIDEO_FRAMES" in validate.__code__.co_names) == (case == "fixed")
    assert (
        "STRICT_SINK_REQUIRED_VIDEO_FRAMES"
        in namespace["capture_final_sink_media_segment"].__code__.co_names
    )


def test_actual_media_helper_generates_one_finite_sample_per_count_for_both_validators(
    helper, monkeypatch
):
    events = []
    monkeypatch.setenv("CI_NATIVE_SHORT_EOF", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _: True)

    def sample(frames, work, deadline):
        assert deadline > time.monotonic()
        events.append(("generate", frames))
        return work / f"finite-{frames}.flv"

    def case(mode, frames, source, _work, _deadline):
        events.append((mode, frames, source.name))

    monkeypatch.setitem(helper["main"].__globals__, "generate_sample", sample)
    monkeypatch.setitem(helper["main"].__globals__, "run_case", case)
    assert helper["main"]() == 0
    assert events == [
        item
        for frames in (61, 89, 90)
        for item in [
            ("generate", frames),
            ("old", frames, f"finite-{frames}.flv"),
            ("fixed", frames, f"finite-{frames}.flv"),
        ]
    ]
