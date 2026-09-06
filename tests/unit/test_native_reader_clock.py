from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-reader-clock.py"


def test_ci_runs_independent_counterfactual_after_native_failure_without_masking_it():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    native = workflow.split(
        "      - name: SSH bootstrap and native Moblin Relay end-to-end smoke\n", 1
    )[1].split("      - name:", 1)[0]
    reader = workflow.split("      - name: Strict native reader clock counterfactual\n", 1)[
        1
    ].split("      - name:", 1)[0]
    assert "id: native_onboarding" in native
    assert "run: uv run --locked python scripts/ci_node_onboarding_smoke.py" in native
    assert "if:" not in native
    assert (
        "if: ${{ !cancelled() && (success() || steps.native_onboarding.outcome == 'failure') }}"
        in reader
    )
    assert "CI_NATIVE_READER_CLOCK=isolated-fixture" in reader
    assert "< deploy/moblin-relay/test-native-reader-clock.py" in reader
    assert "continue-on-error" not in native + reader
    assert "|| true" not in native + reader


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


@pytest.mark.parametrize(
    "duration,offset,reason",
    [
        (4, 4.0106875, "frame_continuity"),
        (8, 8.000020833, "eof"),
    ],
)
def test_actual_observer_rejects_padding_seam_but_accepts_aligned_codec_period(
    helper, capsys, duration, offset, reason
):
    # FFmpeg5.1 seek_to_start uses the largest max_pts-min_pts+final-unit
    # across copied streams. These codec-grid endpoints model that formula;
    # the CI remux/ffprobe proves the actual generated files independently.
    seam = duration * 30
    pts = [
        index / 30 if index < seam else offset + (index - seam) / 30 for index in range(seam + 3)
    ]
    lines = [b"rtmp://secret.invalid/key ignored stderr\n"]
    for index, value in enumerate(pts):
        key = index % 60 == 0
        lines.append(
            (
                f"[Parsed_showinfo_0 @ 0x1] n: {index} pts: {index} pts_time:{value:.6f} "
                f"pos: 1 iskey:{int(key)} type:{'I' if key else 'P'} checksum:1\n"
            ).encode()
        )
    observer = helper["PhaseObserver"](io.BytesIO(b"".join(lines)))
    observer.run()
    assert observer.failure["reason"] == reason
    assert len(observer.frames) == (seam if duration == 4 else seam + 3)
    if duration == 4:
        assert observer.failure["frame_delta"] == 1
        assert observer.failure["pts_delta"] == pytest.approx(0.044021, abs=1e-6)
    with pytest.raises(helper["ProbeFailure"], match="observer failed"):
        observer.snapshot()
    evidence = capsys.readouterr().out
    assert "secret" not in evidence and "rtmp" not in evidence
    assert json.loads(evidence)["observer_failure"]["reason"] == reason


@pytest.mark.parametrize(
    "payload,reason", [(b"x" * 2049, "line_bound"), (b"partial", "line_bound")]
)
def test_observer_line_failure_is_bounded_and_does_not_print_payload(helper, payload, reason):
    observer = helper["PhaseObserver"](io.BytesIO(payload))
    observer.run()
    assert observer.failure == {
        "reason": reason,
        "line_bytes": len(payload),
        "frame_count": 0,
        "last_frame": None,
        "last_pts": None,
        "frame_delta": None,
        "pts_delta": None,
    }


def test_observer_io_failure_never_carries_exception_text(helper):
    def fail(_limit):
        raise OSError("rtmp://secret.invalid/key")

    observer = helper["PhaseObserver"](SimpleNamespace(readline=fail))
    observer.run()
    assert observer.failure["reason"] == "pipe_io"
    assert "secret" not in json.dumps(observer.failure)


def loop_probe_payload(duration, seam_error=0):
    seam = duration * 30
    return json.dumps(
        {
            "streams": [
                {
                    "codec_name": "h264",
                    "profile": "Main",
                    "level": 40,
                    "width": 1080,
                    "height": 1920,
                    "r_frame_rate": "30/1",
                }
            ],
            "packets": [
                {
                    "pts_time": 1.4 + index / 30 + (seam_error if index >= seam else 0),
                    "flags": "K_" if index % 60 == 0 else "__",
                }
                for index in range(seam + 6)
            ],
        }
    )


