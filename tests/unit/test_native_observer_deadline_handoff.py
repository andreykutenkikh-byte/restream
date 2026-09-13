"""Prove the observer handoff mechanism without claiming the historical CI cause."""

from __future__ import annotations

import threading
from types import MethodType, SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

# Exact Observer.wait_sample from c491685fc351e8f9b789ba9fb62eed6bae5d17bf,
# dedented for execution against the same current helpers and controlled clock.
# Frozen here so testing the before/after mechanism never requires Git or media.
BASELINE_WAIT_SAMPLE = """\
def wait_sample(
    self,
    description: str,
    predicate,
    timeout: float,
    *,
    not_before: float = 0.0,
    health_check=None,
) -> dict:
    global SELF_TEST_WAIT_FAILURE
    started = time.monotonic()
    deadline = started + timeout
    fresh_after = max(started, not_before)
    current_sample = {}
    previous_sample = {}
    state_ok = False
    while time.monotonic() < deadline:
        if health_check is not None:
            health_check()
        sample = self.checked_snapshot(description)
        if sample.get("t") != current_sample.get("t"):
            previous_sample = current_sample
        current_sample = sample
        dead_processes = [
            name
            for name in ("dut", "sink", "reader")
            if sample and not sample.get(f"{name}_alive")
        ]
        if dead_processes:
            raise TestFailure(
                "core test process exited during a transition: " + ",".join(dead_processes)
            )
        state_ok = bool(
            sample.get("t", 0.0) >= fresh_after
            and complete_metrics_sample(sample)
            and predicate(sample)
        )
        if state_ok:
            return sample
        time.sleep(0.05)
    if health_check is not None:
        health_check()
    failure = TestFailure(f"timed out waiting for {description}")
    # Keep the last evaluated predicate and distinct observed samples;
    # diagnostics must not re-run a stateful predicate or replace failure.
    with contextlib.suppress(Exception):
        flags = safe_wait_failure_flags(
            current_sample,
            previous_sample,
            state_ok=state_ok,
            expected_ingest_ids=None,
        )
        elapsed = time.monotonic() - started
        if math.isfinite(elapsed) and 0 <= elapsed <= 660:
            SELF_TEST_WAIT_FAILURE = (failure, flags, round(elapsed, 3))
    raise failure
"""


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


def deadline_handoff(monkeypatch, *, slate_finished: float, baseline=False, late_wake=None):
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
    clock = SimpleNamespace(now=100.0, sleeps=[], waits=[], publications=[], snapshots=[])
    slate = media_sample(slate_finished, slate=True)
    pending = [media_sample(round(100.0 + index * 0.2, 3)) for index in range(1, 40)]
    pending.append(slate)

    def advance(target):
        clock.now = round(target, 6)
        while pending and pending[0]["finished"] <= clock.now:
            with observer.lock:
                current = pending.pop(0)
                observer.samples.append(current)
                clock.publications.append(current)
                observer.sample_ready.set()

    def sleep(seconds):
        # The same scheduler offset drives the preserved pre-fix polling method.
        delay = 0.029 if not clock.sleeps else 0.0
        clock.sleeps.append(seconds)
        advance(clock.now + seconds + delay)

    class ControlledEvent:
        ready = False

        def set(self):
            self.ready = True

        def clear(self):
            self.ready = False

        def wait(self, timeout):
            clock.waits.append((clock.now, timeout, self.ready))
            if self.ready:
                return True
            until = clock.now + timeout
            if pending and pending[0]["finished"] <= until:
                until = pending[0]["finished"]
                if pending[0] is slate and late_wake is not None:
                    until = late_wake
            elif len(clock.waits) == 1:
                until += 0.029
            advance(until)
            return self.ready

    observer.sample_ready = ControlledEvent()
    original_snapshot = observer.checked_snapshot

    def snapshot(description):
        current = original_snapshot(description)
        clock.snapshots.append(current)
        return current

    observer.checked_snapshot = snapshot

    monkeypatch.setitem(
        observer_type.wait_sample.__globals__,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    if baseline:
        scope = observer_type.wait_sample.__globals__
        exec(  # noqa: S102 - frozen repository method, no external code or inputs.
            compile(BASELINE_WAIT_SAMPLE, "c491685-observer-wait-sample", "exec"), scope
        )
        observer.wait_sample = MethodType(scope["wait_sample"], observer)
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

    clock.advance = advance
    return observer, namespace, clock, slate, slate_state, evaluated


def test_baseline_waiter_misses_completed_slate_during_final_poll_sleep(monkeypatch):
    observer, namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990, baseline=True
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

    assert clock.now == 108.0
    assert all(finished != slate_finished for _now, finished in evaluated)
    assert slate["t"] < 108.0 <= slate["finished"]
    # A final snapshot alone would be unsafe: the real fixed-cutoff oracle rejects it.
    with pytest.raises(namespace["TestFailure"], match="expired observation timing"):
        predicate(slate)


@pytest.mark.parametrize("slate_finished", [107.960, 107.990])
def test_publication_wakes_waiter_and_returns_original_snapshot_before_cutoff(
    monkeypatch, slate_finished
):
    observer, _namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=slate_finished
    )
    returned = observer.wait_sample("fixture", predicate, 8.0, not_before=100.0)
    assert returned is clock.snapshots[-1] and returned == slate
    assert returned["ingest_ids"] == ["fixture-ingest"]
    assert evaluated[-1] == (slate_finished, slate_finished)
    assert slate["finished"] == clock.now < 108.0


