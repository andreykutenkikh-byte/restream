"""Private pause observations must preserve original calls and reject unsafe payloads."""

from __future__ import annotations

import copy
import json
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deploy/moblin-relay/test-native-startup.py"


@pytest.fixture
def runner():
    return runpy.run_path(str(SOURCE), run_name="_pause_timeline_unit")


def test_event_ring_retains_bounded_numeric_evidence_and_freezes_before_later_faults(runner):
    clock = [10**9]
    timeline = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    timeline.start_pause()
    for _ in range(520):
        clock[0] += 1000
        timeline.record("wait-return", True)
    timeline.freeze()
    first = timeline.report()
    assert len(first["events"]) == 512 and first["dropped_events"] == 9
    assert first["clock"] == "monotonic_ns_relative_to_pause_request"
    assert first["events"][-1][1:] == ["wait-return", True]

    clock[0] += 600 * 10**9
    timeline.record("wait-return", False)
    timeline.start_pause()
    timeline.freeze()
    assert timeline.report() == first
    # Report callers cannot mutate the cached evidence returned on later failures.
    first["events"].clear()
    assert timeline.report()["events"]


@pytest.mark.parametrize(
    "code,values",
    [
        ("PRIVATE_CODE", ()),
        ("pause-return", (True, "PRIVATE_TOKEN", 10, 100)),
        ("pause-return", (True, {}, 10, 100)),
        ("pause-return", (True, float("nan"), 10, 100)),
        ("pause-return", (True, float("inf"), 10, 100)),
        ("pause-return", (True, 2**128, 10, 100)),
    ],
)
def test_invalid_event_values_cannot_export_raw_identifiers_or_nonfinite_numbers(
    runner, code, values
):
    timeline = runner["PauseTimeline"]({}, clock=lambda: 10**9)
    timeline.start_pause()
    timeline.record(code, *values)
    timeline.freeze()
    exported = json.dumps(timeline.report(), allow_nan=False)
    assert "PRIVATE" not in exported
    assert str(2**128) not in exported


@pytest.fixture
def delegated(runner, monkeypatch, tmp_path):
    calls = []
    outcome = SimpleNamespace(pause=True, resume=True, wait=None, fetch="original metrics")

    def result_or_raise(value):
        if isinstance(value, Exception):
            raise value
        return value

    class Feeder:
        def __init__(self):
            self._condition = threading.Condition()
            self._clock_last = 0.9
            self._clock_packets = 2
            self._clock_bytes = 2048

        def pause(self, *args, **kwargs):
            calls.append(("pause", args, kwargs))
            return result_or_raise(outcome.pause)

        def resume(self, *args, **kwargs):
            calls.append(("resume", args, kwargs))
            return result_or_raise(outcome.resume)

    class Observer:
        def __init__(self):
            self.lock = threading.Lock()
            self.samples = []

        def wait_sample(self, description, predicate, timeout, **kwargs):
            calls.append(("wait", description, timeout, kwargs))
            if isinstance(outcome.wait, Exception):
                raise outcome.wait
            current = outcome.wait
            return current if predicate(current) else None

    def fetch_metrics(*args, **kwargs):
        calls.append(("fetch", args, kwargs))
        return result_or_raise(outcome.fetch)

    api = {
        "PacedMPEGTSFeeder": Feeder,
        "Observer": Observer,
        "fetch_metrics": fetch_metrics,
        "DUT_METRICS_PORT": 10001,
        "SINK_METRICS_PORT": 10002,
        "SRT_IDLE_LOWER_BOUND_SECONDS": 8,
    }
    state = {"last_stage": "stall-pause"}
    timeline = runner["install_pause_timeline"](api, state, tmp_path, {})
    observer = api["Observer"]()
    monkeypatch.setitem(
        runner["install_pause_timeline"].__globals__,
        "threading",
        SimpleNamespace(current_thread=lambda: observer, Lock=threading.Lock),
    )
    return SimpleNamespace(
        api=api,
        state=state,
        timeline=timeline,
        calls=calls,
        outcome=outcome,
        observer=observer,
        feeder=api["PacedMPEGTSFeeder"](),
    )