@pytest.mark.parametrize("duration,seam_error", [(4, 0.0106875), (8, 0.000020833)])
def test_source_seam_accepts_only_original_negative_and_aligned_positive(
    helper, duration, seam_error
):
    evidence = helper["validate_loop_packets"](loop_probe_payload(duration, seam_error), duration)
    assert evidence["seam_frame"] == duration * 30
    assert evidence["seam_error_seconds"] == pytest.approx(seam_error, abs=1e-6)


def test_source_seam_count_failure_retains_only_bounded_numeric_evidence(helper, capsys):
    data = json.loads(loop_probe_payload(4))
    data["packets"] = data["packets"][:120]
    data["secret"] = "rtmp://secret.invalid/key"
    with pytest.raises(helper["ProbeFailure"], match="packet count failed"):
        helper["validate_loop_packets"](json.dumps(data), 4)
    assert json.loads(capsys.readouterr().out) == {
        "source_loop_probe": {
            "duration_seconds": 4,
            "video_packets": 120,
            "first_pts_seconds": 1.4,
            "last_pts_seconds": 5.366667,
        }
    }


@pytest.mark.parametrize(
    "change",
    ["no_old_seam", "bad_new_seam", "extra_gap", "gop", "profile", "count", "bounds", "size"],
)
def test_source_seam_negative_cannot_hide_unrelated_failure(helper, change):
    duration = 8 if change == "bad_new_seam" else 4
    data = json.loads(loop_probe_payload(duration, 0 if change == "no_old_seam" else 0.0106875))
    if change == "extra_gap":
        data["packets"][20]["pts_time"] += 0.01
    elif change == "gop":
        data["packets"][60]["flags"] = "__"
    elif change == "profile":
        data["streams"][0]["profile"] = "High"
    elif change == "count":
        data["packets"] = data["packets"][:120]
    elif change == "bounds":
        data["packets"][1]["pts_time"] = float("inf")
    payload = " " * 131073 if change == "size" else json.dumps(data)
    with pytest.raises(helper["ProbeFailure"]):
        helper["validate_loop_packets"](payload, duration)


@pytest.mark.parametrize("duration,guessed_rate", [(4, "120/1"), (8, "30/1")])
def test_prepared_source_uses_packet_clock_across_only_the_designed_negative_seam(
    helper, duration, guessed_rate
):
    # Actual FFmpeg 5.1.2 prepared-TS loops: 4s has a 10.678ms seam error;
    # 8s has an 11us rounding error. Only the invalid loop confuses its guess.
    data = json.loads(loop_probe_payload(duration, 0.010678 if duration == 4 else 0.000011))
    data["streams"][0]["r_frame_rate"] = guessed_rate
    helper["validate_loop_packets"](json.dumps(data), duration)
    if duration == 8:
        data["streams"][0]["r_frame_rate"] = "120/1"
        with pytest.raises(helper["ProbeFailure"], match="nominal frame rate"):
            helper["validate_loop_packets"](json.dumps(data), duration)


def transport_namespace(run):
    return {
        "capture_final_sink_media_segment": SimpleNamespace(
            __globals__={"run": run, "FFPROBE": "ffprobe"}
        ),
        "local_mpegts_remux_command": lambda path: [
            "ffmpeg",
            "-loglevel",
            "quiet",
            "-stream_loop",
            "-1",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ],
    }


@pytest.mark.parametrize("change", ["valid", "rate", "profile", "stderr", "oversize"])
def test_preconversion_applies_both_filters_once_and_retains_original_video_contract(
    helper, tmp_path, change
):
    live = tmp_path / "live.mp4"
    live.write_bytes(b"fixture")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"transport")
        if change in {"stderr", "oversize"}:
            kwargs["stderr"].write(b"x" * (65537 if change == "oversize" else 1))
        return SimpleNamespace(returncode=0)

    namespace = transport_namespace(run)
    video = helper["VIDEO_CONTRACT"] | {"r_frame_rate": "30/1"}
    if change == "rate":
        video["r_frame_rate"] = "120/1"
    elif change == "profile":
        video["profile"] = "High"
    namespace["stream_signature"] = lambda path, **kwargs: {"streams": [video]}
    if change == "valid":
        assert helper["prepare_loop_source"](namespace, live, tmp_path) == (
            tmp_path / "prepared-source.ts"
        )
    else:
        with pytest.raises(helper["ProbeFailure"]):
            helper["prepare_loop_source"](namespace, live, tmp_path)
    if change in {"rate", "profile"}:
        assert calls == []
        return
    command, options = calls[0]
    assert "-stream_loop" not in command and "-xerror" in command
    assert command[command.index("-bsf:v") + 1] == "h264_mp4toannexb,dump_extra=freq=keyframe"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-loglevel") + 1] == "error"
    assert command[command.index("-fs") + 1] == str(11 * 1024**2)
    assert options["timeout"] == 10


