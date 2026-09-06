from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
NORMALIZER = ROOT / "deploy" / "moblin-relay" / "moblin-relay-normalize"
RELAYCTL = ROOT / "deploy" / "moblin-relay" / "relayctl"
SOURCE_ID = "11111111-2222-4333-8444-555555555555"


def load_normalizer() -> dict[str, object]:
    return runpy.run_path(str(NORMALIZER), run_name="_confirmed_input_stall_normalizer")


def load_relayctl() -> dict[str, object]:
    with patch.dict(
        sys.modules,
        {
            "fcntl": ModuleType("fcntl"),
            "termios": ModuleType("termios"),
        },
    ):
        return runpy.run_path(str(RELAYCTL), run_name="_confirmed_input_stall_relayctl")


def test_confirmed_stall_requires_one_exact_source_and_continuous_valid_metrics() -> None:
    loaded = load_normalizer()
    gate_type = loaded["ConfirmedInputStallGate"]
    grace = loaded["CONFIRMED_INPUT_STALL_GRACE_SECONDS"]

    assert grace == 6.0
    assert grace > 2.0
    assert grace < 10.0
    assert loaded["CONFIRMED_INPUT_STALL_REQUIRED_OBSERVATIONS"] >= 3

    gate = gate_type(SOURCE_ID, 100, 1.0)
    assert gate.observe(True, (SOURCE_ID, 100), 1.1) is False
    assert gate.observe(True, (SOURCE_ID, 100), 6.999) is False
    assert gate.observe(True, (SOURCE_ID, 100), 7.0) is True

    # A metrics failure provides no stall evidence and starts a fresh full grace.
    gate = gate_type(SOURCE_ID, 100, 1.0)
    assert gate.observe(False, None, 6.9) is False
    assert gate.observe(True, (SOURCE_ID, 100), 7.0) is False
    assert gate.observe(True, (SOURCE_ID, 100), 12.999) is False
    assert gate.observe(True, (SOURCE_ID, 100), 13.0) is True

    # Any real media growth resets the grace; a different UUID is never evidence.
    gate = gate_type(SOURCE_ID, 100, 1.0)
    assert gate.observe(True, (SOURCE_ID, 101), 6.9) is False
    assert gate.observe(True, (SOURCE_ID, 101), 12.899) is False
    assert gate.observe(True, (SOURCE_ID, 101), 12.9) is False
    assert gate.observe(True, (SOURCE_ID, 101), 12.901) is True
    assert gate.observe(True, ("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", 101), 20.0) is False
    assert gate.observe(True, (SOURCE_ID, 101), 26.0) is False


def test_sparse_media_growth_can_reopen_bridge_without_consecutive_scrapes() -> None:
    loaded = load_normalizer()
    gate = loaded["GrowthGate"]()

    assert gate.observe(100) is False
    assert gate.observe(110) is False
    assert gate.observe(110) is False
    assert gate.observe(120) is True


def test_confirmed_stall_opens_the_existing_bounded_exact_uuid_circuit_once() -> None:
    loaded = load_normalizer()
    breaker = loaded["RecoveryCircuitBreaker"](SOURCE_ID)
    grace = loaded["CONFIRMED_INPUT_STALL_GRACE_SECONDS"]

    assert breaker.source_id == SOURCE_ID
    assert not loaded["SOURCE_RESET_ELIGIBLE_REASONS"]
    for observed_at in (1.0, 2.0, 3.0):
        assert breaker.record_failure(loaded["RESTART_REASON_CHILD_EXIT"], observed_at) is False
    assert breaker.opened is False

    assert breaker.open_after_confirmed_input_stall(100, grace) is True
    assert breaker.opened is True
    assert breaker.reason == loaded["RESTART_REASON_INGEST_CONFIRMED_STALL"]
    assert breaker.attempts == 0
    assert breaker.should_attempt(grace) is False
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 100), grace + 0.05)
        == loaded["RECOVERY_PREFLIGHT_WAIT"]
    )
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 100), grace + 0.10)
        == loaded["RECOVERY_PREFLIGHT_READY"]
    )
    assert breaker.should_attempt(grace + 0.10) is True
    assert breaker.open_after_confirmed_input_stall(100, grace + 1.0) is False

    failure = loaded["RECOVERY_RESULT_TRANSPORT"]
    cooldown = loaded["RECOVERY_RETRY_COOLDOWN_SECONDS"]
    now = grace + 0.10
    for attempt in range(loaded["RECOVERY_MAX_API_ATTEMPTS"]):
        assert breaker.should_attempt(now) is True
        breaker.record_result(failure, now)
        now += cooldown
        if attempt + 1 < loaded["RECOVERY_MAX_API_ATTEMPTS"]:
            assert breaker.should_attempt(now - 0.001) is False
            assert (
                breaker.observe_before_reset(True, (SOURCE_ID, 100), now - 0.05)
                == loaded["RECOVERY_PREFLIGHT_WAIT"]
            )
            assert (
                breaker.observe_before_reset(True, (SOURCE_ID, 100), now)
                == loaded["RECOVERY_PREFLIGHT_READY"]
            )
    assert breaker.exhausted is True
    assert breaker.should_attempt(now) is False


