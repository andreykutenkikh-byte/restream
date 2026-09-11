"""Video restart retains independent input proof, never creates reset authority.

Virtual serial request timings below exercise the real supervisor's branch order.
They are not media packet timestamps, actual API latency or full media acceptance.
"""

from __future__ import annotations

import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"
SOURCE = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def load():
    return runpy.run_path(str(NORMALIZER), run_name="_video_stall_proof_test")


def supervisor_witness(discard_video_proof):
    ns = load()
    clock, children, reasons, handoffs, resets, input_reads, handlers = (
        [0.0],
        [],
        [],
        [],
        [],
        [],
        {},
    )
    original_handoff = ns["MediaWatchdog"].confirmed_stall_gate

    def advance(seconds):
        clock[0] = round(clock[0] + seconds, 6)
        assert clock[0] < 13, "supervisor failed to reach bounded reset confirmation"

    def handoff(watchdog, source):
        gate = original_handoff(watchdog, source)
        handoffs.append((clock[0], watchdog.failure_reason, watchdog.joint_idle_since))
        assert gate is not None and gate.idle_since == watchdog.joint_idle_since
        return None if discard_video_proof and watchdog.failure_reason == "video-stalled" else gate

    class Reader:
        def __init__(self, _port, path, _parser):
            self.ingest = path != ns["OUTPUT_METRICS_PATH"]
            self.counter = 0

        def sample(self):
            advance(0.190)  # Below the unchanged 200 ms complete-request deadline.
            if self.ingest:
                counter = round(min(clock[0], 2.0) * 10000)
                input_reads.append((clock[0], counter))
                return True, (SOURCE, counter)
            if clock[0] < 2.0:
                self.counter += 100
            if clock[0] < 1.8:
                os.write(children[-1].writer, f"frame={self.counter}\nprogress=continue\n".encode())
            return True, ("output", self.counter)

        def close(self):
            pass

    class Child:
        def __init__(self, _argv, **kwargs):
            assert kwargs["stdout"] == subprocess.PIPE
            read_fd, self.writer = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.stopped = False
            children.append(self)

        def poll(self):
            return 0 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self, *, timeout):
            advance(timeout)  # Model the existing force-stop grace, not a new delay.
            return 0

    def reset(*_args):
        resets.append(clock[0])
        handlers[15](15, None)
        return ns["RECOVERY_RESULT_KICKED"]

    replacements = {
        "time": SimpleNamespace(monotonic=lambda: clock[0], sleep=advance),
        "signal": SimpleNamespace(
            SIGHUP=1,
            SIGINT=2,
            SIGTERM=15,
            signal=lambda number, handler: handlers.update({number: handler}),
        ),
        "subprocess": SimpleNamespace(
            Popen=Child,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
        "make_parent_death_setup": lambda _pid: lambda: None,
        "MetricsReader": Reader,
        "emit_restart_reason": lambda reason: reasons.append((clock[0], reason)),
        "emit_state_event": lambda _event: None,
        "emit_recovery_event": lambda _event: None,
        "kick_srt_source": reset,
    }
    try:
        with (
            patch.dict(ns["run_supervisor"].__globals__, replacements),
            patch.object(ns["MediaWatchdog"], "confirmed_stall_gate", handoff),
        ):
            assert ns["run_supervisor"](18554, 11936, 19998, SOURCE) == 0
        assert len(children) == 1 and children[0].stopped and children[0].stdout.closed
        assert len(resets) == 1 and len(handoffs) == 1
        assert [reason for _, reason in reasons] == ["video-stalled", "ingest-confirmed-stall"]
        return ns, {
            "reasons": reasons,
            "handoff": handoffs[0],
            "reset": resets[0],
            "input_reads": input_reads,
        }
    finally:
        for child in children:
            child.stdout.close()
            os.close(child.writer)


def test_real_supervisor_video_handoff_preserves_existing_proof_and_nine_second_budget():
    ns, before = supervisor_witness(discard_video_proof=True)
    _, after = supervisor_witness(discard_video_proof=False)
    assert before["handoff"] == after["handoff"] == (4.26, "video-stalled", 2.3)
    assert before["reset"] > 9.0
    assert after["reset"] == pytest.approx(8.845) and after["reset"] <= 9.0
    confirmed = after["reasons"][1][0]
    assert confirmed == pytest.approx(8.365)
    assert confirmed - after["handoff"][2] >= ns["CONFIRMED_INPUT_STALL_GRACE_SECONDS"] == 6.0
    prechecks = [item for item in after["input_reads"] if item[0] > confirmed]
    assert prechecks == [(8.605, 20000), (8.845, 20000)]
    assert ns["RECOVERY_PRE_RESET_REQUIRED_OBSERVATIONS"] == 2
    assert ns["CONFIRMED_INPUT_STALL_REQUIRED_OBSERVATIONS"] == 3
    assert ns["VERIFIED_STALL_TIMEOUT_SECONDS"] == 2.0
    assert ns["OUTPUT_IDLE_FALLBACK_SECONDS"] == 2.5
    assert ns["METRICS_REQUEST_TIMEOUT_SECONDS"] == 0.2
    assert not ns["SOURCE_RESET_ELIGIBLE_REASONS"]


def joint_proof(ns, observations=3):
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    for step in range(1, observations + 1):
        now = step * 0.05
        assert watchdog.observe_output(True, ("output", 100), now) == (True, True)
        assert watchdog.observe_ingest(True, (SOURCE, 500), now, now + 0.001)
    return watchdog


@pytest.mark.parametrize(
    "invalid",
    ["no_input", "two_samples", "output_growth", "input_growth", "output_blind", "input_blind"],
)
def test_video_stop_cannot_manufacture_input_stall_proof(invalid):
    ns = load()
    watchdog = joint_proof(ns, 0 if invalid == "no_input" else 2 if invalid == "two_samples" else 3)
    if invalid == "output_growth":
        assert watchdog.observe_output(True, ("output", 101), 0.2) == (True, False)
    elif invalid == "input_growth":
        assert watchdog.observe_ingest(True, (SOURCE, 501), 0.2, 0.201)
    elif invalid == "output_blind":
        assert watchdog.observe_output(False, None, 0.2) == (True, False)
    elif invalid == "input_blind":
        assert watchdog.observe_ingest(False, None, 0.2, 0.201)
    assert not watchdog.reject(ns["RESTART_REASON_VIDEO_STALLED"])
    assert watchdog.confirmed_stall_gate(SOURCE) is None
    breaker = ns["RecoveryCircuitBreaker"](SOURCE)
    for now in (2.5, 5.0, 7.5, 10.0):
        assert not breaker.record_failure(watchdog.failure_reason, now)
    assert not breaker.opened and not breaker.should_attempt(20.0)


@pytest.mark.parametrize("change", ["growth", "missing", "identity", "regression"])
def test_carried_video_proof_still_requires_full_grace_and_fresh_unchanged_input(change):
    ns = load()
    watchdog = joint_proof(ns)
    watchdog.reject(ns["RESTART_REASON_VIDEO_STALLED"])
    assert watchdog.confirmed_stall_gate(OTHER) is None
    gate = watchdog.confirmed_stall_gate(SOURCE)
    assert gate is not None and gate.unchanged_observations == 1
    assert not gate.observe(True, (SOURCE, 500), 6.05)  # Grace has not elapsed.
    assert gate.observe(True, (SOURCE, 500), 6.052)
    sample = {
        "growth": (SOURCE, 501),
        "missing": None,
        "identity": (OTHER, 500),
        "regression": (SOURCE, 499),
    }[change]
    assert not gate.observe(change != "missing", sample, 6.1)
    counter = 501 if change == "growth" else 499 if change == "regression" else 500
    assert not gate.observe(True, (SOURCE, counter), 6.34)
    assert not gate.observe(True, (SOURCE, counter), 12.099)


@pytest.mark.parametrize("change", ["growth", "missing", "identity"])
def test_final_recheck_cancels_or_invalidates_carried_video_proof(change):
    ns = load()
    watchdog = joint_proof(ns)
    watchdog.reject(ns["RESTART_REASON_VIDEO_STALLED"])
    gate = watchdog.confirmed_stall_gate(SOURCE)
    assert gate is not None
    assert not gate.observe(True, (SOURCE, 500), 6.052)
    assert gate.observe(True, (SOURCE, 500), 6.292)
    breaker = ns["RecoveryCircuitBreaker"](SOURCE)
    assert breaker.open_after_confirmed_input_stall(gate.counter, 6.292)
    assert breaker.observe_before_reset(True, (SOURCE, 500), 6.532) == ns["RECOVERY_PREFLIGHT_WAIT"]
    sample = {"growth": (SOURCE, 501), "missing": None, "identity": (OTHER, 500)}[change]
    outcome = breaker.observe_before_reset(change != "missing", sample, 6.772)
    expected = "RECOVERY_PREFLIGHT_RESUMED" if change == "growth" else "RECOVERY_PREFLIGHT_INVALID"
    assert outcome == ns[expected] and not breaker.should_attempt(6.772)
    if change == "growth":
        assert breaker.cancel_after_source_resumed()
    else:
        assert breaker.invalidate_unverified_source()
    assert not breaker.opened and breaker.attempts == 0