@pytest.mark.parametrize(
    "change", ["linux", "windows", "generic", "extra", "lookalike", "empty", "count", "pts", "rate"]
)
def test_original_loop_requires_exact_bsf_eof_pairs_and_complete_first_cycle(
    helper, tmp_path, capsys, change
):
    address = "00000145d4757340" if change == "windows" else "0xabc123"
    newline = "\r\n" if change == "windows" else "\n"
    pair = (
        f"[bsf_list @ {address}] A non-NULL packet sent after an EOF.{newline}"
        f"Error applying bitstream filters to an output packet for stream #0:0.{newline}"
    ).encode()
    errors = pair * 6
    if change == "generic":
        errors = b"Input/output error\n"
    elif change == "extra":
        errors += b"rtmp://secret.invalid/key\n"
    elif change == "lookalike":
        errors = errors.replace(b"stream #0:0", b"stream #0:1")
    elif change == "empty":
        errors = b""
    data = json.loads(loop_probe_payload(4))
    data["packets"] = data["packets"][: 119 if change == "count" else 120]
    if change == "pts":
        data["packets"][20]["pts_time"] += 0.01
    elif change == "rate":
        data["streams"][0]["r_frame_rate"] = "120/1"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"transport")
            kwargs["stderr"].write(errors)
        else:
            kwargs["stdout"].write(json.dumps(data).encode())
        return SimpleNamespace(returncode=0)

    namespace = transport_namespace(run)
    if change in {"linux", "windows"}:
        helper["verify_original_bsf_loop"](namespace, tmp_path / "live.mp4", tmp_path)
        evidence = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert evidence == {
            "source_bsf_eof": {"video_packets": 120, "reason": "explicit_bsf_after_eof"}
        }
    else:
        with pytest.raises(helper["ProbeFailure"]):
            helper["verify_original_bsf_loop"](namespace, tmp_path / "live.mp4", tmp_path)
        assert "secret" not in capsys.readouterr().out
    command = calls[0]
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert command[command.index("-bsf:v") + 1] == helper["ANNEX_B_FILTERS"]
    assert command[command.index("-t") + 1] == "4.2"
    assert "-xerror" not in command  # Finish the known first cycle for its strict packet probe.


