from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-reader-clock.py"


@pytest.fixture
def helper():
    return runpy.run_path(str(HELPER), run_name="_reader_clock_test")


def frames(rate=1.0, start=0, count=241):
    return [
        (index, index / 30, start + index / 30 / rate, index % 60 == 0) for index in range(count)
    ]


@pytest.mark.parametrize("key,kind", [(0, "P"), (1, "I")])
def test_showinfo_parser_keeps_only_numeric_frame_evidence(helper, key, kind):
    line = (
        f"[Parsed_showinfo_0 @ 0xabc123] n:   60 pts: 180000 pts_time:2 "
        f"pos: 99 fmt:yuv420p s:1080x1920 iskey:{key} type:{kind} checksum:123\n"
    ).encode()
    assert helper["parse_frame"](line, 10.0) == (60, 2.0, 10.0, bool(key))
    assert helper["parse_frame"](b"rtmp://secret.invalid/key config stderr\n", 10.0) is None
    assert helper["parse_frame"](b"x" * 2049, 10.0) is None


def test_showinfo_parser_rejects_unbounded_pts_and_fake_keyframe(helper):
    line = b"[Parsed_showinfo_0 @ 0x1] n: 1 pts: 1 pts_time:601 pos: 1 iskey:1 type:I "
    with pytest.raises(helper["ProbeFailure"], match="frame bounds"):
        helper["parse_frame"](line, 1)
    line = line.replace(b"601", b"1").replace(b"type:I", b"type:P")
    with pytest.raises(helper["ProbeFailure"], match="keyframe type"):
        helper["parse_frame"](line, 1)


@pytest.mark.parametrize("case,rate", [("single", 0.298), ("fixed", 0.991)])
def test_phase_requires_two_complete_live_gop_intervals(helper, case, rate):
    sequence = frames(rate)
    assert helper["stable_gops"](sequence[:120], case) is None
    gate, rates = helper["stable_gops"](sequence[:121], case)
    assert gate == sequence[120]
    assert rates == pytest.approx([rate, rate])
    assert helper["validate_phase"](sequence, gate, gate[2] + 0.1, case) == pytest.approx(
        2 / rate - 0.1, abs=1e-6
    )


def test_initial_probe_burst_cannot_authorize_reader_phase(helper):
    assert helper["stable_gops"](frames(20)[:121], "fixed") is None
    assert helper["stable_gops"](frames(1)[:121], "single") is None


@pytest.mark.parametrize("rate,case", [(0.25, "single"), (1.0, "fixed")])
def test_post_capture_observer_can_finish_next_idr_without_restarting_reader(
    helper, monkeypatch, rate, case
):
    sequence = frames(rate)
    gate = sequence[120]
    clock = SimpleNamespace(now=gate[2] + (15 if case == "single" else 3.5))
    monkeypatch.setattr(helper["time"], "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        helper["time"], "sleep", lambda delay: setattr(clock, "now", clock.now + delay)
    )
    observer = SimpleNamespace(
        snapshot=lambda: [frame for frame in sequence if frame[2] <= clock.now]
    )
    observed = helper["finish_phase"](observer, gate, case, 100, lambda: None)
    assert observed[-1][0] == 240
    assert clock.now == pytest.approx(sequence[240][2], abs=0.006)