def test_failed_reset_is_cancelled_when_the_exact_source_resumes() -> None:
    loaded = load_normalizer()
    breaker = loaded["RecoveryCircuitBreaker"](SOURCE_ID)

    assert breaker.open_after_confirmed_input_stall(100, 10.0) is True
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 100), 10.05)
        == loaded["RECOVERY_PREFLIGHT_WAIT"]
    )
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 100), 10.10)
        == loaded["RECOVERY_PREFLIGHT_READY"]
    )
    breaker.record_result(loaded["RECOVERY_RESULT_TRANSPORT"], 10.10)
    assert breaker.opened is True
    assert breaker.completed is False
    assert breaker.attempts == 1

    assert breaker.source_has_resumed((SOURCE_ID, 110)) is True
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 110), 40.0)
        == loaded["RECOVERY_PREFLIGHT_RESUMED"]
    )
    assert breaker.cancel_after_source_resumed() is True
    assert breaker.opened is False
    assert breaker.exhausted is False
    assert breaker.attempts == 0
    assert breaker.should_attempt(100.0) is False

    event = loaded["RECOVERY_EVENT_SOURCE_RESUMED"]
    assert loaded["RECOVERY_EVENT_TOKENS"][event] == (
        "moblin-relay-normalize:recovery:source-resumed-before-reset"
    )


def test_pre_reset_metrics_or_identity_failure_requires_a_new_full_stall_proof() -> None:
    loaded = load_normalizer()
    breaker = loaded["RecoveryCircuitBreaker"](SOURCE_ID)

    assert breaker.open_after_confirmed_input_stall(100, 10.0) is True
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 100), 10.05)
        == loaded["RECOVERY_PREFLIGHT_WAIT"]
    )
    assert breaker.observe_before_reset(False, None, 10.10) == loaded["RECOVERY_PREFLIGHT_INVALID"]
    assert breaker.invalidate_unverified_source() is True
    assert breaker.opened is False
    assert breaker.should_attempt(100.0) is False

    # A different UUID is also invalid and can never become the POST target.
    assert breaker.open_after_confirmed_input_stall(100, 16.10) is True
    assert (
        breaker.observe_before_reset(
            True,
            ("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", 100),
            16.15,
        )
        == loaded["RECOVERY_PREFLIGHT_INVALID"]
    )
    assert breaker.invalidate_unverified_source() is True

    # A single real increase is enough to cancel the pending transport reset.
    assert breaker.open_after_confirmed_input_stall(100, 22.15) is True
    assert (
        breaker.observe_before_reset(True, (SOURCE_ID, 101), 22.20)
        == loaded["RECOVERY_PREFLIGHT_RESUMED"]
    )
    assert breaker.cancel_after_source_resumed() is True


def test_recovery_and_lifecycle_markers_are_fixed_and_secret_free() -> None:
    loaded = load_normalizer()
    restart_marker = loaded["RESTART_LOG_TOKENS"][loaded["RESTART_REASON_INGEST_CONFIRMED_STALL"]]
    state_markers = loaded["STATE_EVENT_TOKENS"]

    assert restart_marker == "moblin-relay-normalize:restart:ingest-confirmed-stall"
    assert state_markers[loaded["STATE_EVENT_SOURCE_ATTACHED"]] == (
        "moblin-relay-normalize:state:source-attached"
    )
    assert state_markers[loaded["STATE_EVENT_SOURCE_DETACHED"]] == (
        "moblin-relay-normalize:state:source-detached"
    )
    assert SOURCE_ID not in restart_marker
    assert all(SOURCE_ID not in marker for marker in state_markers.values())


