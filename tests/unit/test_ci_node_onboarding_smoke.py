"""Secret-safe diagnostics for the native relay onboarding smoke."""

from typing import Any

import pytest

from scripts.ci_node_onboarding_smoke import (
    safe_api_payload,
    safe_bootstrap_diagnostic_code,
    safe_relay_api_diagnostic,
)
from scripts.ci_output_smoke import SmokeFailure


def test_bootstrap_diagnostic_code_is_strictly_allowlisted() -> None:
    assert (
        safe_bootstrap_diagnostic_code(
            {"safe_error": {"code": "remote_command_failed", "message": "ignored"}}
        )
        == "remote_command_failed"
    )
    assert (
        safe_bootstrap_diagnostic_code(
            {
                "safe_error": {
                    "code": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A",
                    "message": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A",
                }
            }
        )
        == "unknown"
    )
    assert safe_bootstrap_diagnostic_code({"safe_error": "invalid"}) == "unknown"


def test_native_safe_failure_code_is_allowlisted() -> None:
    for code in (
        "relay_self_test_failed",
        "relay_self_test_decode_streams_failed",
        "relay_self_test_decode_format_failed",
        "relay_self_test_decode_gop_failed",
        "relay_self_test_decode_decoder_failed",
        "relay_self_test_decode_frames_failed",
        "relay_self_test_decode_timestamps_failed",
        "relay_self_test_timestamp_probe_pts_failed",
        "relay_self_test_timestamp_packet_dts_failed",
        "relay_self_test_timestamp_video_pts_failed",
        "relay_self_test_timestamp_audio_pts_failed",
        "relay_self_test_timestamp_gaps_failed",
        "relay_self_test_timestamp_av_sync_failed",
        "relay_self_test_auth_exclusivity_core_failed",
        "relay_self_test_auth_exclusivity_candidate_failed",
        "relay_self_test_auth_exclusivity_primary_failed",
        "relay_self_test_auth_exclusivity_live_failed",
        "relay_self_test_auth_exclusivity_ingest_failed",
        "relay_self_test_auth_exclusivity_normalizer_failed",
        "relay_self_test_auth_exclusivity_downstream_failed",
        "relay_self_test_auth_exclusivity_progress_failed",
        "relay_self_test_auth_exclusivity_observability_failed",
        "relay_self_test_auth_exclusivity_proof_failed",
        "relay_self_test_stall_resume_failed",
        "relay_self_test_stall_core_failed",
        "relay_self_test_stall_source_failed",
        "relay_self_test_stall_ingest_failed",
        "relay_self_test_stall_ingest_offline_failed",
        "relay_self_test_stall_ingest_identity_failed",
        "relay_self_test_stall_ingest_identity_pre_resume_failed",
        "relay_self_test_stall_ingest_identity_recovery_failed",
        "relay_self_test_stall_ingest_progress_failed",
        "relay_self_test_stall_helper_observability_failed",
        "relay_self_test_stall_helper_path_failed",
        "relay_self_test_stall_helper_forward_failed",
        "relay_self_test_stall_helper_state_failed",
        "relay_self_test_stall_normalizer_failed",
        "relay_self_test_stall_downstream_failed",
        "relay_self_test_stall_observability_failed",
        "relay_self_test_stall_identity_failed",
    ):
        assert safe_bootstrap_diagnostic_code({"safe_error": {"code": code}}) == code


def test_api_payload_password_marker_fails_closed() -> None:
    with pytest.raises(SmokeFailure):
        safe_api_payload(
            {"unexpected": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"},
            "unit test payload",
        )

    clean: dict[str, Any] = {"install_profile": "moblin_relay"}
    assert safe_api_payload(clean, "unit test payload") is clean


def test_relay_api_diagnostic_reports_only_allowlisted_readiness_facts() -> None:
    marker = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"
    diagnostic = safe_relay_api_diagnostic(
        {
            "items": [
                {
                    "id": marker,
                    "status": "connecting",
                    "node_kind": "moblin_relay",
                    "agent_version": "wrong-and-sensitive-" + marker,
                    "protocol_version": 9,
                    "capabilities": ["other", marker],
                    "hostname": marker,
                }
            ]
        },
        {
            "items": [
                {
                    "node_id": "different-" + marker,
                    "available": False,
                    "status": {
                        "service": "active",
                        "enabled": False,
                        "main_process": "stopped",
                        "srt_listener": "closed",
                        "source": "NONE",
                        "input_bitrate_bps": None,
                        "youtube_forward": "inactive",
                        "overall": "ok",
                        "youtube_url_configured": False,
                        "youtube_key_configured": False,
                        "portrait_profile": True,
                        "error_code": None,
                        marker: marker,
                    },
                }
            ]
        },
        expected_node_id="different-expected-" + marker,
        expected_host_fingerprint="different-fingerprint-" + marker,
    )

    assert diagnostic == {
        "node_count": "one",
        "relay_count": "one",
        "node_status": "connecting",
        "node_kind": "moblin_relay",
        "agent_version_match": False,
        "protocol_version_match": False,
        "relay_capability_present": False,
        "hostname_match": False,
        "completed_node_identity_match": False,
        "host_key_fingerprint_match": False,
        "host_key_trust_mode_match": False,
        "resolved_ip_present": False,
        "relay_identity_match": False,
        "relay_available": False,
        "relay_status_shape": "mapping",
        "relay_status_mismatches": ["service"],
        "relay_status_unexpected_fields": True,
    }
    assert marker not in repr(diagnostic)


def test_relay_api_diagnostic_distinguishes_missing_and_invalid_state() -> None:
    assert safe_relay_api_diagnostic({"items": []}, {"items": [{"status": "bad"}]}) == {
        "node_count": "none",
        "relay_count": "one",
        "relay_identity_match": False,
        "relay_available": "invalid",
        "relay_status_shape": "invalid",
    }
    assert safe_relay_api_diagnostic(None, None) == {
        "node_count": "invalid",
        "relay_count": "invalid",
    }
    unhashable = safe_relay_api_diagnostic(
        {"items": [{"status": [], "node_kind": {}}]},
        {"items": []},
    )
    assert unhashable["node_status"] == "unexpected"
    assert unhashable["node_kind"] == "unexpected"
