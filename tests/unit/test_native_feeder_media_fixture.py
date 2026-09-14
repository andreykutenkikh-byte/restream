from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-feeder-media.py"


def test_clock_probe_stages_live_tmpfs_through_exec_not_docker_archive():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        item
        for item in workflow["jobs"]["test"]["steps"]
        if item.get("name") == "Native fixture media clock under scheduler jitter"
    )
    script = step["run"]
    assert "exec -T ci-ssh-target python3 -c" in script
    assert "target.write_bytes(data)" in script
    assert "target.read_bytes() == data" in script
    assert "< deploy/moblin-relay/self-test" in script
    assert "cp deploy/moblin-relay/self-test" not in script


@pytest.mark.parametrize("missing", ["staged self-test", "FFmpeg", "FFprobe"])
def test_clock_probe_reports_missing_prerequisite_without_media_work(monkeypatch, missing):
    namespace = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    names = {
        namespace["SELF_TEST"]: "staged self-test",
        Path("/usr/bin/ffmpeg"): "FFmpeg",
        Path(namespace["FFPROBE"]): "FFprobe",
    }
    monkeypatch.setenv("CI_NATIVE_FEEDER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda path: names[path] != missing)
    with pytest.raises(
        namespace["ProbeFailure"], match=f"^isolated fixture prerequisite missing: {missing}$"
    ):
        namespace["main"]()


def test_finite_clock_source_keeps_media_copy_and_places_sps_before_buffering_sei(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    source = runpy.run_path(str(ROOT / "deploy/moblin-relay/self-test"), run_name="_feeder")
    live, transport = Path("live.mp4"), Path("source.ts")
    original = source["local_mpegts_remux_command"](live)
    command = helper["finite_transport_command"](source, live, transport)
    expected = list(original)
    index = expected.index("-stream_loop")
    del expected[index : index + 2]
    expected[-1:] = ["-bsf:v", "h264_mp4toannexb,dump_extra=freq=keyframe", str(transport)]
    assert command == expected
    assert command[command.index("-c") + 1] == "copy"
    assert original[-1] == "pipe:1"
    assert "-bsf:v" not in original


def test_four_second_source_diagnostic_reports_bytes_pts_and_nominal_send_time(
    monkeypatch, tmp_path, capsys
):
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    make = helper["make_transport"]
    payload = (b"\x47" + b"\xff" * 187) * 112

    def generate(directory):
        return directory / "live.mp4"

    def remux(command, **_kwargs):
        Path(command[-1]).write_bytes(payload)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setitem(generate.__globals__, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(generate.__globals__, "LIVE_FIXTURE_DURATION_SECONDS", 8)
    namespace = {
        "generate_live": generate,
        "local_mpegts_remux_command": lambda _path: ["ffmpeg", "-stream_loop", "-1", "pipe:1"],
        "LIVE_FEED_CHUNK_BYTES": 10528,
        "LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND": 9_000_000,
    }
    monkeypatch.setitem(make.__globals__, "load_feeder", lambda _case: namespace)
    monkeypatch.setattr(helper["subprocess"], "run", remux)
    monkeypatch.setitem(
        make.__globals__, "probe_packets", lambda *_args: [1.4 + index / 30 for index in range(120)]
    )
    _path, actual = make(tmp_path, 4)
    assert actual == payload
    assert capsys.readouterr().out.strip() == (
        "Synthetic source clock: duration_seconds=4 source_bytes=21056 padding_bytes=0 "
        "video_packets=120 pts_span_seconds=3.966667 nominal_send_seconds=0.009358"
    )


def test_transport_clock_normalization_fixes_measured_finite_source_bias_not_real_drift():
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    rate = helper["transport_clock_rate"]
    source_bytes, chunk_bytes, byte_rate = 4_800_768, 10_528, 1_125_000
    actual_seconds, pts_span = 4.241209, 3.966667
    # Exact Linux CI measurement: video PTS omits finite mux padding, despite
    # correct byte pacing. Keep reporting that ratio, but do not call it drift.
    assert pts_span / actual_seconds == pytest.approx(0.935268, abs=1e-6)
    assert pts_span / actual_seconds < 0.95
    assert rate(source_bytes, chunk_bytes, byte_rate, actual_seconds) == pytest.approx(
        1.003957, abs=1e-6
    )
    assert 0.95 <= rate(source_bytes, chunk_bytes, byte_rate, actual_seconds) <= 1.05
    assert rate(source_bytes, chunk_bytes, byte_rate, 8.920063) < 0.60
    nominal_seconds = (source_bytes - chunk_bytes) / byte_rate
    assert rate(source_bytes, chunk_bytes, byte_rate, nominal_seconds / 0.94) < 0.95
    assert rate(source_bytes, chunk_bytes, byte_rate, nominal_seconds / 1.06) > 1.05


@pytest.mark.parametrize("case", ["old", "single", "fixed"])
@pytest.mark.parametrize("jitter", [0.003, 0.010])
def test_real_media_helper_replays_exact_removed_clock_policies(monkeypatch, case, jitter):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    load = helper["load_feeder"]
    monkeypatch.setitem(load.__globals__, "SELF_TEST", ROOT / "deploy/moblin-relay/self-test")
    clocks = runpy.run_path(str(Path(__file__).with_name("test_native_live_feeder_clock.py")))
    observed, interval, _clock = clocks["run_delayed_feeder"](
        wakeup_delay=jitter, sends=1001, feeder_namespace=load(case)
    )
    rate = 1000 * interval / observed[-1][0]
    if case == "fixed" or (case == "single" and jitter == 0.003):
        assert 0.995 <= rate <= 1
    else:
        assert 0 < rate < 0.9


def test_phase_debt_without_explicit_send_accounting_reproduces_four_packet_burst(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    load = helper["load_feeder"]
    monkeypatch.setitem(load.__globals__, "SELF_TEST", ROOT / "deploy/moblin-relay/self-test")
    # Keep the new phase window but remove its explicit guard: this is the
    # rejected phase-debt-only policy, not another accepted media-gate case.
    monkeypatch.setitem(load.__globals__, "PREVIOUS_CONDITION", helper["FIXED_CONDITION"])
    clocks = runpy.run_path(str(Path(__file__).with_name("test_native_live_feeder_clock.py")))
    _observed, _interval, clock = clocks["run_delayed_feeder"](
        wakeup_delay=0,
        sends=20,
        first_delay_intervals=2.9,
        processing_intervals=0.1,
        feeder_namespace=load("old"),
        check_burst_bound=False,
    )
    assert clock.bursts[1] == 4


@pytest.mark.parametrize(
    "rates,error",
    [
        ([0.738, 0.991, 0.481, 0.984], None),
        ([0.95, 0.991, 0.481, 0.984], "old fixture media clock slowdown was not reproduced"),
        ([0.738, 0.94, 0.481, 0.984], "fixed fixture media clock does not follow wall time"),
        ([0.738, 0.991, 0.8, 0.984], "single-interval fixture scheduling cliff was not reproduced"),
        ([0.738, 0.991, 0.481, 0.94], "bounded catch-up fixture clock does not follow wall time"),
        ([0.738, 0.991, 0.481, 1.06], "bounded catch-up fixture clock does not follow wall time"),
    ],
)
def test_media_gate_preserves_original_cases_and_adds_bounded_cliff_cases(
    monkeypatch, capsys, rates, error
):
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    main = helper["main"]
    captures, generated = [], []
    monkeypatch.setenv("CI_NATIVE_FEEDER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)

    def make(directory, duration=8):
        generated.append(duration)
        return directory / "source.ts", b"complete fixed media"

    def measure(case, directory, source, payload, *, jitter=0.003, duration=8, rate_basis="video"):
        assert source == directory / "source.ts"
        assert payload == b"complete fixed media"
        captures.append((case, jitter, duration, rate_basis))
        return rates[len(captures) - 1]

    monkeypatch.setitem(main.__globals__, "make_transport", make)
    monkeypatch.setitem(main.__globals__, "media_clock_rate", measure)
    if error is None:
        assert main() == 0
    else:
        with pytest.raises(helper["ProbeFailure"], match=f"^{error}$"):
            main()
    assert generated == [8, 4]
    assert captures == [
        ("old", 0.003, 8, "video"),
        ("fixed", 0.003, 8, "video"),
        ("single", 0.010, 4, "transport"),
        ("fixed", 0.010, 4, "transport"),
    ]
    assert helper["CAPTURE_TIMEOUT_SECONDS"] == 20.0
    assert helper["MAX_CAPTURE_BYTES"] == 12 * 1024 * 1024
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == (
        "Real media clock rates before assertions: "
        f"old_3ms={rates[0]:.6f} bounded_3ms={rates[1]:.6f} "
        f"single_10ms_transport={rates[2]:.6f} bounded_10ms_transport={rates[3]:.6f}"
    )
    assert "complete fixed media" not in first_line