def test_feeder_returns_and_timeout_arguments_are_delegated_once(delegated):
    w = delegated
    assert w.state["pause_timeline"] is w.timeline
    w.outcome.pause = False
    assert w.feeder.pause(timeout=0.37) is False
    w.outcome.resume = False
    assert w.feeder.resume(timeout=0.41) is False
    assert w.calls == [("pause", (), {"timeout": 0.37}), ("resume", (), {"timeout": 0.41})]
    assert w.timeline.report()["summary"]["pause_ack_observed_ns"] is None


@pytest.mark.parametrize("operation", ["pause", "resume", "wait", "fetch"])
def test_original_exception_object_survives_each_diagnostic_delegate(delegated, operation):
    w = delegated
    if operation != "pause":
        w.feeder.pause()
    error = OSError("PRIVATE_EXCEPTION")
    setattr(w.outcome, operation, error)
    operations = {
        "pause": lambda: w.feeder.pause(timeout=0.37),
        "resume": lambda: w.feeder.resume(timeout=0.41),
        "wait": lambda: w.observer.wait_sample(
            "same-session SLATE transition",
            lambda _sample: True,
            8.0,
        ),
        "fetch": lambda: w.api["fetch_metrics"](10001),
    }
    with pytest.raises(OSError) as raised:
        operations[operation]()
    assert raised.value is error
    assert sum(call[0] == operation for call in w.calls) == 1
    w.timeline.freeze()
    assert "PRIVATE" not in json.dumps(w.timeline.report())


def test_predicate_receipt_preserves_late_completion_and_uses_finished_for_age(delegated):
    w = delegated
    now = [100 * 10**9]
    w.timeline.clock = lambda: now[0]
    w.feeder._clock_last = 99.9
    w.feeder.pause()
    current = {"t": 107.9, "finished": 108.001}
    w.outcome.wait = current
    now[0] = 108_029_000_000
    predicates = []

    def rejects_late(sample):
        predicates.append(sample)
        return False

    assert (
        w.observer.wait_sample(
            "same-session SLATE transition",
            rejects_late,
            8,
            not_before=100.0,
        )
        is None
    )
    assert predicates == [current]
    report = w.timeline.report()
    row = next(row for row in report["events"] if row[1] == "predicate")
    fields = dict(zip(report["event_fields"]["predicate"], row[2:], strict=True))
    assert fields == {
        "evaluated_ns": 8_029_000_000,
        "sample_started_ns": 7_900_000_000,
        "sample_finished_ns": 8_001_000_000,
        "sample_age_ns": 28_000_000,
        "accepted": False,
    }
    assert report["summary"]["pause_deadline_ns"] == 8_000_000_000


def test_waiter_preserves_predicate_result_object_kwargs_and_call_count(delegated):
    w = delegated
    w.feeder.pause()
    sample = {"t": 1.0, "finished": 9.001, "PRIVATE_ID": "PRIVATE_VALUE"}
    w.outcome.wait = sample
    predicates, health = [], object()

    def predicate(current):
        predicates.append(current)
        return True

    returned = w.observer.wait_sample(
        "same-session SLATE transition",
        predicate,
        8.0,
        not_before=1.0,
        health_check=health,
    )
    assert returned is sample and predicates == [sample]
    assert w.calls[-1] == (
        "wait",
        "same-session SLATE transition",
        8.0,
        {"not_before": 1.0, "health_check": health},
    )
    assert "PRIVATE" not in json.dumps(w.timeline.report())


def test_metrics_delegate_has_no_extra_request_and_does_not_change_payload(delegated):
    w = delegated
    w.feeder.pause()
    payload = "PRIVATE_METRICS_PAYLOAD"
    w.outcome.fetch = payload
    assert w.api["fetch_metrics"](10001) is payload
    assert [call for call in w.calls if call[0] == "fetch"] == [("fetch", (10001,), {})]
    w.timeline.freeze()
    assert "PRIVATE" not in json.dumps(w.timeline.report())


