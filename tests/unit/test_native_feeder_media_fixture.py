from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

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
    ],
)
def test_media_gate_preserves_original_cases_and_adds_bounded_cliff_cases(
    monkeypatch, rates, error
):
    helper = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    main = helper["main"]
    captures, generated = [], []
    monkeypatch.setenv("CI_NATIVE_FEEDER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)

    def make(directory, duration=8):
        generated.append(duration)
        return directory / "source.ts", b"complete fixed media"

    def measure(case, directory, source, payload, *, jitter=0.003, duration=8):
        assert source == directory / "source.ts"
        assert payload == b"complete fixed media"
        captures.append((case, jitter, duration))
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
        ("old", 0.003, 8),
        ("fixed", 0.003, 8),
        ("single", 0.010, 4),
        ("fixed", 0.010, 4),
    ]
    assert helper["CAPTURE_TIMEOUT_SECONDS"] == 20.0
    assert helper["MAX_CAPTURE_BYTES"] == 12 * 1024 * 1024