def test_incident_classifier_maps_common_forward_failures_without_raw_details() -> None:
    classify = load_relayctl()["classify_incident"]
    expected = (
        "YouTube-forward",
        "YOUTUBE_FORWARD_INTERRUPTED",
        "The YouTube forward was interrupted; MediaMTX retries automatically",
    )
    failures = (
        "EOF",
        "connection refused",
        "network is unreachable",
        "no route to host",
        "no such host",
        "TLS handshake failure",
        "context deadline exceeded",
        "i/o timeout",
        "timed out",
        "temporary failure in name resolution",
    )
    for failure in failures:
        raw = f"[path relay-output] [RTMPS dest 0 deadbeef] {failure} private.example"
        assert classify(raw) == expected
        assert "private.example" not in repr(classify(raw))

    assert classify("moblin-relay-normalize:restart:ingest-confirmed-stall") == (
        "SRT input",
        "SRT_INPUT_MEDIA_STILL_STALLED",
        "The same Moblin input remained connected but media did not resume after the grace period",
    )
    assert classify("moblin-relay-normalize:state:source-attached") == (
        "SRT input",
        "SRT_INPUT_ATTACHED",
        "The server accepted a Moblin input connection",
    )
    assert classify("moblin-relay-normalize:state:source-detached") == (
        "SRT input",
        "SRT_INPUT_DETACHED",
        "The Moblin input connection ended or the relay was stopped",
    )
    assert classify("moblin-relay-normalize:recovery:source-resumed-before-reset") == (
        "SRT input",
        "SRT_INPUT_RESUMED",
        "Moblin media resumed after the network interruption; "
        "the pending transport reset was cancelled",
    )


def test_normalizer_wires_confirmed_stall_to_exact_source_recovery() -> None:
    loaded = load_normalizer()
    source = NORMALIZER.read_text(encoding="utf-8")
    supervisor = source.split("def run_supervisor(", 1)[1].split("def main()", 1)[0]

    assert "confirmed_input_stall = watchdog.confirmed_stall_gate(source_id)" in supervisor
    carry = source.split("def confirmed_stall_gate(", 1)[1].split("def observe_output(", 1)[0]
    assert "self.ingest_connection_id != source_id" in carry
    assert "self.failure_reason != RESTART_REASON_VERIFIED_STALL" in carry
    assert "ConfirmedInputStallGate(source_id, self.ingest_counter, self.joint_idle_since)" in carry
    assert "recovery.open_after_confirmed_input_stall(" in supervisor
    kick_call = (
        "kick_srt_source(\n"
        "                        control_api_port,\n"
        "                        source_id,"
    )
    assert kick_call in supervisor
    assert "confirmed_stall_detected = confirmed_input_stall.observe(" in supervisor
    assert "recovery.observe_before_reset(" in supervisor
    assert "recovery.cancel_after_source_resumed()" in supervisor
    assert "recovery.invalidate_unverified_source()" in supervisor
    opened_block = supervisor.split("if recovery.opened:", 1)[1].split("if child is None:", 1)[0]
    assert opened_block.index("recovery.observe_before_reset(") < opened_block.index(
        "recovery.should_attempt(now):"
    )
    assert supervisor.count("emit_recovery_event(RECOVERY_EVENT_THRESHOLD)") == 1
    assert not loaded["SOURCE_RESET_ELIGIBLE_REASONS"]
    assert "if confirmed_input_stall is None:" in supervisor
    assert "confirmed_input_stall = None" in supervisor
    assert "emit_state_event(STATE_EVENT_SOURCE_ATTACHED)" in supervisor
    assert "emit_state_event(STATE_EVENT_SOURCE_DETACHED)" in supervisor

    # The normalizer subprocess still receives only a sanitized environment.
    environment = load_normalizer()["sanitized_environment"](18554, 11936, 19998, SOURCE_ID)
    assert set(environment) == {
        "LANG",
        "LC_ALL",
        "PATH",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT",
        "MOBLIN_RELAY_INTERNAL_SRT_CONNECTION_ID",
    }
    assert not any("TOKEN" in name or "KEY" in name for name in environment)
    assert os.fspath(NORMALIZER) not in repr(environment)
