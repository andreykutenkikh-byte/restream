from __future__ import annotations

import base64
from collections.abc import Sequence
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from bootstrap_worker.errors import BootstrapError, InvalidTransitionError, safe_failure
from bootstrap_worker.host_keys import HostKeyVerifier, normalize_fingerprint
from bootstrap_worker.models import BootstrapRequest, HostTrustMode, JobState
from bootstrap_worker.state_machine import JobStateMachine
from bootstrap_worker.targets import TargetPolicy


def test_native_relay_failures_have_stable_localized_messages() -> None:
    codes = {
        "unsupported_relay_operating_system",
        "relay_bundle_invalid",
        "remote_relay_conflict",
        "remote_relay_account_conflict",
        "relay_active_during_install",
        "relay_port_conflict",
        "invalid_relay_control_origin",
        "invalid_relay_target",
        "invalid_enrollment_token",
        "relay_dependency_install_failed",
        "relay_dependency_check_failed",
        "mediamtx_archive_invalid",
        "mediamtx_binary_invalid",
        "mediamtx_checksum_failed",
        "mediamtx_download_failed",
        "mediamtx_license_missing",
        "relay_slate_generation_failed",
        "relay_install_failed",
        "relay_agent_accounts_failed",
        "relay_agent_sysusers_failed",
        "relay_agent_tmpfiles_failed",
        "relay_agent_broker_failed",
        "relay_agent_copy_failed",
        "relay_agent_install_failed",
        "relay_agent_journal_failed",
        "relay_agent_preflight_failed",
        "relay_agent_units_failed",
        "relay_self_test_failed",
        "relay_unit_verify_failed",
        "relay_self_test_startup_failed",
        "relay_self_test_assets_failed",
        "relay_self_test_topology_failed",
        "relay_self_test_auth_failed",
        "relay_self_test_auth_source_failed",
        "relay_self_test_auth_source_helper_failed",
        "relay_self_test_auth_source_publisher_bind_failed",
        "relay_self_test_auth_source_feeder_failed",
        "relay_self_test_auth_source_path_failed",
        "relay_self_test_auth_live_failed",
        "relay_self_test_auth_scan_failed",
        "relay_self_test_auth_exclusivity_failed",
        "relay_self_test_auth_exclusivity_core_failed",
        "relay_self_test_auth_exclusivity_candidate_failed",
        "relay_self_test_auth_exclusivity_primary_failed",
        "relay_self_test_auth_exclusivity_live_failed",
        "relay_self_test_auth_exclusivity_ingest_failed",
        "relay_self_test_auth_exclusivity_normalizer_failed",
        "relay_self_test_auth_exclusivity_normalizer_child_exit_failed",
        "relay_self_test_auth_exclusivity_normalizer_start_timeout_failed",
        "relay_self_test_auth_exclusivity_normalizer_metrics_blind_failed",
        "relay_self_test_auth_exclusivity_normalizer_output_identity_failed",
        "relay_self_test_auth_exclusivity_normalizer_output_regression_failed",
        "relay_self_test_auth_exclusivity_normalizer_output_fallback_failed",
        "relay_self_test_auth_exclusivity_normalizer_ingest_timing_failed",
        "relay_self_test_auth_exclusivity_normalizer_ingest_missing_failed",
        "relay_self_test_auth_exclusivity_normalizer_ingest_identity_failed",
        "relay_self_test_auth_exclusivity_normalizer_ingest_regression_failed",
        "relay_self_test_auth_exclusivity_normalizer_verified_stall_failed",
        "relay_self_test_auth_exclusivity_normalizer_watchdog_unknown_failed",
        "relay_self_test_auth_exclusivity_downstream_failed",
        "relay_self_test_auth_exclusivity_progress_failed",
        "relay_self_test_auth_exclusivity_observability_failed",
        "relay_self_test_auth_exclusivity_proof_failed",
        "relay_self_test_live_ingest_failed",
        "relay_self_test_live_normalize_failed",
        "relay_self_test_normalizer_hook_failed",
        "relay_self_test_normalizer_child_failed",
        "relay_self_test_normalizer_publish_failed",
        "relay_self_test_normalizer_flap_failed",
        "relay_self_test_stall_slate_failed",
        "relay_self_test_stall_precondition_failed",
        "relay_self_test_stall_pause_failed",
        "relay_self_test_stall_switch_failed",
        "relay_self_test_stall_capture_failed",
        "relay_self_test_stall_resume_failed",
        "relay_self_test_stall_live_failed",
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
        "relay_self_test_stall_continuity_failed",
        "relay_self_test_crash_death_failed",
        "relay_self_test_crash_live_failed",
        "relay_self_test_crash_continuity_failed",
        "relay_self_test_outages_failed",
        "relay_self_test_outage_slate_failed",
        "relay_self_test_outage_normal_failed",
        "relay_self_test_outage_hold_failed",
        "relay_self_test_outage_live_failed",
        "relay_self_test_continuity_failed",
        "relay_self_test_decode_failed",
        "relay_self_test_decode_streams_failed",
        "relay_self_test_decode_format_failed",
        "relay_self_test_decode_format_video_codec_failed",
        "relay_self_test_decode_format_video_profile_failed",
        "relay_self_test_decode_format_video_level_failed",
        "relay_self_test_decode_format_video_b_frames_failed",
        "relay_self_test_decode_format_video_dimensions_failed",
        "relay_self_test_decode_format_video_pixel_format_failed",
        "relay_self_test_decode_format_video_r_frame_rate_failed",
        "relay_self_test_decode_format_audio_codec_failed",
        "relay_self_test_decode_format_audio_profile_failed",
        "relay_self_test_decode_format_audio_sample_rate_failed",
        "relay_self_test_decode_format_audio_channels_failed",
        "relay_self_test_decode_format_audio_layout_failed",
        "relay_self_test_decode_gop_failed",
        "relay_self_test_decode_decoder_failed",
        "relay_self_test_decode_frames_failed",
        "relay_self_test_decode_timestamps_failed",
        "relay_self_test_timestamp_probe_pts_failed",
        "relay_self_test_timestamp_packet_dts_failed",
        "relay_self_test_timestamp_video_pts_failed",
        "relay_self_test_timestamp_video_pts_offset_failed",
        "relay_self_test_timestamp_video_pts_order_failed",
        "relay_self_test_timestamp_video_frame_rate_failed",
        "relay_self_test_timestamp_audio_pts_failed",
        "relay_self_test_timestamp_gaps_failed",
        "relay_self_test_timestamp_gap_video_dts_failed",
        "relay_self_test_timestamp_gap_audio_dts_failed",
        "relay_self_test_timestamp_gap_video_pts_failed",
        "relay_self_test_timestamp_gap_audio_pts_failed",
        "relay_self_test_timestamp_gap_decoded_video_failed",
        "relay_self_test_timestamp_gap_decoded_audio_failed",
        "relay_self_test_timestamp_av_sync_failed",
        "relay_self_test_secrets_failed",
        "relay_self_test_cleanup_failed",
        "relay_final_check_failed",
    }
    fallback = safe_failure("unknown_test_failure").safe_message
    for code in codes:
        failure = safe_failure(code)
        assert failure.code == code
        assert failure.safe_message != fallback
        assert "\n" not in failure.safe_message


