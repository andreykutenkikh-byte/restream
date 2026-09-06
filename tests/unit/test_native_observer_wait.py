"""Exercise generic observer timeouts without processes, clocks or live media."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

from scripts.ci_node_onboarding_smoke import safe_self_test_progress


def sample(timestamp: float, sink_bytes: int) -> dict:
    return {
        "t": timestamp,
        "live": True,
        "normalized": True,
        "path_ready": True,
        "ingest_live": True,
        "dut_metrics_ok": True,
        "sink_metrics_ok": True,
        "dut_alive": True,
        "sink_alive": True,
        "reader_alive": True,
        "ingest_ids": ["PRIVATE_INGEST_ID"],
        "sink_ids": ["PRIVATE_SINK_ID"],
        "sink_bytes": sink_bytes,
        "forward": True,
        "transport_url": "rtmps://private.invalid/live#PRIVATE_KEY",
    }


def observer_wait(monkeypatch: pytest.MonkeyPatch, samples: list[dict]):
    namespace = load_self_test()
    observer_type = namespace["Observer"]
    state = observer_type.wait_sample.__globals__
    observer = observer_type.__new__(observer_type)
    clock = SimpleNamespace(now=100.0, sleeps=[], reads=0)

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now = round(clock.now + seconds, 6)

    def snapshot(_description):
        current = samples[min(clock.reads, len(samples) - 1)]
        clock.reads += 1
        return current

    observer.checked_snapshot = snapshot
    monkeypatch.setitem(state, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return observer, state, clock


def test_observer_timeout_retains_last_distinct_flags_and_safe_checkpoint(monkeypatch) -> None:
    samples = [sample(100.0, 12345), sample(100.05, 12346)]
    observer, state, clock = observer_wait(monkeypatch, samples)
    predicates = []
    health_checks = []

    def needs_slate(current):
        predicates.append(current)
        return not current["normalized"] and not current["live"] and current["path_ready"]

    with pytest.raises(state["TestFailure"]) as caught:
        observer.wait_sample(
            "PRIVATE_DESCRIPTION", needs_slate, 0.2, health_check=lambda: health_checks.append(1)
        )
    failure, flags, elapsed = state["SELF_TEST_WAIT_FAILURE"]
    assert failure is caught.value
    assert str(failure) == "timed out waiting for PRIVATE_DESCRIPTION"
    assert elapsed == 0.2
    assert clock.sleeps == [0.05] * 4
    assert predicates == [samples[0], samples[1], samples[1], samples[1]]
    assert len(health_checks) == 5
    assert flags == {
        "live": True,
        "normalized": True,
        "path_ready": True,
        "ingest_live": True,
        "metrics_ok": True,
        "core_alive": True,
        "ingest_one": True,
        "sink_one": True,
        "sink_growth": True,
        "state_ok": False,
    }

    checkpoints = []
    monkeypatch.setitem(state, "SELF_TEST_STAGE_FILE", "unused-fixture-stage")
    monkeypatch.setitem(
        state,
        "SELF_TEST_LAST_PROGRESS",
        {"job_id": "test-job", "stage": "outage-normal", "elapsed_seconds": 2.0},
    )
    monkeypatch.setitem(state, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(state, "atomic_json", lambda _path, value: checkpoints.append(value))
    state["persist_self_test_failure_progress"](failure)
    checkpoint = checkpoints[0]
    assert safe_self_test_progress(checkpoint, job_id="test-job") == {
        key: value for key, value in checkpoint.items() if key != "job_id"
    }
    assert checkpoint["failure_flags"] == flags
    assert checkpoint["failure_wait_seconds"] == elapsed
    assert "PRIVATE" not in json.dumps(checkpoint)
    assert "rtmps" not in json.dumps(checkpoint)
    assert "1234" not in json.dumps(checkpoint)


@pytest.mark.parametrize("missing_metrics", [True, False])
def test_observer_timeout_does_not_evaluate_incomplete_or_stale_samples(
    monkeypatch, missing_metrics
) -> None:
    current = sample(100.0 if missing_metrics else 99.0, 12345)
    if missing_metrics:
        current["dut_metrics_ok"] = False
    observer, state, _clock = observer_wait(monkeypatch, [current])

    def unexpected_predicate(_sample):
        pytest.fail("Incomplete or stale metrics must not evaluate the predicate")

    with pytest.raises(state["TestFailure"], match="^timed out waiting for fixture$"):
        observer.wait_sample("fixture", unexpected_predicate, 0.1)
    _failure, flags, elapsed = state["SELF_TEST_WAIT_FAILURE"]
    assert elapsed == 0.1
    assert flags["metrics_ok"] is not missing_metrics
    assert flags["state_ok"] is False
    assert flags["sink_growth"] is False


def test_observer_diagnostic_failure_cannot_replace_timeout(monkeypatch) -> None:
    observer, state, _clock = observer_wait(monkeypatch, [sample(100.0, 12345)])
    failures = []

    class RecordedFailure(state["TestFailure"]):
        def __init__(self, message):
            super().__init__(message)
            failures.append(self)

    def broken_diagnostic(*_args, **_kwargs):
        raise ValueError("PRIVATE_DIAGNOSTIC_FAILURE")

    monkeypatch.setitem(state, "TestFailure", RecordedFailure)
    monkeypatch.setitem(state, "safe_wait_failure_flags", broken_diagnostic)
    with pytest.raises(RecordedFailure) as caught:
        observer.wait_sample("fixture", lambda _sample: False, 0.1)
    assert failures == [caught.value]
    assert str(caught.value) == "timed out waiting for fixture"
    assert state["SELF_TEST_WAIT_FAILURE"] is None


@pytest.mark.parametrize("elapsed", [660.0, 660.001, float("inf")])
def test_observer_timeout_diagnostic_bounds_elapsed(monkeypatch, elapsed) -> None:
    observer, state, clock = observer_wait(monkeypatch, [sample(100.0, 12345)])
    state["time"].sleep = lambda _seconds: setattr(clock, "now", 100.0 + elapsed)
    with pytest.raises(state["TestFailure"], match="^timed out waiting for fixture$"):
        observer.wait_sample("fixture", lambda _sample: False, 1)
    if elapsed == 660:
        assert state["SELF_TEST_WAIT_FAILURE"][2] == 660
    else:
        assert state["SELF_TEST_WAIT_FAILURE"] is None


def test_observer_success_never_invokes_failure_diagnostics(monkeypatch) -> None:
    current = sample(100.0, 12345)
    observer, state, clock = observer_wait(monkeypatch, [current])

    def unexpected_diagnostic(*_args, **_kwargs):
        pytest.fail("Successful waits must not invoke failure diagnostics")

    monkeypatch.setitem(state, "safe_wait_failure_flags", unexpected_diagnostic)
    assert observer.wait_sample("fixture", lambda _sample: True, 1) is current
    assert clock.sleeps == []
    assert state["SELF_TEST_WAIT_FAILURE"] is None
