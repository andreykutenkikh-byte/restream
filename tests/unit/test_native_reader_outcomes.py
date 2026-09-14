"""Reader completion is distinct from the mandatory source-clock regression."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import FunctionType, ModuleType, SimpleNamespace

import pytest
from test_native_live_feeder_clock import run_delayed_feeder
from test_native_short_eof import media_validator as _production_media_validator

media_validator = _production_media_validator

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-reader-clock.py"


@pytest.fixture
def helper():
    return runpy.run_path(str(HELPER), run_name="_reader_outcomes_test")


def clock_measure(rate=1.0):
    chunk, byte_rate, media_seconds = 10528, 1125000, 8
    return (
        {
            "bytes": chunk + byte_rate * media_seconds,
            "first": 10.0,
            "last": 10.0 + media_seconds / rate,
            "waits": 3,
            "max_gap": 0.031,
            "max_overrun": 0.022,
        },
        byte_rate,
        chunk,
    )


@pytest.mark.parametrize("rate", [0.25, 0.296512, 0.31, 0.949999, 1.050001, 2.0])
def test_one_common_wall_clock_predicate_rejects_slow_and_fast_rates(helper, rate):
    assert helper["source_clock_passes"](rate) is False


@pytest.mark.parametrize("rate", [0.95, 0.99, 1.0, 1.05])
def test_one_common_wall_clock_predicate_accepts_original_fixed_band(helper, rate):
    assert helper["source_clock_passes"](rate) is True


@pytest.mark.parametrize("rate", [None, True, float("nan"), float("inf"), "1.0"])
def test_common_clock_predicate_never_accepts_missing_or_untrusted_numbers(helper, rate):
    assert helper["source_clock_passes"](rate) is False


@pytest.mark.parametrize("case,rate,result", [("single", 0.296512, "FAIL"), ("fixed", 1, "PASS")])
def test_whole_period_uses_common_predicate_with_explicit_old_counterexample(
    helper, case, rate, result
):
    measure, byte_rate, chunk = clock_measure(rate)
    evidence = helper["validate_clock_evidence"](case, measure, byte_rate, chunk)
    assert evidence["clock_result"] == result
    assert evidence["transport_rate"] == pytest.approx(rate, abs=1e-6)
    assert evidence["media_seconds"] >= 8
    assert evidence["predicate_low"] == 0.95 and evidence["predicate_high"] == 1.05


def test_exact_removed_algorithm_fails_same_gate_as_fixed_under_identical_injection(
    helper, monkeypatch
):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    loader = runpy.run_path(str(ROOT / "deploy/moblin-relay/test-native-feeder-media.py"))
    monkeypatch.setitem(
        loader["load_feeder"].__globals__, "SELF_TEST", ROOT / "deploy/moblin-relay/self-test"
    )
    results = {}
    measures = {}
    for case in ("single", "fixed"):
        namespace = loader["load_feeder"](case)
        observed, interval, clock = run_delayed_feeder(
            wakeup_delay=helper["JITTER_SECONDS"],
            sends=1001,
            feeder_namespace=namespace,
        )
        chunk = namespace["LIVE_FEED_CHUNK_BYTES"]
        byte_rate = namespace["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] / 8
        assert len(observed) == 1001 and clock.waits > 0
        measure = {
            "bytes": sum(len(payload) for _, payload in observed),
            "first": observed[0][0],
            "last": observed[-1][0],
            "waits": clock.waits,
            "max_gap": max(
                right[0] - left[0] for left, right in zip(observed[:-1], observed[1:], strict=True)
            ),
            "max_overrun": helper["JITTER_SECONDS"],
        }
        measures[case] = (measure, byte_rate, chunk)
        results[case] = helper["validate_clock_evidence"](case, measure, byte_rate, chunk)
        assert results[case]["transport_rate"] == pytest.approx(
            1000 * interval / observed[-1][0], abs=1e-6
        )
    assert results["single"]["clock_result"] == "FAIL"
    assert results["fixed"]["clock_result"] == "PASS"
    # Mutation: execute the exact old feeder in the mandatory fixed slot.
    # It is not enough merely to label that old result an expected negative.
    with pytest.raises(helper["CaseFailure"]):
        helper["validate_clock_evidence"]("fixed", *measures["single"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("first", None),
        ("last", float("nan")),
        ("last", float("inf")),
        ("last", 9.0),
        ("last", "rtmp://private.invalid/secret"),
        ("bytes", 10528),
        ("bytes", 10528 + 1125000),
        ("bytes", True),
        ("waits", 0),
        ("waits", True),
    ],
)
def test_missing_untrusted_or_short_clock_evidence_is_fatal(helper, field, value):
    measure, byte_rate, chunk = clock_measure()
    measure[field] = value
    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["validate_clock_evidence"]("fixed", measure, byte_rate, chunk)
    assert caught.value.outcome == "FIXTURE_PRECONDITION_FAILURE"
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("measure", [None, {}, []])
def test_absent_whole_clock_evidence_is_fatal(helper, measure):
    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["validate_clock_evidence"]("fixed", measure, 1125000, 10528)
    assert caught.value.outcome == "FIXTURE_PRECONDITION_FAILURE"


def test_progress_90_or_clean_exit_never_replaces_real_production_validation(
    helper, media_validator, monkeypatch
):
    state = media_validator
    state.source.write_bytes(b"f" * state.namespace["SLATE_CAPTURE_GROWTH_BYTES"])
    calls = []

    def decode(command, **kwargs):
        calls.append(command)
        assert "-xerror" in command and kwargs["timeout"] == 15
        return state.child

    monkeypatch.setitem(state.globals, "run", decode)
    original = state.globals["analyze_decoded_video_frames"]
    result = helper["validate_reader_segment"](
        state.namespace, state.source, state.source.stat().st_size, dict(state.video)
    )
    assert result == {
        "reader_outcome": "VALID_SEGMENT_COMPLETED",
        "decoded_frames": 90,
        "full_validation": True,
    }
    assert calls and not state.source.exists()
    assert state.globals["analyze_decoded_video_frames"] is original


@pytest.mark.parametrize("frames", [61, 89])
def test_old_and_fixed_short_eof_remain_fatal_with_real_decoded_count(
    helper, media_validator, monkeypatch, frames
):
    state = media_validator
    state.source.write_bytes(b"f" * state.namespace["SLATE_CAPTURE_GROWTH_BYTES"])
    state.decoded.update(frame_count=frames, presentation_timestamp_count=frames)
    monkeypatch.setitem(state.globals, "run", lambda *_args, **_kwargs: state.child)
    original = state.globals["analyze_decoded_video_frames"]
    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["validate_reader_segment"](
            state.namespace, state.source, state.source.stat().st_size, dict(state.video)
        )
    assert caught.value.outcome == "SHORT_EOF"
    assert not state.source.exists()
    assert state.globals["analyze_decoded_video_frames"] is original


@pytest.mark.parametrize(
    "change,outcome",
    [
        ("format", "DECODE_OR_FORMAT_FAILURE"),
        ("decode", "DECODE_OR_FORMAT_FAILURE"),
        ("decoded", "DECODE_OR_FORMAT_FAILURE"),
        ("gop", "DECODE_OR_FORMAT_FAILURE"),
        ("pts", "TIMESTAMP_OR_AV_SYNC_FAILURE"),
        ("dts", "TIMESTAMP_OR_AV_SYNC_FAILURE"),
        ("av_sync", "TIMESTAMP_OR_AV_SYNC_FAILURE"),
    ],
)
def test_full_production_validator_failures_are_not_expected_old_outcomes(
    helper, media_validator, monkeypatch, change, outcome
):
    state = media_validator
    expected = dict(state.video)
    state.source.write_bytes(b"f" * state.namespace["SLATE_CAPTURE_GROWTH_BYTES"])
    if change == "format":
        state.video["width"] = 1280
    elif change == "decode":
        state.child.returncode = 7
    elif change == "decoded":
        state.decoded["decode_error_flags"] = True
    elif change == "gop":
        state.gop["interval_frames"] = [59]
    elif change == "pts":
        state.decoded["strict_presentation_timestamps_monotonic"] = False
    elif change == "dts":
        state.timestamps["negative_dts_steps"] = {0: 1}
    else:
        state.timestamps["audio_video_end_difference_seconds"] = 1.0
    monkeypatch.setitem(state.globals, "run", lambda *_args, **_kwargs: state.child)
    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["validate_reader_segment"](
            state.namespace, state.source, state.source.stat().st_size, expected
        )
    assert caught.value.outcome == outcome
    assert not state.source.exists()


def timeout_error(failure_class, seconds=15):
    error = failure_class("strict RTMP sink media read timed out")
    error.__cause__ = subprocess.TimeoutExpired(["synthetic"], seconds)
    return error


def timeout_progress():
    return {
        "reader_input": True,
        "reader_output": True,
        "reader_frames": 71,
        "reader_first_frame_seconds": 9.0,
        "reader_last_frame_seconds": 14.0,
    }


def test_expected_old_timeout_requires_precise_provenance_and_fixed_timeout_still_fails(helper):
    class NativeFailure(Exception):
        pass

    classify = helper["classify_capture_failure"]
    error = timeout_error(NativeFailure)
    assert classify("single", error, NativeFailure, timeout_progress()) == {
        "reader_outcome": "EXPECTED_READER_TIMEOUT",
        "decoded_frames": None,
        "full_validation": False,
    }
    with pytest.raises(helper["CaseFailure"]):
        classify("fixed", error, NativeFailure, timeout_progress())
    for invalid in (
        timeout_error(NativeFailure, 16),
        NativeFailure("sink unavailable"),
        OSError("private"),
    ):
        with pytest.raises(helper["CaseFailure"]) as caught:
            classify("single", invalid, NativeFailure, timeout_progress())
        assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reader_input", False),
        ("reader_output", False),
        ("reader_frames", 0),
        ("reader_frames", 90),
    ],
)
def test_timeout_with_wrong_reader_preconditions_is_fatal(helper, field, value):
    class NativeFailure(Exception):
        pass

    with pytest.raises(helper["CaseFailure"]):
        helper["classify_capture_failure"](
            "single",
            timeout_error(NativeFailure),
            NativeFailure,
            timeout_progress() | {field: value},
        )


def test_old_validator_success_without_decode_evidence_cannot_admit_segment(
    helper, media_validator, monkeypatch
):
    state = media_validator
    state.source.write_bytes(b"f" * state.namespace["SLATE_CAPTURE_GROWTH_BYTES"])
    original = state.globals["analyze_decoded_video_frames"]

    def unvalidated(_capture, _size, _video, _guard, **_kwargs):
        return {"video_frames": 90}

    # Retain all production dependencies, not just a fake function lacking
    # its globals, so this proves the after-call observation guard itself.
    mutant = FunctionType(unvalidated.__code__, state.globals)
    monkeypatch.setitem(state.namespace, "validate_final_sink_media_segment", mutant)
    with pytest.raises(helper["CaseFailure"], match="^full decoded validation evidence missing$"):
        helper["validate_reader_segment"](
            state.namespace, state.source, state.source.stat().st_size, dict(state.video)
        )
    assert state.globals["analyze_decoded_video_frames"] is original


def test_other_wrapped_timeout_is_not_mistaken_for_expected_reader_timeout(helper):
    class NativeFailure(Exception):
        pass

    error = timeout_error(NativeFailure)
    error.args = ("unrelated fixture command timed out",)
    assert not helper["expected_old_timeout"](error, NativeFailure, timeout_progress())


def test_delayed_progress_pipe_drain_does_not_redefine_reader_process_deadline(helper):
    class NativeFailure(Exception):
        pass

    error = timeout_error(NativeFailure)
    progress = timeout_progress() | {"reader_last_frame_seconds": 15.001}
    assert helper["expected_old_timeout"](error, NativeFailure, progress)
    error.__cause__ = subprocess.TimeoutExpired(["synthetic"], 15.001)
    assert not helper["expected_old_timeout"](error, NativeFailure, progress)


def test_safe_error_evidence_does_not_serialize_transport_or_raw_exception(helper):
    class NativeFailure(Exception):
        pass

    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["classify_capture_failure"](
            "single",
            NativeFailure("rtmp://private.invalid/secret"),
            NativeFailure,
            timeout_progress(),
        )
    assert "private" not in str(caught.value)
    assert "private" not in json.dumps(caught.value.evidence)


@pytest.mark.parametrize("failed_case", ["single", "fixed", "both"])
def test_pair_runs_fixed_independently_but_keeps_each_real_failure(
    helper, monkeypatch, tmp_path, failed_case
):
    calls, directories = [], []

    def run_case(case, namespace, live, prepared, directory, deadline):
        assert namespace == {"case": case} and deadline == 1000
        assert live == tmp_path / "live.mp4" and prepared == tmp_path / "prepared.ts"
        calls.append(case)
        directories.append(directory)
        if failed_case in (case, "both"):
            raise helper["CaseFailure"]("DECODE_OR_FORMAT_FAILURE", "strict validation failed")
        return {"reader_outcome": "VALID_SEGMENT_COMPLETED"}

    monkeypatch.setitem(helper["run_pair"].__globals__, "run_case", run_case)
    monkeypatch.setattr(helper["time"], "monotonic", lambda: 0)
    with pytest.raises(helper["ProbeFailure"]):
        helper["run_pair"](
            lambda case: {"case": case},
            tmp_path / "live.mp4",
            tmp_path / "prepared.ts",
            tmp_path,
            1000,
        )
    assert calls == ["single", "fixed"]
    assert [directory.name for directory in directories] == calls


def test_pair_does_not_begin_independent_fixed_after_outer_budget_is_spent(
    helper, monkeypatch, tmp_path
):
    clock = SimpleNamespace(now=0.0)
    calls = []

    def run_case(case, *_args):
        calls.append(case)
        clock.now = 90
        raise helper["CaseFailure"]("FIXTURE_PRECONDITION_FAILURE", "strict fixture failed")

    monkeypatch.setitem(helper["run_pair"].__globals__, "run_case", run_case)
    monkeypatch.setattr(helper["time"], "monotonic", lambda: clock.now)
    with pytest.raises(helper["ProbeFailure"]):
        helper["run_pair"](lambda _: {}, tmp_path / "live", tmp_path / "prepared", tmp_path, 100)
    assert calls == ["single"]


@pytest.fixture
def case_lifecycle(helper, media_validator, monkeypatch, tmp_path):
    """No media process: exercise the actual owned-resource try/finally."""
    state = SimpleNamespace(
        processes=[],
        feeder_started=False,
        feeder_finished=False,
        cleanup_success=True,
        sink_available=True,
        phase_valid=True,
        now=0.0,
    )
    namespace = dict(media_validator.namespace)
    globals_ = helper["run_case"].__globals__
    ports = iter([31000, 31001, 31002])
    monkeypatch.setitem(globals_, "free_port", lambda _: next(ports))
    monkeypatch.setitem(globals_, "configure", lambda *_args: {})
    monkeypatch.setattr(helper["time"], "monotonic", lambda: state.now)
    monkeypatch.setattr(
        helper["time"], "sleep", lambda delay: setattr(state, "now", state.now + delay)
    )

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.terminated = self.waited = False
            self.stderr = SimpleNamespace(close=lambda: None)
            state.processes.append(self)

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, *, timeout):
            assert timeout == 1
            self.waited = True

    class Feeder:
        def __init__(self, *_args):
            pass

        def start(self):
            state.feeder_started = True

        def wait_ready(self, timeout):
            assert timeout == 5
            return True

        def healthy(self):
            return True

        def finish(self, *, timeout):
            assert timeout == 2
            state.feeder_finished = True
            return state.cleanup_success

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, timeout):
            assert timeout == 0.25

        def connect_ex(self, _address):
            return 111

        def bind(self, _address):
            pass

    class Observer:
        def __init__(self, pipe):
            self.pipe = pipe
            self.joined = False

        def start(self):
            pass

        def snapshot(self):
            if not state.phase_valid:
                raise helper["ProbeFailure"]("observer failed")
            return []

        def join(self, *, timeout):
            assert timeout == 1
            self.joined = True

        def is_alive(self):
            return False

    def wait_tcp(_port, *, timeout):
        assert timeout == 5
        if not state.sink_available:
            raise OSError("rtmp://private.invalid/sink unavailable")

    monkeypatch.setitem(globals_, "PhaseObserver", Observer)
    monkeypatch.setattr(helper["subprocess"], "Popen", Process)
    monkeypatch.setattr(helper["socket"], "socket", Socket)
    namespace.update(
        PacedMPEGTSFeeder=Feeder,
        wait_tcp=wait_tcp,
        fetch_metrics=lambda _: "fixed metrics",
        parse_metrics=lambda *_: [({"name": "live/sink", "state": "ready"}, 1)],
    )

    def invoke():
        return helper["run_case"](
            "fixed", namespace, tmp_path / "live.mp4", tmp_path / "prepared.ts", tmp_path, 100
        )

    state.invoke = invoke
    return state


def test_unavailable_sink_is_fatal_and_owned_process_is_still_reaped(helper, case_lifecycle):
    state = case_lifecycle
    state.sink_available = False
    with pytest.raises(helper["CaseFailure"]) as caught:
        state.invoke()
    assert caught.value.outcome == "FIXTURE_PRECONDITION_FAILURE"
    assert "private" not in str(caught.value)
    assert len(state.processes) == 1
    assert all(process.terminated and process.waited for process in state.processes)


def test_invalid_phase_is_fatal_and_feeder_cleanup_still_runs(helper, case_lifecycle):
    state = case_lifecycle
    state.phase_valid = False
    with pytest.raises(helper["CaseFailure"]) as caught:
        state.invoke()
    assert caught.value.outcome == "FIXTURE_PRECONDITION_FAILURE"
    assert state.feeder_started and state.feeder_finished
    assert all(process.terminated and process.waited for process in state.processes)


def test_cleanup_failure_cannot_be_masked_by_precondition_or_old_expected_result(
    helper, case_lifecycle
):
    state = case_lifecycle
    state.phase_valid = False
    state.cleanup_success = False
    with pytest.raises(helper["CaseFailure"]) as caught:
        state.invoke()
    assert caught.value.outcome == "CLEANUP_FAILURE"
    assert state.feeder_finished
    assert all(process.terminated and process.waited for process in state.processes)


@pytest.mark.parametrize("failure", ["feeder", "process", "observer", "tcp", "udp"])
def test_cleanup_attempts_all_owned_resources_after_any_one_failure(helper, monkeypatch, failure):
    events = []

    def event(label):
        events.append(label)
        if label == failure:
            raise OSError("private diagnostic")

    class Process:
        def __init__(self, name):
            self.name = name

        def terminate(self):
            event(self.name)

        def wait(self, *, timeout):
            assert timeout == 1
            event("wait-" + self.name)

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            event("socket-close")

        def settimeout(self, timeout):
            assert timeout == 0.25

        def connect_ex(self, _address):
            event("tcp")
            return 111

        def bind(self, _address):
            event("udp")

    def finish(*, timeout):
        assert timeout == 2
        event("feeder")
        return True

    def join(*, timeout):
        assert timeout == 1
        event("observer")

    observer = SimpleNamespace(
        join=join, is_alive=lambda: False, pipe=SimpleNamespace(close=lambda: event("pipe-close"))
    )
    monkeypatch.setattr(helper["socket"], "socket", Socket)
    with pytest.raises(helper["CaseFailure"]) as caught:
        helper["cleanup_case"](
            SimpleNamespace(finish=finish),
            True,
            [Process("other"), Process("process")],
            observer,
            31000,
            31001,
            31002,
        )
    assert caught.value.outcome == "CLEANUP_FAILURE"
    assert "private" not in str(caught.value)
    assert {"feeder", "process", "other", "observer", "pipe-close", "tcp", "udp"} <= set(events)
    assert events.count("tcp") == 2 and events.count("socket-close") == 3


def test_process_cleanup_uses_bounded_kill_after_terminate_timeout(helper, monkeypatch):
    events = []

    class Process:
        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

        def wait(self, *, timeout):
            assert timeout == 1
            events.append("wait")
            if "kill" not in events:
                raise subprocess.TimeoutExpired(["synthetic"], timeout)

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, timeout):
            assert timeout == 0.25

        def connect_ex(self, _address):
            return 111

        def bind(self, _address):
            pass

    monkeypatch.setattr(helper["socket"], "socket", Socket)
    helper["cleanup_case"](None, False, [Process()], None, 31000, 31001, 31002)
    assert events == ["terminate", "wait", "kill", "wait"]