IMAGE = f"ghcr.io/andreykutenkikh-byte/restream-node@sha256:{'a' * 64}"
FINGERPRINT = "SHA256:" + base64.b64encode(bytes(range(32))).decode().rstrip("=")


class SequenceResolver:
    def __init__(self, responses: Sequence[Sequence[str]]) -> None:
        self.responses = list(responses)

    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return self.responses.pop(0)


def make_request(**updates: object) -> BootstrapRequest:
    values: dict[str, object] = {
        "node_id": uuid4(),
        "address": "node.example.com",
        "port": 22,
        "username": "root",
        "password": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A",
        "control_url": "https://restream.example.com",
        "node_agent_image": IMAGE,
    }
    values.update(updates)
    return BootstrapRequest.model_validate(values)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "169.254.169.254",
        "192.0.2.5",
        "2001:db8::1",
        "localhost",
        "ci-ssh-target",
        "host.docker.internal",
        "ssh://user:secret@example.com",
        "example.com/path",
        "user@example.com",
        "example.com?query=1",
        "example.com\ninvalid",
    ],
)
async def test_target_policy_rejects_non_public_or_structured_targets(address: str) -> None:
    policy = TargetPolicy(environment="production")
    with pytest.raises(BootstrapError, match="политик") as captured:
        await policy.resolve(address, 22)
    assert captured.value.code == "invalid_target"


async def test_target_policy_accepts_public_literal() -> None:
    target = await TargetPolicy(environment="production").resolve("8.8.8.8", 22)
    assert target.resolved_ip == "8.8.8.8"
    assert target.test_allowlisted is False


async def test_target_policy_rejects_any_private_dns_answer() -> None:
    policy = TargetPolicy(
        environment="production",
        resolver=SequenceResolver([("8.8.8.8", "10.0.0.1")]),
    )
    with pytest.raises(BootstrapError) as captured:
        await policy.resolve("node.example.com", 22)
    assert captured.value.code == "invalid_target"


async def test_dns_rebinding_cannot_replace_pinned_ip() -> None:
    resolver = SequenceResolver([("8.8.8.8",), ("1.1.1.1",)])
    policy = TargetPolicy(environment="production", resolver=resolver)
    target = await policy.resolve("node.example.com", 22)
    with pytest.raises(BootstrapError) as captured:
        await policy.revalidate(target)
    assert captured.value.code == "target_resolution_changed"


async def test_dns_revalidation_keeps_original_ip_when_answer_order_changes() -> None:
    resolver = SequenceResolver([("8.8.8.8", "1.1.1.1"), ("1.1.1.1", "8.8.8.8")])
    policy = TargetPolicy(environment="production", resolver=resolver)
    target = await policy.resolve("node.example.com", 22)
    checked = await policy.revalidate(target)
    assert checked.resolved_ip == "1.1.1.1"
    assert checked.resolution_set == ("1.1.1.1", "8.8.8.8")