@pytest.mark.parametrize("mode", ["valid", "oversize", "stderr"])
def test_source_seam_runs_bounded_copy_and_checks_output_before_reading(
    helper, tmp_path, monkeypatch, mode
):
    live = tmp_path / "live.mp4"
    live.write_bytes(b"fixture")
    calls, reads = [], []
    read_text = Path.read_text

    def read(path, **kwargs):
        reads.append(path)
        return read_text(path, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ffmpeg":
            (tmp_path / "loop-seam.ts").write_bytes(b"transport")
        else:
            payload = b"x" * 131073 if mode == "oversize" else loop_probe_payload(8).encode()
            kwargs["stdout"].write(payload)
            if mode == "stderr":
                kwargs["stderr"].write(b"rtmp://secret.invalid/key")
        return SimpleNamespace(returncode=0)

    def capture():
        pass

    globals_ = capture.__globals__.copy() | {
        "run": run,
        "FFPROBE": "/usr/bin/ffprobe",
    }
    namespace = {
        "capture_final_sink_media_segment": SimpleNamespace(__globals__=globals_),
        "local_mpegts_remux_command": lambda _path: [
            "ffmpeg",
            "-loglevel",
            "quiet",
            "-stream_loop",
            "-1",
            "-i",
            str(live),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        "LIVE_FEED_FIFO_UNITS": 4096,
        "LIVE_FEED_SOCKET_BUFFER_BYTES": 262144,
    }
    if mode == "valid":
        helper["verify_source_loop"](namespace, live, tmp_path, 8)
        assert reads == [tmp_path / "loop-seam.probe.json"]
    else:
        message = "output bound" if mode == "oversize" else "probe failed"
        with pytest.raises(helper["ProbeFailure"], match=message):
            helper["verify_source_loop"](namespace, live, tmp_path, 8)
        assert reads == []
    command, options = calls[0]
    assert "0:v:0" in command and "0:a:0" in command
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-t") + 1] == "8.2"
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert command[command.index("-fs") + 1] == str(11 * 1024**2)
    assert "-bsf:v" not in command and "-xerror" in command
    assert command[command.index("-loglevel") + 1] == "error"
    assert options["timeout"] == 10
    command, options = calls[1]
    assert command[command.index("-select_streams") + 1] == "v:0"
    assert "-show_packets" in command and options["timeout"] == 10
    assert helper["WORK_SECONDS"] == 132


@pytest.mark.parametrize("case,rate", [("single", 0.298), ("fixed", 0.991)])
def test_phase_requires_one_complete_source_period(helper, case, rate):
    sequence = frames(rate, count=361)
    assert helper["stable_gops"](sequence[:240], case) is None
    gate, rates = helper["stable_gops"](sequence[:241], case)
    assert gate == sequence[240]
    assert rates == pytest.approx([rate])
    assert helper["validate_phase"](
        sequence, gate, gate[2] + 0.1, case, tuple(sequence[:241])
    ) == pytest.approx(2 / rate - 0.1, abs=1e-6)


def test_initial_probe_burst_cannot_authorize_reader_phase(helper):
    assert helper["stable_gops"](frames(20), "fixed") is None
    assert helper["stable_gops"](frames(1), "single") is None


def frames_with_gop_wall_intervals(intervals):
    sequence, elapsed = [], 0.0
    for gop, interval in enumerate(intervals):
        sequence.extend(
            (gop * 60 + index, gop * 2 + index / 30, elapsed + interval * index / 60, index == 0)
            for index in range(60)
        )
        elapsed += interval
    sequence.append((len(intervals) * 60, len(intervals) * 2, elapsed, True))
    return sequence


@pytest.mark.parametrize(
    "case,intervals",
    [
        ("single", [7.032, 6.781, 7.776805, 6.242581]),
        ("fixed", [1.989599, 1.932437, 2.222995, 1.761156]),
    ],
)
def test_actual_complementary_arrival_variation_is_not_a_wrong_source_clock(
    helper, case, intervals
):
    # Actual pinned-MediaMTX/FFmpeg5.1.2 loopback measurements using QPC:
    # the fixed following GOP pair reads .900/1.136, but repays its delay.
    # Keep the same rate band on all four GOPs of the complete source period.
    sequence = frames_with_gop_wall_intervals(intervals)
    low, high = helper["RATE_BOUNDS"][case]
    assert not all(low <= 2 / interval <= high for interval in intervals)
    gate, rates = helper["stable_gops"](sequence, case)
    assert gate == sequence[-1]
    assert rates == pytest.approx([8 / sum(intervals)])
    gate = sequence[120]
    prior = tuple(sequence[:121])
    # Model eviction of all pre-gate frames from the live observer deque.
    assert helper["validate_phase"](sequence[121:], gate, gate[2] + 0.1, case, prior) == (
        pytest.approx(intervals[2] - 0.1)
    )
    evidence = helper["phase_evidence"](sequence, case, 0, sequence[-1][2], {}, 1125000, 10528)
    assert evidence["period_rate"] == pytest.approx(rates[0], abs=1e-6)


@pytest.mark.parametrize(
    "case,rate",
    [("single", 0.249999), ("single", 0.310001), ("fixed", 0.949999), ("fixed", 1.050001)],
)
def test_complete_period_still_rejects_source_clock_just_outside_original_rate_band(
    helper, case, rate
):
    sequence = frames(rate)
    assert helper["stable_gops"](sequence, case) is None
    gate = sequence[120]
    with pytest.raises(helper["ProbeFailure"], match="capture rate changed"):
        helper["validate_phase"](sequence, gate, gate[2] + 0.1, case, tuple(sequence[:121]))


@pytest.mark.parametrize("change", ["missing", "replaced_gate", "prior_pts", "extra_gop"])
def test_period_validation_keeps_immutable_prior_context_and_all_observed_gop_checks(
    helper, change
):
    sequence = frames(count=301)
    gate = sequence[120]
    prior = tuple(sequence[:121])
    if change == "missing":
        prior = prior[61:]
    elif change == "replaced_gate":
        prior = (*prior[:-1], (gate[0], gate[1], gate[2] + 0.01, True))
    elif change == "prior_pts":
        prior = ((0, 0.01, 0, True), *prior[1:])
    else:
        index, pts, wall, key = sequence[300]
        sequence[300] = (index + 1, pts, wall, key)
    with pytest.raises(helper["ProbeFailure"]):
        helper["validate_phase"](sequence, gate, gate[2] + 0.1, "fixed", prior)


def test_added_period_measurement_budget_does_not_change_strict_reader_or_phase_limits(helper):
    # Two added 2s media intervals per clock need <=16 +4.211 wall seconds.
    added = 4 / helper["RATE_BOUNDS"]["single"][0] + 4 / helper["RATE_BOUNDS"]["fixed"][0]
    assert 20 < added < 22
    assert helper["WORK_SECONDS"] == 110 + 22
    assert helper["PHASE_AGE_SECONDS"] == 0.2 and helper["PHASE_GOPS"] == 4
    source = HELPER.read_text(encoding="utf-8")
    assert "self.frames = deque(maxlen=512)" in source
    assert "prior_frames = tuple(frames)" in source
    assert "phase_deadline = min(deadline - 20, time.monotonic() + 45)" in source


@pytest.mark.parametrize("rate", [0.298, 1.0, 20.0])
def test_phase_diagnostic_distinguishes_live_rate_from_probe_burst_without_changing_gate(
    helper, rate
):
    base, chunk, byte_rate = 1000000.0, 10528, 1125000
    sequence = frames(rate, start=base, count=121)
    accepted = helper["stable_gops"](sequence, "single")
    observed = sequence[-1][2] + 0.05
    measured = {
        "bytes": chunk + byte_rate * 4,
        "first": base,
        "last": sequence[-1][2],
        "secret": "rtmp://secret.invalid/key",
    }
    evidence = helper["phase_evidence"](
        sequence, "single", base - 0.1, observed, measured, byte_rate, chunk
    )
    assert evidence["retained_frames"] == 121 and evidence["retained_keys"] == 3
    assert evidence["samples_valid"] is True
    assert evidence["first_retained_frame_seconds"] == 0.1
    assert evidence["last_idr_age_seconds"] == 0.05
    assert evidence["phase_age_seconds"] == pytest.approx(4 / rate + 0.15, abs=1e-6)
    assert evidence["transport_rate"] == pytest.approx(rate, abs=1e-6)
    assert (
        evidence["idr_intervals"]
        == [{"media_seconds": 2.0, "wall_seconds": round(2 / rate, 6), "rate": round(rate, 6)}] * 2
    )
    assert helper["stable_gops"](sequence, "single") == accepted
    encoded = json.dumps(evidence, allow_nan=False)
    assert len(encoded) < 2048
    assert "secret" not in encoded and "1000000" not in encoded and "rtmp" not in encoded


def test_phase_diagnostic_exposes_no_parsed_frames_without_inventing_a_rate(helper):
    evidence = helper["phase_evidence"]([], "single", 100, 145, {}, 1125000, 10528)
    assert evidence == {
        "case": "single",
        "phase_age_seconds": 45,
        "samples_valid": True,
        "retained_frames": 0,
        "retained_keys": 0,
        "first_retained_frame_seconds": None,
        "last_idr_age_seconds": None,
        "idr_intervals": [],
        "period_rate": None,
        "transport_rate": None,
        "transport_seconds": None,
    }


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, "rtmp://secret.invalid/key"])
def test_phase_diagnostic_rejects_invalid_and_unbounded_numeric_observations(helper, invalid):
    sequence = [(0, invalid, 10, True)]
    evidence = helper["phase_evidence"](
        sequence,
        "rtmp://secret.invalid/key",
        10,
        143,
        {"bytes": invalid, "first": invalid, "last": invalid},
        invalid,
        10528,
    )
    assert evidence["case"] == "unknown"
    assert evidence["samples_valid"] is False
    assert evidence["retained_frames"] is None and evidence["idr_intervals"] == []
    assert evidence["phase_age_seconds"] is None and evidence["transport_rate"] is None
    assert "secret" not in json.dumps(evidence, allow_nan=False)


