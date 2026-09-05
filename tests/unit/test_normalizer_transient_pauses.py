from __future__ import annotations

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"


def load():
    return runpy.run_path(str(NORMALIZER), run_name="_pause_policy_test")


@pytest.mark.parametrize("pause_seconds", [0.25, 0.50, 0.75, 1.0, 1.5, 1.9])
def test_short_pause_keeps_existing_processor_and_resumes_without_restart(pause_seconds):
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    # Three consecutive episodes prevent a stale idle timer from accumulating.
    for episode in range(3):
        start = episode * 4.0
        baseline = 100 + episode * 1000
        assert watchdog.observe_output(True, ("output", baseline), start)[0]
        steps = int(pause_seconds / 0.05)
        for step in range(1, steps + 1):
            now = start + step * 0.05
            keep, probe = watchdog.observe_output(True, ("output", baseline), now)
            assert keep and probe
            assert watchdog.observe_ingest(True, ("input", baseline), now, now + 0.001)
        assert watchdog.observe_output(
            True, ("output", baseline + 500), start + pause_seconds + 0.01
        ) == (True, False)
        assert watchdog.failure_reason == ""
        assert watchdog.joint_idle_since is None


def test_sustained_joint_stall_releases_output_within_unchanged_three_second_gate():
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    failed_at = None
    for step in range(1, 61):
        now = step * 0.05
        keep, probe = watchdog.observe_output(True, ("output", 100), now)
        if keep and probe:
            keep = watchdog.observe_ingest(True, ("input", 100), now, now + 0.001)
        if not keep:
            failed_at = now
            break
    assert failed_at is not None and 2.0 <= failed_at < 2.5
    assert watchdog.failure_reason == "verified-stall"
    assert failed_at + ns["CHILD_STOP_GRACE_SECONDS"] < 3.0


def test_stuck_output_with_advancing_ingress_is_bounded_but_not_called_network_failure():
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    failed_at = None
    for step in range(1, 61):
        now = step * 0.05
        keep, probe = watchdog.observe_output(True, ("output", 100), now)
        if keep and probe:
            keep = watchdog.observe_ingest(True, ("input", 100 + step), now, now + 0.001)
        if not keep:
            failed_at = now
            break
    assert failed_at is not None and 2.5 <= failed_at < 3.0
    assert watchdog.failure_reason == "output-fallback"
    assert not ns["SOURCE_RESET_ELIGIBLE_REASONS"]


def test_short_metrics_gap_does_not_kill_processor_or_count_as_ingress_stall():
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    for now in (0.25, 0.50, 1.0, 1.5, 1.9):
        assert watchdog.observe_output(False, None, now) == (True, False)
        assert watchdog.joint_idle_since is None
    assert watchdog.observe_output(True, ("output", 1000), 1.95) == (True, False)
    assert watchdog.failure_reason == ""


def test_long_metrics_gap_is_observability_failure_not_proof_to_reset_srt():
    ns = load()
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    assert watchdog.observe_output(False, None, 2.01) == (False, False)
    assert watchdog.failure_reason == "metrics-blind"
    assert watchdog.ingest_counter is None
    assert not ns["SOURCE_RESET_ELIGIBLE_REASONS"]


def test_watchdog_carries_continuous_input_proof_without_shortening_six_second_grace():
    ns = load()
    source = "11111111-2222-4333-8444-555555555555"
    watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
    for step in range(1, 51):
        now = step * 0.05
        keep, probe = watchdog.observe_output(True, ("output", 100), now)
        if keep and probe:
            keep = watchdog.observe_ingest(True, (source, 100), now, now + 0.001)
        if not keep:
            break
    gate = watchdog.confirmed_stall_gate(source)
    assert gate is not None
    assert gate.idle_since == pytest.approx(0.051)
    for now in (2.2, 3.0, 4.0, 5.0, 6.05):
        assert gate.observe(True, (source, 100), now) is False
    assert gate.observe(True, (source, 100), 6.052) is True
    assert gate.observe(False, None, 6.06) is False
    assert gate.observe(True, (source, 100), 6.1) is False
    assert gate.observe(True, (source, 100), 12.09) is False
    assert gate.observe(True, (source, 100), 12.101) is True

    assert watchdog.confirmed_stall_gate("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee") is None
    watchdog.reset_joint_idle()
    assert watchdog.confirmed_stall_gate(source) is None


def test_other_failure_reasons_cannot_carry_proof_into_srt_reset():
    ns = load()
    source = "11111111-2222-4333-8444-555555555555"
    for reason in ("metrics-blind", "output-fallback", "output-regression", "child-exit"):
        watchdog = ns["MediaWatchdog"](("output", 100), 0.0)
        watchdog.ingest_connection_id = source
        watchdog.ingest_counter = 100
        watchdog.joint_idle_since = 0.0
        watchdog.joint_unchanged_observations = 20
        watchdog.failure_reason = reason
        assert watchdog.confirmed_stall_gate(source) is None


def test_credential_preflight_zeroes_secret_and_never_starts_media(capsys):
    ns = load()
    token = bytearray(b"synthetic-test-token-not-a-real-secret")
    check = ns["check_control_credential"]
    with patch.dict(check.__globals__, {"read_control_token": lambda _path: token}):
        assert check() == 0
    assert not any(token)
    assert capsys.readouterr().err == "moblin-relay-normalize:credential-check:ready\n"


def test_credential_preflight_reports_fixed_reason_without_os_exception_or_token(capsys):
    ns = load()
    check = ns["check_control_credential"]

    def fail(_path):
        raise ns["ControlCredentialError"]("parent-owner-unsafe")

    with patch.dict(check.__globals__, {"read_control_token": fail}):
        assert check() == 1
    assert capsys.readouterr().err == (
        "moblin-relay-normalize:credential-check:parent-owner-unsafe\n"
    )
    ns["emit_credential_failure"](ns["ControlCredentialError"]("untrusted-secret-marker"))
    assert capsys.readouterr().err == "moblin-relay-normalize:credential-check:unavailable\n"