async def test_private_test_target_requires_exact_test_allowlist() -> None:
    resolver = SequenceResolver([("172.20.0.5",)])
    policy = TargetPolicy(
        environment="test",
        test_allowlist=("ci-ssh-target:22",),
        resolver=resolver,
    )
    target = await policy.resolve("ci-ssh-target", 22)
    assert target.resolved_ip == "172.20.0.5"
    assert target.test_allowlisted is True

    with pytest.raises(BootstrapError):
        await policy.resolve("ci-ssh-target", 2222)


def test_test_target_allowlist_is_rejected_outside_tests() -> None:
    with pytest.raises(ValueError, match="ENVIRONMENT=test"):
        TargetPolicy(environment="production", test_allowlist=("ci-ssh-target:22",))
    with pytest.raises(ValueError, match="host:port"):
        TargetPolicy(environment="test", test_allowlist=("172.20.0.0/16",))


def test_fingerprint_normalization_and_tofu() -> None:
    assert normalize_fingerprint(FINGERPRINT.removeprefix("SHA256:")) == FINGERPRINT
    result = HostKeyVerifier(expected_fingerprint=None, pinned_fingerprint=None).verify(
        "ssh-ed25519", FINGERPRINT
    )
    assert result.fingerprint == FINGERPRINT
    assert result.trust_mode is HostTrustMode.TOFU


def test_expected_and_pinned_fingerprints_are_enforced() -> None:
    expected = HostKeyVerifier(expected_fingerprint=FINGERPRINT, pinned_fingerprint=None)
    assert expected.verify("ssh-ed25519", FINGERPRINT).trust_mode is HostTrustMode.EXPECTED
    pinned = HostKeyVerifier(expected_fingerprint=None, pinned_fingerprint=FINGERPRINT)
    assert pinned.verify("ssh-ed25519", FINGERPRINT).trust_mode is HostTrustMode.PINNED

    other = "SHA256:" + base64.b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
    with pytest.raises(BootstrapError) as captured:
        HostKeyVerifier(expected_fingerprint=FINGERPRINT, pinned_fingerprint=None).verify(
            "ssh-ed25519", other
        )
    assert captured.value.code == "ssh_host_key_changed"


def test_unsupported_host_key_algorithm_fails_closed() -> None:
    with pytest.raises(BootstrapError) as captured:
        HostKeyVerifier(expected_fingerprint=None, pinned_fingerprint=None).verify(
            "ssh-dss", FINGERPRINT
        )
    assert captured.value.code == "ssh_host_key_unsupported"


def test_secret_request_repr_is_redacted_and_image_is_digest_pinned() -> None:
    marker = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"
    request = make_request(password=marker, sudo_password="another-private-value")
    assert marker not in repr(request)
    assert "another-private-value" not in repr(request)
    assert isinstance(request.ssh_password, SecretStr)

    with pytest.raises(ValidationError):
        make_request(node_agent_image="ghcr.io/example/node:latest")
    with pytest.raises(ValidationError):
        make_request(control_url="http://restream.example.com")
    with pytest.raises(ValidationError):
        make_request(control_url="https://restream.example.com\n")
    with pytest.raises(ValidationError):
        make_request(recover_failed_install="true")
    with pytest.raises(ValidationError):
        make_request(rotate_existing_credential=True)
    with pytest.raises(ValidationError):
        make_request(
            adopt_empty_managed_root_for_test=True,
            node_agent_environment="production",
        )


def test_http_control_url_is_only_valid_for_explicit_test_agent() -> None:
    request = make_request(
        control_url="http://backend:8000",
        node_agent_environment="test",
    )
    assert request.node_agent_environment == "test"


@pytest.mark.parametrize("environment", ["development", "test"])
def test_nonproduction_agent_allows_valid_local_tag_and_http(environment: str) -> None:
    request = make_request(
        node_agent_environment=environment,
        node_agent_image="adojapan-restream-node:ci",
        control_url="http://backend:8000",
    )
    assert request.node_agent_image == "adojapan-restream-node:ci"


def test_nonproduction_agent_still_rejects_unsafe_image_reference() -> None:
    with pytest.raises(ValidationError):
        make_request(
            node_agent_environment="development",
            node_agent_image="adojapan-node:ci\ncommand",
            control_url="http://backend:8000",
        )


def test_state_machine_has_explicit_transitions_and_terminal_states() -> None:
    machine = JobStateMachine()
    machine.transition(JobState.RESOLVING)
    machine.transition(JobState.CONNECTING)
    machine.transition(JobState.VERIFYING_HOST_KEY)
    machine.transition(JobState.AUTHENTICATING)
    with pytest.raises(InvalidTransitionError):
        machine.transition(JobState.CHECKING_SYSTEM)
    machine.transition(JobState.CHECKING_PRIVILEGES)
    machine.transition(JobState.FAILED)
    with pytest.raises(InvalidTransitionError):
        machine.transition(JobState.RESOLVING)