def test_only_original_observer_thread_requests_are_recorded(delegated, runner, monkeypatch):
    w = delegated
    w.feeder.pause()
    for port in (10001, 10002, 10001):
        w.api["fetch_metrics"](port)
    monkeypatch.setitem(
        w.api["fetch_metrics"].__globals__,
        "threading",
        SimpleNamespace(current_thread=lambda: object()),
    )
    assert w.api["fetch_metrics"](10001) == "original metrics"
    assert sum(call[0] == "fetch" for call in w.calls) == 4
    report = w.timeline.report()
    rows = [row for row in report["events"] if row[1] == "metrics"]
    assert [row[2:4] for row in rows] == [[0, 1], [1, 1], [0, 2]]
    assert [summary["requests"] for summary in report["summary"]["observer_metrics"]] == [2, 1]


def test_predicate_exception_is_not_retried_or_replaced_by_recording(delegated):
    w = delegated
    w.feeder.pause()
    sample = {"t": 1.0, "finished": 1.2}
    w.outcome.wait = sample
    error = ValueError("PRIVATE_PREDICATE_FAILURE")
    evaluated = []

    def predicate(current):
        evaluated.append(current)
        raise error

    with pytest.raises(ValueError) as raised:
        w.observer.wait_sample("same-session SLATE transition", predicate, 8.0)
    assert raised.value is error
    assert evaluated == [sample]
    assert sum(call[0] == "wait" for call in w.calls) == 1
    assert "PRIVATE" not in json.dumps(w.timeline.report())


def test_collector_snapshot_is_unchanged_by_mutating_original_sample_after_freeze(runner):
    clock = [10 * 10**9]
    timeline = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    timeline.start_pause()
    current = {
        "t": 10.1,
        "finished": 10.3,
        "normalized_ids": ["PRIVATE_ID"],
        "ingest_ids": ["PRIVATE_INGEST"],
        "normalized_bytes": 1234,
        "dut_metrics_ok": True,
        "sink_metrics_ok": True,
    }
    timeline.observer = SimpleNamespace(lock=threading.Lock(), samples=[current])
    clock[0] = 11 * 10**9
    timeline.freeze()
    before = copy.deepcopy(timeline.report())
    assert before["samples"][0]["started_ns"] == 100_000_000
    assert before["samples"][0]["finished_ns"] == 300_000_000
    assert before["samples"][0]["collection_age_ns"] == 700_000_000
    assert before["samples"][0]["normalized_generations"] == [1]
    current.update(t=12345.0, finished=67890.0)
    assert timeline.report() == before
    assert "PRIVATE" not in json.dumps(before)


def test_sample_ring_retains_ends_ordinal_changes_and_timely_late_distinction(runner):
    clock = [100 * 10**9]
    timeline = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    timeline.start_pause()
    timeline.deadline_ns = 108 * 10**9
    samples = [
        {
            "t": round(100 + index * 0.01, 3),
            "finished": round(100 + index * 0.01 + 0.005, 3),
            "normalized_ids": ["PRIVATE_FIRST" if index < 100 else "PRIVATE_SECOND"],
            "normalized_bytes": index,
        }
        for index in range(130)
    ]
    samples.extend(
        [
            {"t": 107.98, "finished": 107.99, "normalized_ids": ["PRIVATE_SECOND"]},
            {"t": 107.995, "finished": 108.001, "normalized_ids": ["PRIVATE_SECOND"]},
        ]
    )
    timeline.observer = SimpleNamespace(lock=threading.Lock(), samples=samples)
    clock[0] = 108_029_000_000
    timeline.freeze()
    report = timeline.report()
    assert len(report["samples"]) == 128 and report["sample_rows_omitted"] == 4
    assert report["samples"][0]["finished_ns"] == 5_000_000
    assert report["samples"][-1]["finished_ns"] == 8_001_000_000
    assert report["samples"][-1]["collection_age_ns"] == 28_000_000
    assert report["samples"][0]["normalized_generations"] == [1]
    assert report["samples"][-1]["normalized_generations"] == [2]
    latest = report["summary"]["latest_completed_sample_before_deadline"]
    assert latest["finished_ns"] == 7_990_000_000
    assert "PRIVATE" not in json.dumps(report)
    summary = timeline.report(include_rows=False)
    assert not {"events", "samples", "runtime_events"} & summary.keys()
    assert summary["summary"] == report["summary"]