def test_timely_publication_cannot_pass_when_scheduler_resumes_waiter_after_cutoff(monkeypatch):
    observer, namespace, clock, slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990, late_wake=108.029
    )
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture"):
        observer.wait_sample("fixture", predicate, 8.0, not_before=100.0)
    assert slate["finished"] < 108.0 < clock.now
    assert observer.samples[-1] is slate
    assert all(finished != slate["finished"] for _now, finished in evaluated)


def test_publication_between_snapshot_and_wait_is_not_cleared_or_lost(monkeypatch):
    observer, _namespace, clock, slate, predicate, _evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990
    )
    original_snapshot = observer.checked_snapshot
    injected = []

    def snapshot(description):
        current = original_snapshot(description)
        if clock.now >= 107.95 and not injected:
            injected.append(current)
            clock.advance(slate["finished"])
        return current

    observer.checked_snapshot = snapshot
    assert observer.wait_sample("fixture", predicate, 8.0, not_before=100.0) == slate
    assert injected and injected[0]["live"]
    assert any(now == slate["finished"] and ready for now, _timeout, ready in clock.waits)
    assert clock.now == 107.990


@pytest.mark.parametrize(
    "changes",
    [{"t": 99.0}, {"dut_metrics_ok": False}, {"ingest_ids": ["replacement"]}],
)
def test_notification_never_overrides_freshness_metrics_or_original_ingest_identity(
    monkeypatch, changes
):
    observer, namespace, clock, slate, predicate, _evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990
    )
    slate.update(changes)
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture"):
        observer.wait_sample("fixture", predicate, 8.0, not_before=100.0)
    assert clock.now == 108.0


def test_old_notification_without_slate_does_not_create_a_passing_sample(monkeypatch):
    observer, namespace, clock, _slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990
    )
    observer.sample_ready.set()
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture"):
        observer.wait_sample("fixture", predicate, 0.1, not_before=100.0)
    assert evaluated and all(finished == 100.0 for _now, finished in evaluated)
    assert clock.now == 100.1
    assert not clock.waits[0][2]


def test_spurious_notification_rechecks_original_predicate_without_creating_slate(monkeypatch):
    observer, namespace, clock, _slate, predicate, evaluated = deadline_handoff(
        monkeypatch, slate_finished=107.990
    )
    original_wait = observer.sample_ready.wait
    spurious = []

    def wait(timeout):
        if not spurious:
            spurious.append(True)
            clock.advance(clock.now + 0.01)
            observer.sample_ready.set()
            return True
        return original_wait(timeout)

    observer.sample_ready.wait = wait
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for fixture"):
        observer.wait_sample("fixture", predicate, 0.1, not_before=100.0)
    assert len(evaluated) >= 2
    assert all(finished == 100.0 for _now, finished in evaluated)
    assert clock.now == 100.1