def test_phase_diagnostic_bounds_samples_and_prints_before_timeout_assertion(helper):
    evidence = helper["phase_evidence"](
        frames(count=513),
        "fixed",
        0,
        17.1,
        {"bytes": 2**31 + 10529, "first": 0, "last": 133},
        1125000,
        10528,
    )
    assert evidence["samples_valid"] is False and evidence["transport_rate"] is None
    assert evidence["transport_seconds"] is None
    source = HELPER.read_text(encoding="utf-8")
    assert source.index('print(json.dumps({"phase_gate":') < source.index(
        'require(gate is not None, "stable live IDR phase not established")'
    )
    assert "phase_deadline = min(deadline - 20, time.monotonic() + 45)" in source
    assert helper["RATE_BOUNDS"] == {"single": (0.25, 0.31), "fixed": (0.95, 1.05)}


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
    prior = tuple(sequence[:121])
    spawned = gate[2] + (0.201 if change == "late" else 0.1)
    if change == "missing":
        sequence = sequence[:240]
    elif change in {"rate", "gop", "pts"}:
        target = 240 if change == "rate" else 180
        index, pts, wall, key = sequence[target]
        sequence[target] = (
            index + (change == "gop"),
            pts + (change == "pts"),
            wall + (change == "rate"),
            key,
        )
    with pytest.raises(helper["ProbeFailure"], match=error):
        helper["validate_phase"](sequence, gate, spawned, "fixed", prior)


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
        "local_mpegts_remux_command": lambda path: [
            "ffmpeg",
            "-loglevel",
            "quiet",
            "-stream_loop",
            "-1",
            "-i",
            str(path),
            "-c",
            "copy",
            "pipe:1",
        ],
        "LIVE_FEED_FIFO_UNITS": 4096,
        "LIVE_FEED_SOCKET_BUFFER_BYTES": 262144,
    }
    remux, publisher, observer = helper["media_commands"](
        namespace, Path("prepared-source.ts"), 30100, 30101
    )
    assert remux[remux.index("-stream_loop") + 1] == "-1"
    assert remux[remux.index("-i") + 1] == "prepared-source.ts"
    assert "-bsf:v" not in remux and "-xerror" in remux
    assert remux[remux.index("-loglevel") + 1] == "error"
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
    assert globals_["LIVE_FIXTURE_DURATION_SECONDS"] == 8
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