def test_real_runtime_receipt_shares_pause_clock_and_rejects_raw_replacement(runner, monkeypatch):
    runtime = runpy.run_path(
        str(SOURCE.with_name("test-native-startup-normalizer.py")), run_name="_pause_runtime_unit"
    )
    clock, writes = [99 * 10**9], []
    scope = runtime["SupervisorTimeline"].save.__globals__
    monkeypatch.setitem(scope, "atomic_timeline_record", lambda _stage, value: writes.append(value))
    monkeypatch.setitem(scope, "os", SimpleNamespace(getpid=lambda: 123456789))
    trace = runtime["SupervisorTimeline"](Path("unused-private-stage"), clock=lambda: clock[0])
    clock[0] = 100 * 10**9
    pause = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    pause.runtime_api = runtime
    pause.start_pause()
    reason = runtime["TIMELINE_REASONS"].index("video-stalled")
    for stamp, code, values in (
        (100_200_000_000, 3, (1, 90, 0)),
        (102_700_000_000, 6, (reason,)),
        (102_710_000_000, 8, (1, 0)),
        (102_720_000_000, 9, (1, 1, -9)),
    ):
        clock[0] = stamp
        trace.add(code, *values)
    trace.save()
    assert runtime["validated_timeline_report"](writes[0]) is not None
    clock[0] = 103 * 10**9
    pause.freeze(writes[0])
    report = pause.report()
    expected = [
        [200_000_000, 3, 1, 90, 0],
        [2_700_000_000, 6, reason],
        [2_710_000_000, 8, 1, 0],
        [2_720_000_000, 9, 1, 1, -9],
    ]
    assert report["runtime_events"] == expected
    assert report["runtime_quality"]["available"] is True
    for key, row in zip(
        (
            "last_runtime_video_progress",
            "first_runtime_watchdog_reject",
            "first_runtime_kill_request",
            "first_runtime_child_wait_completed",
        ),
        expected,
        strict=True,
    ):
        assert report["summary"][key] == row
    assert "supervisor_pid" not in json.dumps(report)
    assert "123456789" not in json.dumps(report)

    invalid = copy.deepcopy(writes[0])
    invalid["events"][-1].append("PRIVATE_RUNTIME_TOKEN")
    pause.freeze(invalid)
    assert pause.report() == report
    rejected = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    rejected.runtime_api = runtime
    rejected.start_pause()
    rejected.freeze(invalid)
    assert rejected.report()["runtime_quality"] == {"available": False}
    assert rejected.report()["runtime_events"] == []
    assert "PRIVATE" not in json.dumps(rejected.report())


def test_resumed_growth_does_not_replace_interval_observed_during_pause(runner):
    clock = [100 * 10**9]
    timeline = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    timeline.start_pause()
    samples = [
        {
            "t": finished - 0.05,
            "finished": finished,
            "ingest_ids": ["PRIVATE_INGEST"],
            "normalized_ids": ["PRIVATE_NORMALIZER"],
            "sink_ids": ["PRIVATE_SINK"],
            "ingest_bytes": counter,
            "normalized_bytes": counter,
            "normalized_media_bytes": counter,
            "sink_bytes": counter,
        }
        for finished, counter in ((100.05, 100), (101.0, 200), (102.1, 300), (102.5, 400))
    ]
    timeline.observer = SimpleNamespace(lock=threading.Lock(), samples=samples)
    clock[0] = 102 * 10**9
    timeline.record("resume-request")
    clock[0] = 103 * 10**9
    timeline.freeze()
    report = timeline.report()
    growth = report["summary"]["last_observed_growth_intervals_ns"]
    assert all(value == [50_000_000, 1_000_000_000] for value in growth.values())
    assert report["samples"][-1]["normalized_bytes"] == 400
    assert report["samples"][-1]["finished_ns"] == 2_500_000_000