def test_post_capture_observation_keeps_outer_deadline(helper, monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(helper["time"], "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        helper["time"], "sleep", lambda delay: setattr(clock, "now", clock.now + delay)
    )
    with pytest.raises(helper["ProbeFailure"], match="post-capture IDR"):
        helper["finish_phase"](
            SimpleNamespace(snapshot=lambda: []), frames()[0], "single", 10.01, lambda: None
        )
    assert 10.01 <= clock.now <= 10.016


@pytest.mark.parametrize(
    "change,error",
    [
        ("late", "missed IDR phase"),
        ("missing", "two subsequent IDRs"),
        ("rate", "capture rate changed"),
        ("gop", "GOP continuity"),
        ("pts", "PTS continuity"),
    ],
)
def test_post_capture_phase_checks_fail_closed(helper, change, error):
    sequence = frames()
    gate = sequence[120]
    spawned = gate[2] + (0.201 if change == "late" else 0.1)
    if change == "missing":
        sequence = sequence[:240]
    elif change in {"rate", "gop", "pts"}:
        index, pts, wall, key = sequence[180]
        sequence[180] = (
            index + (change == "gop"),
            pts + (change == "pts"),
            wall + (change == "rate"),
            key,
        )
    with pytest.raises(helper["ProbeFailure"], match=error):
        helper["validate_phase"](sequence, gate, spawned, "fixed")


def test_only_exact_old_15_second_timeout_with_growing_frames_is_expected(helper):
    class NativeFailure(Exception):
        pass

    failure = NativeFailure("strict RTMP sink media read timed out")
    failure.__cause__ = subprocess.TimeoutExpired(["synthetic"], 15)
    progress = {
        "reader_input": True,
        "reader_output": True,
        "reader_frames": 71,
        "reader_first_frame_seconds": 9,
        "reader_last_frame_seconds": 14,
    }
    predicate = helper["expected_old_timeout"]
    assert predicate(failure, NativeFailure, progress)
    for field, value in [
        ("reader_input", False),
        ("reader_output", False),
        ("reader_frames", 0),
        ("reader_frames", 90),
        ("reader_last_frame_seconds", 9),
    ]:
        assert not predicate(failure, NativeFailure, progress | {field: value})
    failure.__cause__ = subprocess.TimeoutExpired(["synthetic"], 16)
    assert not predicate(failure, NativeFailure, progress)
    failure.__cause__ = OSError("not a timeout")
    assert not predicate(failure, NativeFailure, progress)
    assert not predicate(RuntimeError("other failure"), NativeFailure, progress)


def test_media_invocations_are_continuous_loopback_copy_and_native_audio_contract(helper):
    namespace = {
        "local_mpegts_remux_command": lambda _path: [
            "ffmpeg",
            "-stream_loop",
            "-1",
            "-i",
            "live.mp4",
            "-c",
            "copy",
            "pipe:1",
        ],
        "LIVE_FEED_FIFO_UNITS": 4096,
        "LIVE_FEED_SOCKET_BUFFER_BYTES": 262144,
    }
    remux, publisher, observer = helper["media_commands"](namespace, Path("live.mp4"), 30100, 30101)
    assert remux[remux.index("-stream_loop") + 1] == "-1"
    assert remux[-3:] == ["-bsf:v", "h264_mp4toannexb,dump_extra=freq=keyframe", "pipe:1"]
    assert publisher[publisher.index("-c:v") + 1] == "copy"
    assert publisher[publisher.index("-c:a") + 1] == "aac"
    assert publisher[publisher.index("-af") + 1] == "aresample=48000:async=1:first_pts=0"
    assert publisher[publisher.index("-ac") + 1] == "2"
    assert "overrun_nonfatal" not in " ".join(publisher)
    assert all("127.0.0.1" in argument for argument in publisher if "://" in argument)
    assert publisher[-1] == "rtmp://127.0.0.1:30101/live/sink"
    assert observer[observer.index("-vf") + 1] == "showinfo"
    assert observer[observer.index("-threads") + 1] == "1"
    assert "-re" not in publisher and "-re" not in remux
    config = helper["sink_config"](30101, 30102)
    assert config["rtmpAddress"] == "127.0.0.1:30101"
    assert config["metricsAddress"] == "127.0.0.1:30102"
    assert all(config[key] is False for key in ["api", "rtsp", "srt", "hls", "webrtc", "moq"])
    assert set(config["paths"]) == {"live/sink"}
    assert {item["action"] for item in config["authInternalUsers"][0]["permissions"]} == {
        "read",
        "publish",
        "metrics",
    }
    assert "runOnAvailable" not in str(config)


def test_reused_functions_redirect_globals_without_modifying_strict_reader(
    helper, monkeypatch, tmp_path
):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    namespace = runpy.run_path(str(ROOT / "deploy/moblin-relay/self-test"), run_name="_reader")
    capture = namespace["capture_final_sink_media_segment"]
    code = capture.__code__
    globals_ = helper["configure"](namespace, tmp_path, 30101, 100)
    assert capture.__code__ is code
    assert globals_["SINK_RTMP_PORT"] == 30101
    assert globals_["SELF_TEST_STAGE_FILE"] == ""
    assert globals_["SELF_TEST_PROGRESS_FILE"] == tmp_path / "progress.json"
    assert globals_["LIVE_FIXTURE_DURATION_SECONDS"] == 4
    # The unchanged capture's actual argv and deadline remain 90 frames / 15s.
    calls = []

    def reader(command, diagnostic, *, timeout):
        calls.append((command, timeout))
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setitem(globals_, "run_capture_reader", reader)
    with pytest.raises(namespace["TestFailure"]):
        capture(tmp_path, 1, lambda _command: None)
    command, timeout = calls[0]
    assert timeout == 15 and command[command.index("-frames:v") + 1] == "90"
    assert command[command.index("-i") + 1] == "rtmp://127.0.0.1:30101/live/sink"
    assert "-probesize" not in command and "-analyzeduration" not in command


def test_main_is_explicitly_ci_gated_before_any_process(helper, monkeypatch):
    monkeypatch.delenv("CI_NATIVE_READER_CLOCK", raising=False)
    monkeypatch.setattr(
        helper["subprocess"], "run", lambda *_args, **_kwargs: pytest.fail("spawned")
    )
    with pytest.raises(helper["ProbeFailure"], match="CI-only"):
        helper["main"]()


def test_validation_probe_timeout_is_capped_by_remaining_outer_budget(
    helper, monkeypatch, tmp_path
):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    namespace = runpy.run_path(str(ROOT / "deploy/moblin-relay/self-test"), run_name="_reader")
    clock, calls = SimpleNamespace(now=5.0), []
    monkeypatch.setattr(helper["time"], "monotonic", lambda: clock.now)
    marker = SimpleNamespace(returncode=0, stdout="bounded original output", stderr="")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return marker

    monkeypatch.setattr(helper["subprocess"], "run", run)
    globals_ = helper["configure"](namespace, tmp_path, 30101, 8.0)
    assert globals_["run_probe"](["ffprobe"], timeout=60) is marker
    assert calls[0][1]["timeout"] == 3
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.PIPE
    clock.now = 8.0
    with pytest.raises(helper["ProbeFailure"], match="outer deadline"):
        globals_["run_probe"](["ffprobe"], timeout=60)
    assert len(calls) == 1


def test_wrong_installed_mediamtx_version_stops_before_loading_fixture(helper, monkeypatch):
    monkeypatch.setenv("CI_NATIVE_READER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        helper["subprocess"],
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"v1.20.0"),
    )
    monkeypatch.setattr(
        helper["runpy"], "run_path", lambda *_args, **_kwargs: pytest.fail("loaded")
    )
    with pytest.raises(helper["ProbeFailure"], match="pin mismatch"):
        helper["main"]()