def test_main_proves_old_bsf_then_loops_prepared_source_but_validates_original(helper, monkeypatch):
    globals_ = helper["main"].__globals__
    monkeypatch.setenv("CI_NATIVE_READER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        helper["subprocess"],
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"v1.20.1"),
    )
    events, source_globals = [], {}
    namespace = {"generate_live": lambda work: work / "live.mp4"}
    monkeypatch.setattr(
        helper["runpy"], "run_path", lambda *_args, **_kwargs: {"load_feeder": lambda _: namespace}
    )
    monkeypatch.setitem(globals_, "configure", lambda *_args: source_globals)
    monkeypatch.setitem(
        globals_, "verify_original_bsf_loop", lambda _, live, work: events.append(("old", live))
    )

    def prepare(_, live, work):
        events.append(("prepare", live, source_globals["LIVE_FIXTURE_DURATION_SECONDS"]))
        return work / "prepared-source.ts"

    monkeypatch.setitem(globals_, "prepare_loop_source", prepare)
    monkeypatch.setitem(
        globals_,
        "verify_source_loop",
        lambda _, source, work, duration: events.append(("seam", source, duration)),
    )
    monkeypatch.setitem(
        globals_,
        "run_case",
        lambda case, _, live, prepared, work, deadline: events.append((case, live, prepared)),
    )
    assert helper["main"]() == 0
    assert [event[0] for event in events] == [
        "old",
        "prepare",
        "seam",
        "prepare",
        "seam",
        "single",
        "fixed",
    ]
    assert events[1][2] == 4 and events[3][2] == 8
    assert events[2][1].name == events[4][1].name == "prepared-source.ts"
    assert events[-2][1:] == events[-1][1:]
    assert events[-1][1] == events[3][1] and events[-1][2] == events[4][1]
    assert helper["WORK_SECONDS"] == 132 and helper["JITTER_SECONDS"] == 0.022


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
