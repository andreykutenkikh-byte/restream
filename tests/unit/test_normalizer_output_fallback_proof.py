"""Retain valid source-stall evidence when output sampling wins the deadline race."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
OTHER_SOURCE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def load():
    return runpy.run_path(str(NORMALIZER), run_name="_output_fallback_proof_test")


def delayed_output_fallback(ns):
    # Pause at zero can leave two seconds of buffered normalized output.
    # These successful requests are each below the unchanged 200 ms limit.
    request = 0.190
    assert request < ns["METRICS_REQUEST_TIMEOUT_SECONDS"]
    watchdog = ns["MediaWatchdog"](("output", 100), 2.0)
    now = 2.0 + ns["MEDIA_POLL_INTERVAL_SECONDS"]
    decisions = []
    for _ in range(20):
        now = round(now + request, 6)
        keep, probe = watchdog.observe_output(True, ("output", 100), now)
        if keep and probe:
            started = now
            now = round(now + request, 6)
            keep = watchdog.observe_ingest(True, (SOURCE_ID, 500), started, now)
        decisions.append((now, keep))
        if not keep:
            break
        now = round(now + ns["MEDIA_POLL_INTERVAL_SECONDS"], 6)
    assert decisions[-2] == (4.58, True)
    assert decisions[-1] == (4.82, False)
    assert watchdog.failure_reason == ns["RESTART_REASON_OUTPUT_FALLBACK"]
    assert watchdog.joint_idle_since == pytest.approx(2.43)
    assert watchdog.joint_unchanged_observations == 6
    return watchdog, now


def first_confirmation(ns, gate, started):
    now = started
    for _ in range(32):
        if gate.observe(True, (SOURCE_ID, 500), now):
            return now
        now = round(now + 0.190 + ns["MEDIA_POLL_INTERVAL_SECONDS"], 6)
    pytest.fail("continuous valid source evidence did not reach the fixed grace")


def test_output_fallback_keeps_proof_and_meets_existing_reset_budget_without_shortening_grace():
    ns = load()
    watchdog, rejected = delayed_output_fallback(ns)
    gate = watchdog.confirmed_stall_gate(SOURCE_ID)
    assert gate is not None
    assert gate.idle_since == pytest.approx(2.43)
    assert gate.counter == 500 and gate.source_id == SOURCE_ID
    assert gate.unchanged_observations == 1  # Future samples are still required.
    assert ns["CONFIRMED_INPUT_STALL_REQUIRED_OBSERVATIONS"] == 3
    sparse_gate = watchdog.confirmed_stall_gate(SOURCE_ID)
    assert not sparse_gate.observe(True, (SOURCE_ID, 500), 8.440)
    assert sparse_gate.observe(True, (SOURCE_ID, 500), 8.445)

    first_childless_sample = round(
        rejected + ns["CHILD_STOP_GRACE_SECONDS"] + ns["MEDIA_POLL_INTERVAL_SECONDS"] + 0.190,
        6,
    )
    assert first_childless_sample == 5.085
    confirmed = first_confirmation(ns, gate, first_childless_sample)
    assert confirmed == pytest.approx(8.445)
    assert confirmed - 2.43 >= ns["CONFIRMED_INPUT_STALL_GRACE_SECONDS"] == 6.0

    # Replay the previous handoff with the actual existing confirmed-stall
    # class: discarding proof starts a new grace and misses the same 9 s cap.
    old_gate = ns["ConfirmedInputStallGate"](SOURCE_ID, 500, first_childless_sample)
    old_confirmed = first_confirmation(ns, old_gate, first_childless_sample)
    assert old_confirmed == pytest.approx(11.085) and old_confirmed > 9.0

    breaker = ns["RecoveryCircuitBreaker"](SOURCE_ID)
    assert not breaker.record_failure(watchdog.failure_reason, rejected)
    assert not breaker.opened and not ns["SOURCE_RESET_ELIGIBLE_REASONS"]
    assert breaker.open_after_confirmed_input_stall(gate.counter, confirmed)
    assert not breaker.should_attempt(confirmed)
    first_recheck, second_recheck = confirmed + 0.240, confirmed + 0.480
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 500), first_recheck)
        == (ns["RECOVERY_PREFLIGHT_WAIT"])
    )
    assert not breaker.should_attempt(first_recheck)
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 500), second_recheck)
        == (ns["RECOVERY_PREFLIGHT_READY"])
    )
    assert breaker.should_attempt(second_recheck)
    assert second_recheck == pytest.approx(8.925) and second_recheck < 9.0
    assert ns["VERIFIED_STALL_TIMEOUT_SECONDS"] == 2.0
    assert ns["OUTPUT_IDLE_FALLBACK_SECONDS"] == 2.5
    assert ns["REQUIRED_VERIFIED_STALL_OBSERVATIONS"] == 3
    assert ns["RECOVERY_MAX_API_ATTEMPTS"] == 3
    assert ns["RECOVERY_RETRY_COOLDOWN_SECONDS"] == 30.0


@pytest.mark.parametrize(
    "invalid",
    [
        "no_ingest",
        "two_observations",
        "ingest_growth",
        "output_growth",
        "missing_metrics",
        "output_metrics",
    ],
)
def test_output_fallback_without_continuous_joint_proof_cannot_carry_any_grace(invalid):
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    observations = 0 if invalid == "no_ingest" else 2 if invalid == "two_observations" else 3
    for step in range(1, observations + 1):
        now = step * 0.05
        assert watchdog.observe_output(True, ("output", 100), now) == (True, True)
        assert watchdog.observe_ingest(True, (SOURCE_ID, 500), now, now + 0.001)
    output_counter, rejected_at = 100, 2.5
    if invalid == "ingest_growth":
        assert watchdog.observe_ingest(True, (SOURCE_ID, 501), 0.2, 0.201)
    elif invalid == "output_growth":
        assert watchdog.observe_output(True, ("output", 101), 0.2) == (True, False)
        output_counter, rejected_at = 101, 2.701
    elif invalid == "missing_metrics":
        assert watchdog.observe_ingest(False, None, 0.2, 0.201)
    elif invalid == "output_metrics":
        assert watchdog.observe_output(False, None, 0.2) == (True, False)
    assert watchdog.observe_output(True, ("output", output_counter), 0.3) == (True, True)
    assert watchdog.observe_output(True, ("output", output_counter), rejected_at) == (False, False)
    assert watchdog.failure_reason == ns["RESTART_REASON_OUTPUT_FALLBACK"]
    assert watchdog.confirmed_stall_gate(SOURCE_ID) is None


@pytest.mark.parametrize("invalid", ["missing", "identity", "regression", "timing", "output"])
def test_fatal_metrics_or_identity_errors_do_not_carry_existing_joint_proof(invalid):
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    for step in range(1, 4):
        now = step * 0.05
        assert watchdog.observe_output(True, ("output", 100), now) == (True, True)
        assert watchdog.observe_ingest(True, (SOURCE_ID, 500), now, now + 0.001)
    if invalid == "output":
        assert watchdog.observe_output(True, ("replacement", 100), 0.2) == (False, False)
    else:
        sample = {
            "missing": None,
            "identity": (OTHER_SOURCE_ID, 500),
            "regression": (SOURCE_ID, 499),
            "timing": (SOURCE_ID, 500),
        }[invalid]
        assert not watchdog.observe_ingest(
            True, sample, 0.2, 0.199 if invalid == "timing" else 0.201
        )
    assert watchdog.confirmed_stall_gate(SOURCE_ID) is None


@pytest.mark.parametrize("changed", ["growth", "metrics", "identity", "regression"])
def test_carried_output_fallback_proof_still_requires_fresh_exact_source_confirmation(changed):
    ns = load()
    watchdog, _ = delayed_output_fallback(ns)
    assert watchdog.confirmed_stall_gate(OTHER_SOURCE_ID) is None
    gate = watchdog.confirmed_stall_gate(SOURCE_ID)
    assert gate is not None
    now = 5.085
    sample = {
        "growth": (SOURCE_ID, 501),
        "metrics": None,
        "identity": (OTHER_SOURCE_ID, 500),
        "regression": (SOURCE_ID, 499),
    }[changed]
    assert not gate.observe(changed != "metrics", sample, now)
    current_counter = 501 if changed == "growth" else 499 if changed == "regression" else 500
    assert not gate.observe(True, (SOURCE_ID, current_counter), now + 0.240)
    assert not gate.observe(True, (SOURCE_ID, current_counter), 8.445)
    assert not gate.observe(True, (SOURCE_ID, current_counter), now + 5.999)


@pytest.mark.parametrize("changed", ["growth", "metrics", "identity"])
def test_carried_proof_cannot_bypass_the_last_two_fresh_pre_reset_checks(changed):
    ns = load()
    watchdog, _ = delayed_output_fallback(ns)
    gate = watchdog.confirmed_stall_gate(SOURCE_ID)
    assert gate is not None
    confirmed = first_confirmation(ns, gate, 5.085)
    breaker = ns["RecoveryCircuitBreaker"](SOURCE_ID)
    assert breaker.open_after_confirmed_input_stall(gate.counter, confirmed)
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 500), confirmed + 0.240)
        == (ns["RECOVERY_PREFLIGHT_WAIT"])
    )
    sample = {"growth": (SOURCE_ID, 501), "metrics": None, "identity": (OTHER_SOURCE_ID, 500)}[
        changed
    ]
    outcome = breaker.observe_before_reset(changed != "metrics", sample, confirmed + 0.480)
    expected = "RECOVERY_PREFLIGHT_RESUMED" if changed == "growth" else "RECOVERY_PREFLIGHT_INVALID"
    assert outcome == ns[expected]
    assert not breaker.should_attempt(confirmed + 0.480)
    assert breaker.attempts == 0