@pytest.mark.parametrize(
    "succeeded,latest_ns", [(True, 4_000_000_000), (False, 7_990_000_000), (None, 7_990_000_000)]
)
def test_successful_wait_summary_excludes_later_samples_but_preserves_raw_rows(
    runner, succeeded, latest_ns
):
    clock = [100 * 10**9]
    timeline = runner["PauseTimeline"]({}, clock=lambda: clock[0])
    timeline.start_pause()
    timeline.deadline_ns = 108 * 10**9
    timeline.observer = SimpleNamespace(
        lock=threading.Lock(),
        samples=[{"t": stamp - 0.01, "finished": stamp} for stamp in (104.0, 105.01, 107.99)],
    )
    clock[0] = 104 * 10**9
    if succeeded is not None:
        timeline.record("wait-return", succeeded)
    clock[0] = 106 * 10**9
    timeline.record("resume-request")
    clock[0] = 109 * 10**9
    report = timeline.report()
    assert report["summary"]["latest_completed_sample_before_deadline"]["finished_ns"] == latest_ns
    assert report["summary"]["pause_deadline_ns"] == 8_000_000_000
    assert len(report["samples"]) == 3
    assert report["samples"][-1]["finished_ns"] == 7_990_000_000


@pytest.mark.parametrize("video_age,derived_ns", [(500, 600), (-1, None)])
def test_runtime_summary_pairs_retained_requests_and_keeps_paused_watchdog_ages(
    runner, video_age, derived_ns
):
    timeline = runner["PauseTimeline"]({}, clock=lambda: 10**9)
    timeline.start_pause()
    runtime = [
        [-100, 1, 0, 1],
        [100, 2, 0, 1, 2, 1, 100, -1],
        [200, 1, 0, 2],
        [300, 2, 0, 2, 0, 1, -1, -1],
        [400, 1, 0, 3],
        [600, 2, 0, 3, 1, 1, -1, -1],
        [700, 2, 0, 4, 3, 1, -1, -1],  # Start was not retained; do not invent it.
        [750, 1, 1, 1],
        [1000, 2, 1, 1, 2, 2, 200, -1],
        [1100, 3, 1, 90, video_age],
        [1200, 1, 1, 2],  # No end receipt; no completed-request duration.
        [1200, 4, 1, 0, 0, 1100, 2],
        [1300, 5, 1, 0, 200, 1200, 3],
        [1400, 4, 0, 1, 7, 1300, 3],
        [1500, 5, 0, 12, 200, 1400, 4],
        [2001, 2, 1, 2, 2, 3, 900, 700],
        [2002, 3, 2, 180, 0],
        [2003, 4, 1, 0, 0, 0, 0],
        [2004, 5, 1, 0, 900, 0, 0],
    ]
    summary = timeline.summarize([[2000, "resume-request"]], [], runtime)
    assert summary["runtime_metrics"] == [
        {
            "metric_kind": 0,
            "completed_requests": 4,
            "matched_requests": 3,
            "availability_counts": [1, 1, 1, 1],
            "max_duration_ns": 200,
            "last_request": {
                "request": 4,
                "started_ns": None,
                "finished_ns": 700,
                "duration_ns": None,
                "availability": 3,
                "generation": 1,
            },
        },
        {
            "metric_kind": 1,
            "completed_requests": 1,
            "matched_requests": 1,
            "availability_counts": [0, 0, 1, 0],
            "max_duration_ns": 250,
            "last_request": {
                "request": 1,
                "started_ns": 750,
                "finished_ns": 1000,
                "duration_ns": 250,
                "availability": 2,
                "generation": 2,
            },
        },
    ]
    assert summary["last_runtime_video_growth_receipt_derived_ns"] == derived_ns
    assert summary["last_runtime_watchdog_output_observation"] == runtime[13]
    assert summary["last_runtime_watchdog_ingest_observation"] == runtime[14]
