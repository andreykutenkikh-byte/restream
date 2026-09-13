"""Characterize the current poll handoff; this does not prove the historical CI cause."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test


def media_sample(finished: float, *, slate: bool = False) -> dict:
    return {
        "t": finished - 0.005 if slate else finished,
        "finished": finished,
        "live": not slate,
        "normalized": not slate,
        "normalized_ids": [] if slate else ["fixture-normalizer"],
        "normalized_bytes": None if slate else round(finished * 1000),
        "path_ready": True,
        "ingest_live": True,
        "ingest_ids": ["fixture-ingest"],
        "dut_metrics_ok": True,
        "sink_metrics_ok": True,
        "dut_alive": True,
        "sink_alive": True,
        "reader_alive": True,
    }


def deadline_handoff(monkeypatch, *, slate_finished: float):
    namespace = load_self_test()
    observer_type = namespace["Observer"]
    # Keep the actual wait, checked_snapshot, health checks and stored sample list.
    # Only the clock and background sample publication are driven synchronously.
    observer = observer_type.__new__(observer_type)
    threading.Thread.__init__(observer)
    observer.lock = threading.Lock()
    observer.monitoring_started = 100.0
    observer.samples = [media_sample(100.0)]
    monkeypatch.setattr(observer, "is_alive", lambda: True)
    clock = SimpleNamespace(now=100.0, sleeps=[])
    slate = media_sample(slate_finished, slate=True)
    pending = [media_sample(round(100.0 + index * 0.2, 3)) for index in range(1, 40)]
    pending.append(slate)

    def sleep(seconds):
        # One earlier scheduler delay creates the poll phase ending at 107.979.
        # The final 50 ms sleep is ordinary; the producer completes independently.
        delay = 0.029 if not clock.sleeps else 0.0
        clock.sleeps.append(seconds)
        clock.now = round(clock.now + seconds + delay, 6)
        while pending and pending[0]["finished"] <= clock.now:
            observer.samples.append(pending.pop(0))

    monkeypatch.setitem(
        observer_type.wait_sample.__globals__,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    proof = namespace["PauseSLATEEvidence"](100.0, 108.0, media_sample(99.9))
    evaluated = []

    def slate_state(current):
        evaluated.append((clock.now, current["finished"]))
        proof.observe(current)
        return (
            not current["normalized"]
            and not current["live"]
            and current["ingest_live"]
            and current["ingest_ids"] == ["fixture-ingest"]
            and current["path_ready"]
        )

    return observer, namespace, clock, slate, slate_state, evaluated


def test_baseline_waiter_misses_completed_slate_during_final_poll_sleep(monkeypatch):
    observer, namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990
    )
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture") as caught:
        observer.wait_sample("fixture", predicate, 8.0, not_before=100.0)

    assert evaluated[-1] == (107.979, 107.8)
    assert clock.now == 108.029
    assert observer.samples[-1] is slate
    assert slate["finished"] < 108.0 < clock.now
    state = namespace["Observer"].wait_sample.__globals__
    failure, flags, elapsed = state["SELF_TEST_WAIT_FAILURE"]
    assert failure is caught.value and elapsed == 8.029
    assert flags["live"] and flags["normalized"] and not flags["state_ok"]
    # The actual pause oracle accepts this interval. The waiter never evaluated it.
    assert predicate(slate)


@pytest.mark.parametrize("slate_finished", [108.0, 108.001])
def test_expired_slate_must_remain_failure_even_if_its_request_started_early(
    monkeypatch, slate_finished
):
    observer, namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=slate_finished
    )
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture"):
        observer.wait_sample("fixture", predicate, 8.0, not_before=100.0)

    assert evaluated[-1] == (107.979, 107.8)
    assert observer.samples[-1] is slate
    assert slate["t"] < 108.0 <= slate["finished"] <= clock.now
    # A final snapshot alone would be unsafe: the real fixed-cutoff oracle rejects it.
    with pytest.raises(namespace["TestFailure"], match="expired observation timing"):
        predicate(slate)


def test_same_waiter_accepts_slate_completed_before_its_last_poll(monkeypatch):
    observer, _namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.960
    )
    assert observer.wait_sample("fixture", predicate, 8.0, not_before=100.0) == slate
    assert evaluated[-1] == (107.979, 107.960)
    assert slate["finished"] < clock.now < 108.0
