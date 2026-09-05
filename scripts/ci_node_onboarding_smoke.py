"""Secret-safe native Moblin relay onboarding smoke for the public API.

The disposable SSH target runs the real native installer, pinned MediaMTX,
FFmpeg slate generation, and the installer's quick media self-test. A tiny
CI-only protocol peer sends the first relay heartbeat because production relay
code intentionally refuses non-HTTPS/non-production control origins.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from bootstrap_worker.relay_installer import _SELF_TEST_STAGE_CODES, RELAY_RELEASE
from scripts.ci_output_smoke import COMPOSE, APIClient, SmokeFailure

PASSWORD_MARKER = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"  # noqa: S105
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_RETAINED_LOG_BYTES = 80 * 1024 * 1024
JOB_TIMEOUT_SECONDS = 900
READY_TIMEOUT_SECONDS = 60
SAFE_BOOTSTRAP_DIAGNOSTIC_CODES = frozenset(
    {
        "agent_enrollment_failed",
        "bootstrap_failed",
        "bootstrap_rejected",
        "bootstrap_unavailable",
        "bootstrap_worker_restarted",
        "cancelled",
        "credential_rotation_unavailable",
        "insufficient_cpu",
        "insufficient_disk",
        "insufficient_memory",
        "invalid_enrollment_token",
        "invalid_job_state",
        "invalid_relay_control_origin",
        "invalid_relay_target",
        "invalid_target",
        "job_conflict",
        "job_not_found",
        "mediamtx_archive_invalid",
        "mediamtx_binary_invalid",
        "mediamtx_checksum_failed",
        "mediamtx_download_failed",
        "mediamtx_license_missing",
        "outbound_https_unavailable",
        "overall_timeout",
        "relay_active_during_install",
        "relay_agent_accounts_failed",
        "relay_agent_sysusers_failed",
        "relay_agent_tmpfiles_failed",
        "relay_agent_broker_failed",
        "relay_agent_copy_failed",
        "relay_agent_install_failed",
        "relay_agent_journal_failed",
        "relay_agent_preflight_failed",
        "relay_agent_units_failed",
        "relay_bundle_invalid",
        "relay_dependency_check_failed",
        "relay_dependency_install_failed",
        "relay_final_check_failed",
        "relay_install_failed",
        "relay_port_conflict",
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
        "relay_self_test_auth_exclusivity_normalizer_confirmed_input_stall_failed",
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
        "relay_self_test_dts_regression_failed",
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
        "relay_self_test_persistent_stall_precondition_failed",
        "relay_self_test_persistent_stall_slate_failed",
        "relay_self_test_persistent_stall_confirmation_failed",
        "relay_self_test_persistent_stall_reset_failed",
        "relay_self_test_persistent_stall_reconnect_failed",
        "relay_self_test_persistent_stall_source_failed",
        "relay_self_test_persistent_stall_continuity_failed",
        "relay_self_test_crash_death_failed",
        "relay_self_test_crash_live_failed",
        "relay_self_test_crash_continuity_failed",
        "relay_self_test_reset_precondition_failed",
        "relay_self_test_reset_injection_failed",
        "relay_self_test_reset_slate_failed",
        "relay_self_test_reset_circuit_failed",
        "relay_self_test_reset_kick_failed",
        "relay_self_test_reset_reconnect_failed",
        "relay_self_test_reset_source_failed",
        "relay_self_test_reset_continuity_failed",
        "relay_self_test_outages_failed",
        "relay_self_test_outage_slate_failed",
        "relay_self_test_outage_normal_failed",
        "relay_self_test_outage_hold_failed",
        "relay_self_test_outage_live_failed",
        "relay_self_test_continuity_failed",
        "relay_self_test_continuity_disconnect_failed",
        "relay_self_test_continuity_final_slate_failed",
        "relay_self_test_continuity_capture_failed",
        "relay_self_test_continuity_ledger_failed",
        "relay_self_test_continuity_reader_failed",
        "relay_self_test_sink_format_failed",
        "relay_self_test_sink_gop_failed",
        "relay_self_test_sink_decode_failed",
        "relay_self_test_sink_video_failed",
        "relay_self_test_sink_audio_failed",
        "relay_self_test_sink_timestamps_failed",
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
        "relay_slate_generation_failed",
        "remote_command_failed",
        "remote_command_timeout",
        "remote_relay_account_conflict",
        "remote_relay_conflict",
        "remote_upload_failed",
        "ssh_authentication_failed",
        "ssh_connection_failed",
        "ssh_host_key_changed",
        "ssh_host_key_unsupported",
        "sudo_password_invalid",
        "target_resolution_changed",
        "unsupported_relay_operating_system",
    }
)
SAFE_BOOTSTRAP_DIAGNOSTIC_CODES |= frozenset(
    code + "_timeout" for code in SAFE_BOOTSTRAP_DIAGNOSTIC_CODES if not code.endswith("_timeout")
)
SAFE_HEARTBEAT_DIAGNOSTIC_CODES = frozenset(
    {
        "accepted",
        "attempts_exhausted",
        "http_auth_rejected",
        "http_other",
        "http_payload_rejected",
        "http_protocol_conflict",
        "http_rate_limited",
        "http_server_error",
        "network_error",
        "response_invalid",
        "revoked",
        "starting",
        "token_invalid",
        "token_unreadable",
        "unexpected_root",
    }
)
SAFE_NODE_STATUSES = frozenset(
    {"installing", "connecting", "ready", "degraded", "offline", "revoked", "failed"}
)
SAFE_NODE_KINDS = frozenset({"generic_node", "moblin_relay"})
EXPECTED_RELAY_STATUS: dict[str, Any] = {
    "service": "inactive",
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
}
NATIVE_RESULT_FAILURES = frozenset(
    {
        "native_result_unreadable",
        "native_result_header",
        "native_result_same_session",
        "native_result_supervisor",
        "native_result_bridge",
        "native_result_persistent",
        "native_result_idle",
        "native_result_continuity",
        "native_result_forward",
        "native_result_media",
        "native_result_secrets",
        "native_result_cleanup",
    }
)
SMOKE_STAGES = NATIVE_RESULT_FAILURES | frozenset(
    {
        "bootstrap_job",
        "create_job",
        "credential_boundary",
        "host_fingerprint",
        "login",
        "password_non_persistence",
        "relay_ready",
        "remote_lifecycle",
        "remote_lifecycle_files",
        "remote_lifecycle_services",
        "remote_lifecycle_result",
        "remote_lifecycle_accounts",
        "revoke",
        "revoke_quiescence",
        "startup",
    }
)
_current_stage = "startup"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")


def require_marker_absent(payload: bytes, location: str) -> None:
    if PASSWORD_MARKER.encode("ascii") in payload:
        raise SmokeFailure(f"password persistence marker found in {location}")


def safe_api_payload[T](value: T, location: str) -> T:
    require_marker_absent(encoded(value), location)
    return value


def safe_bootstrap_diagnostic_code(payload: Mapping[str, Any]) -> str:
    error = payload.get("safe_error")
    if not isinstance(error, Mapping):
        return "unknown"
    code = error.get("code")
    if isinstance(code, str) and code in SAFE_BOOTSTRAP_DIAGNOSTIC_CODES:
        return code
    return "unknown"


_MEDIA_DIAGNOSTIC_MARKERS = frozenset(
    {
        "attached",
        "active",
        "detached",
        "child-exit",
        "start-timeout",
        "metrics-blind",
        "output-identity",
        "output-regression",
        "output-fallback",
        "ingest-timing",
        "ingest-missing",
        "ingest-identity",
        "ingest-regression",
        "verified-stall",
        "ingest-confirmed-stall",
        "watchdog-unknown",
        "reset-requested",
        "reset-succeeded",
    }
)
_MEDIA_FIRST_SEEN_MARKERS = frozenset({"attached", "active", "start-timeout", "child-exit"})


def _diagnostic_seconds(value: Any, maximum: float = 660) -> bool:
    # Compare the bound before isfinite so enormous JSON integers cannot overflow.
    return type(value) in {int, float} and 0 <= value <= maximum and math.isfinite(value)


def _safe_failure_media(value: Any) -> dict[str, Any] | None:
    """Reject the entire nested diagnostic on any non-schema value; never reflect text."""
    required = {"scope", "elapsed_seconds", "log_ok", "markers", "first_seen"}
    optional = {"supervisor_count", "child_count", "supervisor_seen_seconds", "child_seen_seconds"}
    reader = {"reader_input", "reader_output", "reader_frames"}
    if not isinstance(value, dict) or not required <= value.keys():
        return None
    scope = value.get("scope")
    if not isinstance(scope, str) or scope not in {"crash", "capture"}:
        return None
    if scope == "capture":
        required |= reader
    if not required <= value.keys() or not value.keys() <= required | optional:
        return None
    elapsed = value["elapsed_seconds"]
    if not _diagnostic_seconds(elapsed) or type(value["log_ok"]) is not bool:
        return None
    markers, first_seen = value["markers"], value["first_seen"]
    if (
        not isinstance(markers, dict)
        or not markers.keys() <= _MEDIA_DIAGNOSTIC_MARKERS
        or any(type(count) is not int or not 1 <= count <= 255 for count in markers.values())
        or not isinstance(first_seen, dict)
        or not first_seen.keys() <= _MEDIA_FIRST_SEEN_MARKERS & markers.keys()
        or any(not _diagnostic_seconds(seconds, elapsed) for seconds in first_seen.values())
    ):
        return None
    for name in ("supervisor_count", "child_count"):
        if name in value and (type(value[name]) is not int or not 0 <= value[name] <= 32):
            return None
    for name in ("supervisor_seen_seconds", "child_seen_seconds"):
        if name in value and not _diagnostic_seconds(value[name], elapsed):
            return None
    if scope == "capture" and (
        type(value["reader_input"]) is not bool
        or type(value["reader_output"]) is not bool
        or type(value["reader_frames"]) is not int
        or not 0 <= value["reader_frames"] <= 10000
    ):
        return None
    result = dict(value)
    result["elapsed_seconds"] = round(elapsed, 3)
    result["markers"] = dict(markers)
    result["first_seen"] = {name: round(seconds, 3) for name, seconds in first_seen.items()}
    for name in ("supervisor_seen_seconds", "child_seen_seconds"):
        if name in result:
            result[name] = round(result[name], 3)
    return result


def safe_self_test_progress(payload: Any, *, job_id: str) -> dict[str, Any]:
    """Only fixed stage names and bounded numbers may reach the CI log."""
    unavailable = {"progress": "unavailable"}
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        return unavailable
    stage = payload.get("stage")
    elapsed = payload.get("elapsed_seconds")
    segment = payload.get("strict_segment_index")
    failure_lines = payload.get("failure_lines")
    failure_flags = payload.get("failure_flags")
    failure_wait = payload.get("failure_wait_seconds")
    failure_media = payload.get("failure_media")
    safe_media = _safe_failure_media(failure_media) if failure_media is not None else None
    allowed_flags = {
        "live",
        "normalized",
        "path_ready",
        "ingest_live",
        "metrics_ok",
        "core_alive",
        "ingest_one",
        "sink_one",
        "sink_growth",
        "state_ok",
        "ingest_match",
    }
    if (
        not isinstance(stage, str)
        or stage not in _SELF_TEST_STAGE_CODES
        or not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or not 0 <= elapsed <= JOB_TIMEOUT_SECONDS
        or not math.isfinite(elapsed)
        or (segment is not None and (type(segment) is not int or not 1 <= segment <= 32))
        or (
            failure_lines is not None
            and (
                not isinstance(failure_lines, list)
                or not 1 <= len(failure_lines) <= 8
                or any(type(line) is not int or not 1 <= line <= 20_000 for line in failure_lines)
            )
        )
        or (
            failure_flags is not None
            and (
                not isinstance(failure_flags, dict)
                or not failure_flags
                or not set(failure_flags) <= allowed_flags
                or any(type(value) is not bool for value in failure_flags.values())
            )
        )
        or (
            failure_wait is not None
            and (
                not isinstance(failure_wait, (int, float))
                or isinstance(failure_wait, bool)
                or not 0 <= failure_wait <= 660
                or not math.isfinite(failure_wait)
            )
        )
        or (failure_media is not None and safe_media is None)
    ):
        return unavailable
    result: dict[str, Any] = {"stage": stage, "elapsed_seconds": round(elapsed, 3)}
    if segment is not None:
        result["strict_segment_index"] = segment
    if failure_lines is not None:
        result["failure_lines"] = failure_lines
    if failure_flags is not None:
        result["failure_flags"] = failure_flags
    if failure_wait is not None:
        result["failure_wait_seconds"] = round(failure_wait, 3)
    if safe_media is not None:
        result["failure_media"] = safe_media
    return result


def print_self_test_progress(job_id: str) -> None:
    """Inspect the one non-secret progress checkpoint on the disposable CI target."""
    diagnostic: dict[str, Any] = {"progress": "unavailable"}
    try:
        if str(UUID(job_id)) != job_id:
            raise ValueError("invalid job identity")
        result = compose(
            "exec",
            "-T",
            "ci-ssh-target",
            "python3",
            "-c",
            """
import json
import os
import stat
import time
from pathlib import Path

path = Path('/run/moblin-relay-self-test.progress.json')
try:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
            or not 0 < before.st_size <= 2048
            or not -5 <= time.time() - before.st_mtime <= 960):
        raise ValueError('unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        after = os.fstat(handle.fileno())
        if before != after:
            raise ValueError('unavailable')
        raw = handle.read(2049)
    if len(raw) > 2048:
        raise ValueError('unavailable')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('unavailable')
    print(json.dumps({key: value.get(key) for key in
        ('job_id', 'stage', 'elapsed_seconds', 'strict_segment_index', 'failure_lines',
         'failure_flags', 'failure_wait_seconds', 'failure_media')}))
except (OSError, ValueError):
    print('{}')
""",
            max_capture_bytes=4096,
        )
        diagnostic = safe_self_test_progress(json.loads(result.stdout), job_id=job_id)
    except (ValueError, SmokeFailure):
        pass
    print("Self-test progress diagnostic: " + json.dumps(diagnostic, sort_keys=True))


def _safe_item_count(value: Any) -> str:
    if not isinstance(value, list):
        return "invalid"
    if not value:
        return "none"
    return "one" if len(value) == 1 else "multiple"


def safe_relay_api_diagnostic(
    raw_nodes: Mapping[str, Any] | None,
    raw_relays: Mapping[str, Any] | None,
    *,
    expected_node_id: str | None = None,
    expected_host_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Reduce API state to fixed fields and booleans safe for CI output."""

    node_items = raw_nodes.get("items") if isinstance(raw_nodes, Mapping) else None
    relay_items = raw_relays.get("items") if isinstance(raw_relays, Mapping) else None
    diagnostic: dict[str, Any] = {
        "node_count": _safe_item_count(node_items),
        "relay_count": _safe_item_count(relay_items),
    }
    if isinstance(node_items, list) and len(node_items) == 1 and isinstance(node_items[0], Mapping):
        node = node_items[0]
        raw_status = node.get("status")
        raw_kind = node.get("node_kind")
        capabilities = node.get("capabilities")
        diagnostic.update(
            {
                "node_status": (
                    raw_status
                    if isinstance(raw_status, str) and raw_status in SAFE_NODE_STATUSES
                    else "unexpected"
                ),
                "node_kind": (
                    raw_kind
                    if isinstance(raw_kind, str) and raw_kind in SAFE_NODE_KINDS
                    else "unexpected"
                ),
                "agent_version_match": node.get("agent_version") == "1.2.6",
                "protocol_version_match": node.get("protocol_version") == 1,
                "relay_capability_present": isinstance(capabilities, list)
                and "moblin_relay" in capabilities,
                "hostname_match": node.get("hostname") == "ci-native-moblin-relay",
                "completed_node_identity_match": expected_node_id is None
                or node.get("id") == expected_node_id,
                "host_key_fingerprint_match": expected_host_fingerprint is None
                or node.get("host_key_fingerprint") == expected_host_fingerprint,
                "host_key_trust_mode_match": node.get("host_key_trust_mode") == "tofu",
                "resolved_ip_present": bool(node.get("resolved_ip")),
            }
        )
    if (
        isinstance(relay_items, list)
        and len(relay_items) == 1
        and isinstance(relay_items[0], Mapping)
    ):
        relay = relay_items[0]
        matching_node: Mapping[str, Any] | None = (
            node_items[0]
            if isinstance(node_items, list)
            and len(node_items) == 1
            and isinstance(node_items[0], Mapping)
            else None
        )
        status = relay.get("status")
        diagnostic.update(
            {
                "relay_identity_match": matching_node is not None
                and relay.get("node_id") == matching_node.get("id"),
                "relay_available": relay.get("available")
                if isinstance(relay.get("available"), bool)
                else "invalid",
                "relay_status_shape": "mapping" if isinstance(status, Mapping) else "invalid",
            }
        )
        if isinstance(status, Mapping):
            diagnostic["relay_status_mismatches"] = [
                field
                for field, expected in EXPECTED_RELAY_STATUS.items()
                if status.get(field) != expected
            ]
            diagnostic["relay_status_unexpected_fields"] = any(
                field not in EXPECTED_RELAY_STATUS for field in status
            )
    return diagnostic


def relay_fixture_diagnostic() -> dict[str, Any]:
    """Read only fixed CI fixture facts; never return process args or file contents."""

    try:
        result = compose(
            "exec",
            "-T",
            "ci-ssh-target",
            "python3",
            "-c",
            """
import json
import os
import subprocess
from pathlib import Path

allowed = {
    'accepted', 'attempts_exhausted', 'http_auth_rejected', 'http_other',
    'http_payload_rejected', 'http_protocol_conflict', 'http_rate_limited',
    'http_server_error', 'network_error', 'response_invalid', 'revoked',
    'starting', 'token_invalid', 'token_unreadable', 'unexpected_root',
}
state_root = Path('/run/adojapan-ci-relay-agent')
status_path = state_root / 'heartbeat.status'
try:
    status = status_path.read_text(encoding='ascii').strip()
except (OSError, UnicodeError):
    status = 'missing'
if status not in allowed:
    status = 'unknown' if status != 'missing' else status
pid_path = Path('/run/adojapan-ci-systemctl/relay-heartbeat.pid')
try:
    pid_text = pid_path.read_text(encoding='ascii').strip()
except (OSError, UnicodeError):
    pid_state = 'missing'
else:
    if not pid_text.isdecimal() or int(pid_text) < 2:
        pid_state = 'invalid'
    else:
        try:
            os.kill(int(pid_text), 0)
        except ProcessLookupError:
            pid_state = 'exited'
        except PermissionError:
            pid_state = 'alive'
        else:
            pid_state = 'alive'

def succeeds(argv):
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=5,
    ).returncode == 0

print(json.dumps({
    'fixture_probe': 'ok',
    'heartbeat_marker': (state_root / 'heartbeat.ok').is_file(),
    'heartbeat_process': pid_state,
    'heartbeat_status': status,
    'service_active': succeeds(['systemctl', 'is-active', '--quiet',
                                'adojapan-relay-agent.service']),
    'service_enabled': succeeds(['systemctl', 'is-enabled', '--quiet',
                                 'adojapan-relay-agent.service']),
    'token_readable_by_agent': succeeds([
        'runuser', '-u', 'restream-agent', '--', 'test', '-r',
        '/etc/adojapan-relay-agent/node.token',
    ]),
}, ensure_ascii=True, sort_keys=True))
""",
        )
        payload = json.loads(result.stdout.decode("ascii", errors="strict"))
    except (SmokeFailure, UnicodeError, ValueError):
        return {"fixture_probe": "unavailable"}
    if not isinstance(payload, dict):
        return {"fixture_probe": "invalid"}
    heartbeat_status = payload.get("heartbeat_status")
    pid_state = payload.get("heartbeat_process")
    if not isinstance(heartbeat_status, str) or heartbeat_status not in (
        SAFE_HEARTBEAT_DIAGNOSTIC_CODES | {"missing", "unknown"}
    ):
        heartbeat_status = "unknown"
    if not isinstance(pid_state, str) or pid_state not in {
        "alive",
        "exited",
        "invalid",
        "missing",
    }:
        pid_state = "invalid"
    return {
        "fixture_probe": "ok",
        "heartbeat_marker": payload.get("heartbeat_marker") is True,
        "heartbeat_process": pid_state,
        "heartbeat_status": heartbeat_status,
        "service_active": payload.get("service_active") is True,
        "service_enabled": payload.get("service_enabled") is True,
        "token_readable_by_agent": payload.get("token_readable_by_agent") is True,
    }


def print_relay_readiness_diagnostic(
    client: APIClient,
    *,
    expected_node_id: str,
    expected_host_fingerprint: str,
) -> None:
    """Emit one bounded allowlisted snapshot after a relay readiness failure."""

    try:
        raw_nodes = safe_api_payload(client.request("GET", "/api/nodes"), "node diagnostic API")
        nodes = raw_nodes if isinstance(raw_nodes, Mapping) else None
    except SmokeFailure:
        nodes = None
    try:
        raw_relays = safe_api_payload(
            client.request("GET", "/api/relay-nodes"), "relay diagnostic API"
        )
        relays = raw_relays if isinstance(raw_relays, Mapping) else None
    except SmokeFailure:
        relays = None
    diagnostic = {
        "api": safe_relay_api_diagnostic(
            nodes,
            relays,
            expected_node_id=expected_node_id,
            expected_host_fingerprint=expected_host_fingerprint,
        ),
        "fixture": relay_fixture_diagnostic(),
    }
    require_marker_absent(encoded(diagnostic), "relay readiness diagnostic")
    rendered = json.dumps(diagnostic, ensure_ascii=True, sort_keys=True)
    print(f"Relay readiness diagnostic: {rendered}")


def set_smoke_stage(stage: str) -> None:
    global _current_stage  # noqa: PLW0603 - single-process diagnostic state
    if stage not in SMOKE_STAGES:
        raise ValueError("unsupported smoke diagnostic stage")
    _current_stage = stage


def compose(
    *arguments: str,
    input_bytes: bytes | None = None,
    expected: Sequence[int] = (0,),
    max_capture_bytes: int = MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Compose argv and bounded capture
            (*COMPOSE, *arguments),
            cwd=".",
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure("bounded Compose probe failed") from exc
    if result.returncode not in expected:
        raise SmokeFailure("bounded Compose probe returned an unexpected status")
    if len(result.stdout) + len(result.stderr) > max_capture_bytes:
        raise SmokeFailure("bounded Compose probe exceeded its capture limit")
    return result


def wait_for[T](description: str, timeout: float, probe: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(0.5)
    raise SmokeFailure(f"timed out waiting for {description}")


def ci_host_fingerprint() -> str:
    result = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "ssh-keygen",
        "-E",
        "sha256",
        "-lf",
        "/etc/ssh/ssh_host_ed25519_key.pub",
    )
    output = result.stdout.decode("ascii", errors="strict").strip()
    match = re.fullmatch(r"\d+ (SHA256:[A-Za-z0-9+/]{43}) .+ \(ED25519\)", output)
    if match is None:
        raise SmokeFailure("CI SSH host fingerprint is invalid")
    return match.group(1)


def poll_job(client: APIClient, job_id: str) -> Mapping[str, Any]:
    def probe() -> Mapping[str, Any] | None:
        raw = safe_api_payload(
            client.request("GET", f"/api/nodes/bootstrap/{job_id}"),
            "bootstrap job API",
        )
        require(isinstance(raw, dict), "bootstrap job response is invalid")
        require(raw.get("install_profile") == "moblin_relay", "bootstrap profile changed")
        state = str(raw.get("state", ""))
        if state in {"failed", "cancelled"}:
            print(f"Bootstrap terminal safe code: {safe_bootstrap_diagnostic_code(raw)}")
            print_self_test_progress(job_id)
            raise SmokeFailure("bootstrap job reached a non-success terminal state")
        return raw if state == "completed" else None

    return wait_for("native relay bootstrap completion", JOB_TIMEOUT_SECONDS, probe)


def ready_relay(client: APIClient) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    def probe() -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
        raw_nodes = safe_api_payload(client.request("GET", "/api/nodes"), "node list API")
        require(isinstance(raw_nodes, dict), "node list response is invalid")
        nodes = raw_nodes.get("items")
        require(isinstance(nodes, list) and len(nodes) == 1, "node list is not singular")
        node = nodes[0]
        require(isinstance(node, dict), "node response is invalid")
        if node.get("status") != "ready":
            return None
        require(node.get("node_kind") == "moblin_relay", "node kind is not moblin_relay")
        require(node.get("agent_version") == "1.2.6", "relay agent version is invalid")
        require(node.get("protocol_version") == 1, "relay protocol version is invalid")
        capabilities = node.get("capabilities")
        require(
            isinstance(capabilities, list) and "moblin_relay" in capabilities,
            "relay capability is missing",
        )
        require(
            node.get("hostname") == "ci-native-moblin-relay",
            "native relay hostname is invalid",
        )

        raw_relays = safe_api_payload(client.request("GET", "/api/relay-nodes"), "relay list API")
        require(isinstance(raw_relays, dict), "relay list response is invalid")
        relays = raw_relays.get("items")
        require(isinstance(relays, list) and len(relays) == 1, "relay list is not singular")
        relay = relays[0]
        require(isinstance(relay, dict), "relay response is invalid")
        require(relay.get("node_id") == node.get("id"), "relay and node identity diverged")
        require(relay.get("available") is True, "relay heartbeat is not fresh")
        status = relay.get("status")
        require(isinstance(status, dict), "relay status is invalid")
        require(status == EXPECTED_RELAY_STATUS, "fresh relay status is invalid")
        return cast(Mapping[str, Any], node), cast(Mapping[str, Any], relay)

    return wait_for("ready native relay heartbeat", READY_TIMEOUT_SECONDS, probe)


def native_self_test_result_failure(result: Any) -> str | None:
    """Validate the current quick-test contract without returning report values.

    This same pure function executes in the disposable target; only its fixed
    failure code leaves the target. Aggregate RTSP recorder artifacts are not
    the corruption oracle: strict final-RTMP segments and delivery evidence are.
    """

    def section(name: str) -> dict[str, Any]:
        value = result.get(name)
        return value if isinstance(value, dict) else {}

    def number(value: Any, low: float, high: float) -> bool:
        return type(value) in (int, float) and low <= value <= high and math.isfinite(value)

    def flags(value: dict[str, Any], *names: str) -> bool:
        return all(value.get(name) is True for name in names)

    if not isinstance(result, dict) or not (
        result.get("status") == "PASS"
        and result.get("mode") == "quick"
        and result.get("result_phase") == "final"
        and result.get("outage_targets_seconds") == [15, 17, 19]
    ):
        return "native_result_header"
    same = section("same_session_stall")
    if not flags(
        same,
        "srt_connection_preserved",
        "upstream_processes_preserved",
        "feeder_thread_preserved",
        "feeder_remux_preserved",
        "normalizer_reconnected",
    ) or not number(same.get("max_capture_no_growth_seconds"), 0, 3):
        return "native_result_same_session"
    supervisor = section("supervisor_crash_recovery")
    if not flags(
        supervisor,
        "ffmpeg_parent_death_passed",
        "srt_connection_preserved",
        "automatic_rtmp_recovery",
        "delivery_byte_growth_validated",
        "rtsp_capture_pause_diagnostic_only",
    ) or not (
        supervisor.get("recovery_limit_seconds") == 12.0
        and number(supervisor.get("total_recovery_seconds"), 0, 12)
    ):
        return "native_result_supervisor"
    bridge = section("repeated_bridge_failure_recovery")
    if not flags(
        bridge,
        "srt_session_preserved",
        "source_processes_preserved",
        "slate_available",
        "live_restored_after_each_failure",
        "restored_bridge_marker",
        "automatic_rtmp_recovery",
        "delivery_byte_growth_validated",
        "rtsp_capture_pause_diagnostic_only",
    ) or not (
        type(bridge.get("forced_ffmpeg_failures")) is int
        and bridge["forced_ffmpeg_failures"] == 3
        and bridge.get("source_reset_requested") is False
        and bridge.get("source_reset_succeeded") is False
    ):
        return "native_result_bridge"
    persistent = section("persistent_input_stall_recovery")
    if not flags(
        persistent,
        "same_srt_connection_confirmed_stalled",
        "confirmation_grace_observed",
        "stalled_srt_session_observed_before_recovery",
        "confirmed_stall_marker_seen",
        "source_detached_marker_seen",
        "source_reset_succeeded",
        "reset_before_transport_idle_timeout",
        "automatic_srt_recovery",
        "srt_session_replaced",
        "source_processes_preserved",
        "automatic_rtmp_recovery",
    ) or not (
        persistent.get("recovery_path") == "exact-api-reset"
        and number(persistent.get("reset_elapsed_seconds"), 6, 9)
        and number(persistent.get("max_capture_no_growth_seconds"), 0, 3)
    ):
        return "native_result_persistent"

    idle = result.get("srt_idle_expiry_seconds")
    final = section("final_transition")
    if not isinstance(idle, list) or len(idle) != 3:
        return "native_result_idle"
    idle_events = dict(
        zip((f"outage {index} SRT idle expiry" for index in range(1, 4)), idle, strict=True)
    )
    idle_events["final SRT idle expiry"] = final.get("srt_idle_expiry_seconds")
    proved = result.get("confirmed_reset_outage_disconnects", [])
    if not isinstance(proved, list) or len(proved) > 4:
        return "native_result_idle"
    seen: set[str] = set()
    for proof in proved:
        if not isinstance(proof, dict) or set(proof) != {"event", "elapsed_seconds"}:
            return "native_result_idle"
        event = proof["event"]
        elapsed = proof["elapsed_seconds"]
        if (
            not isinstance(event, str)
            or event not in idle_events
            or event in seen
            or not number(elapsed, 6, 8)
            or elapsed != idle_events[event]
        ):
            return "native_result_idle"
        seen.add(event)
    for event, elapsed in idle_events.items():
        if not number(elapsed, 6, 13) or (elapsed < 8 and event not in seen):
            return "native_result_idle"
    # The self-test emits an early-disconnect entry only after exact UUID,
    # continuous byte-counter evidence and fresh ordered reset markers pass.
    # Rounded 7.999x may serialize as 8.0; no unproved early value is accepted.
    outages = result.get("outage_max_capture_no_growth_seconds")
    if not (
        isinstance(outages, list)
        and len(outages) == 3
        and all(number(value, 0, 3) for value in outages)
        and number(final.get("max_capture_no_growth_seconds"), 0, 3)
        and number(result.get("overall_max_capture_no_growth_seconds"), 0, 3)
        and number(result.get("max_metrics_blind_seconds"), 0, 3)
        and result.get("rtsp_capture_session_preserved") is True
    ):
        return "native_result_continuity"

    expected_events = [
        "initial healthy LIVE",
        "same-session SLATE transition",
        "same-session LIVE recovery",
        "persistent-stall SLATE transition",
        "persistent-stall LIVE recovery",
        "supervisor-crash SLATE transition",
        "supervisor-crash LIVE recovery",
    ]
    for index in range(1, 4):
        expected_events.extend(
            (
                f"repeated-failure {index} SLATE transition",
                f"LIVE after forced bridge failure {index}",
            )
        )
    for index in range(1, 4):
        expected_events.extend(
            (f"outage {index} SLATE transition", f"outage {index} LIVE recovery")
        )
    expected_events.extend(("forced RTMP sink reconnect", "final SLATE transition"))
    ledger = result.get("event_to_delivery_recovery")
    if not isinstance(ledger, list) or len(ledger) != len(expected_events):
        return "native_result_forward"
    rotations = 0
    previous_dts = 0
    maximum_recovery = 0.0
    for entry, expected in zip(ledger, expected_events, strict=True):
        if not isinstance(entry, dict):
            return "native_result_forward"
        elapsed = entry.get("recovery_seconds")
        rotated = entry.get("publisher_rotated")
        dts = entry.get("known_dts_marker_count")
        if not (
            entry.get("event") == expected
            and isinstance(elapsed, (int, float))
            and number(elapsed, 0, 15)
            and type(rotated) is bool
            and type(dts) is int
            and dts >= previous_dts
        ):
            return "native_result_forward"
        if expected == "forced RTMP sink reconnect":
            if not rotated:
                return "native_result_forward"
        elif rotated and dts == previous_dts:
            return "native_result_forward"
        previous_dts = dts
        rotations += int(rotated)
        maximum_recovery = max(maximum_recovery, elapsed)
    forward = section("automatic_rtmp_forward_recovery")
    if not (
        forward.get("recovery_limit_seconds") == 15.0
        and forward.get("event_count") == len(expected_events)
        and forward.get("maximum_event_to_delivery_seconds") == round(maximum_recovery, 3)
        and number(forward.get("max_delivery_outage_seconds"), 0, 15)
        and forward.get("id_rotation_count") == rotations
        and forward.get("events_with_publisher_rotation") == rotations
        and forward.get("duplicate_publishers") is False
        and forward.get("counter_regression") is False
        and type(forward.get("invalid_samples")) is int
        and forward["invalid_samples"] == 0
        and flags(forward, "forced_disconnect_recovered", "final_active")
        and number(forward.get("forced_disconnect_max_rtsp_capture_no_growth_seconds"), 0, 3)
        and forward.get("test_scope") == "isolated-loopback-immediate-failure"
        and flags(
            section("rtmp_transition_log_contract"),
            "dts_reconnects_recovered",
            "missing_or_unset_timestamps_absent",
            "mux_invalid_argument_absent",
        )
    ):
        return "native_result_forward"

    strict = section("strict_sink_segment_validation")
    capture = section("strict_sink_segment_capture")
    if not (
        strict.get("segments") == capture.get("segments") == 13
        and strict.get("capture_bytes") == capture.get("capture_bytes")
        and all(
            number(strict.get(key), 1, 10**12)
            for key in ("capture_bytes", "video_frames", "audio_frames", "keyframes")
        )
        and strict.get("temporary_segments_retained") == 0
        and capture.get("pending_validation") == 0
        and result.get("landscape_1280x720_regression_absent") is True
        and result.get("legacy_portrait_720x1280_regression_absent") is True
    ):
        return "native_result_media"
    for name in ("secret_scan_while_live", "secret_scan"):
        scan = section(name)
        if not (
            scan.get("unexpected_file_hits") == []
            and all(
                scan.get(key) is False
                for key in ("journal_hit", "process_cmdline_hit", "process_environment_hit")
            )
        ):
            return "native_result_secrets"
    if not (
        flags(result, "test_ports_released", "workdir_removed")
        and type(result.get("secret_configs_wiped")) is int
        and result["secret_configs_wiped"] > 0
        and "cleanup_failure" not in result
    ):
        return "native_result_cleanup"
    return None


def native_result_probe_source() -> str:
    """Send repository code, not secret report contents, across the CI boundary."""
    return (
        "from __future__ import annotations\nimport json, math, os, stat\n"
        + inspect.getsource(native_self_test_result_failure)
        + """
failure = 'native_result_unreadable'
try:
    descriptor = os.open('/var/lib/moblin-relay/tests/last-quick-result.json',
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if (stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
                and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_nlink == 1 and 0 < metadata.st_size <= 1048576):
            raw = handle.read(1048577)
            if len(raw) <= 1048576:
                failure = native_self_test_result_failure(json.loads(raw))
except (OSError, ValueError, TypeError, OverflowError):
    pass
print(failure or 'NATIVE_SELF_TEST_RESULT_OK')
"""
    )


def verify_remote_lifecycle() -> None:
    set_smoke_stage("remote_lifecycle_files")
    compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        """
set -eu
test "$(cat /etc/moblin-relay/.managed-by-adojapan)" = 'adojapan-moblin-relay:v1'
test "$(cat /etc/moblin-relay/release)" = "$1"
test "$(stat -c '%U:%G:%a' /etc/moblin-relay/secrets.json)" = 'root:root:600'
test "$(stat -c '%U:%G:%a' /etc/adojapan-relay-agent/node.token)" = \
  'restream-agent:restream-agent:600'
test "$(stat -c '%U:%G:%a' /etc/adojapan-relay-agent/preview-reader.token)" = \
  'restream-agent:restream-agent:600'
""",
        "native-lifecycle-files",
        RELAY_RELEASE,
    )
    set_smoke_stage("remote_lifecycle_services")
    compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        """
set -eu
systemctl is-active --quiet adojapan-relay-agent.service
systemctl is-enabled --quiet adojapan-relay-agent.service
systemctl is-active --quiet adojapan-relay-broker.socket
systemctl is-enabled --quiet adojapan-relay-broker.socket
! systemctl is-active --quiet moblin-relay.service
! systemctl is-enabled --quiet moblin-relay.service
/opt/moblin-relay/bin/mediamtx --version | grep -Fx 'v1.20.1' >/dev/null
/usr/local/sbin/relayctl status | grep -Fx 'YouTube RTMPS URL: not configured' >/dev/null
/usr/local/sbin/relayctl status | grep -Fx 'YouTube stream key: not configured' >/dev/null
python3 - <<'PY'
import json
from pathlib import Path

node = json.loads(Path('/etc/moblin-relay/node.json').read_text(encoding='ascii'))
assert node['schema'] == 1
assert node['srt_port'] == 8890
assert node['srt_path'] == 'iphone-live'
assert node['fallback_srt_hosts'] == []
PY
""",
    )
    set_smoke_stage("remote_lifecycle_result")
    probe = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "python3",
        "-c",
        native_result_probe_source(),
        max_capture_bytes=256,
    )
    failure = probe.stdout.decode("ascii", errors="replace").strip()
    if failure in NATIVE_RESULT_FAILURES:
        set_smoke_stage(failure)
    require(failure == "NATIVE_SELF_TEST_RESULT_OK", "native self-test result is invalid")
    set_smoke_stage("remote_lifecycle_accounts")
    lifecycle = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        """
set -eu
for spec in 'moblin-relay|/var/lib/moblin-relay' \
  'restream-agent|/var/lib/adojapan-relay-agent'; do
  name=${spec%%|*}
  home=${spec#*|}
  entry=$(getent passwd "$name")
  group=$(getent group "$name")
  test "$(printf '%s\n' "$entry" | cut -d: -f4)" = \
    "$(printf '%s\n' "$group" | cut -d: -f3)"
  test "$(printf '%s\n' "$entry" | cut -d: -f6)" = "$home"
  test "$(printf '%s\n' "$entry" | cut -d: -f7)" = /usr/sbin/nologin
  test -z "$(printf '%s\n' "$group" | cut -d: -f4)"
done
test -f /run/adojapan-ci-relay-agent/heartbeat.ok
test -z "$(find /tmp -maxdepth 1 -name 'adojapan-relay-bootstrap-*' -print -quit)"
test ! -e /opt/adojapan-restream-node/fake-docker-calls.log
printf 'REMOTE_NATIVE_LIFECYCLE_OK\n'
""",
    )
    require(
        lifecycle.stdout.strip() == b"REMOTE_NATIVE_LIFECYCLE_OK",
        "remote native relay lifecycle is invalid",
    )


def scan_credential_boundary(logs: bytes) -> None:
    probe = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "python3",
        "-c",
        """
import sys
from pathlib import Path

token_path = Path('/etc/adojapan-relay-agent/node.token')
token = token_path.read_bytes().strip()
assert token and b'\\n' not in token and b'\\r' not in token
for path in Path('/proc').iterdir():
    if not path.name.isdecimal():
        continue
    for name in ('cmdline', 'environ'):
        try:
            assert token not in (path / name).read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
for root in (
    Path('/etc/moblin-relay'), Path('/etc/adojapan-relay-agent'),
    Path('/opt/moblin-relay'),
    Path('/var/lib/moblin-relay'), Path('/usr/local/lib/adojapan-relay-agent'),
):
    for path in root.rglob('*'):
        if path != token_path and path.is_file() and not path.is_symlink():
            assert token not in path.read_bytes()
assert token not in sys.stdin.buffer.read()
print('CREDENTIAL_BOUNDARY_OK')
""",
        input_bytes=logs,
    )
    require(
        probe.stdout.strip() == b"CREDENTIAL_BOUNDARY_OK",
        "relay credential escaped its protected file",
    )


def scan_password_non_persistence() -> None:
    marker = PASSWORD_MARKER.encode("ascii")
    database_probe = compose(
        "exec",
        "-T",
        "backend",
        "python",
        "-c",
        (
            "import sys; from pathlib import Path; "
            "needle=sys.stdin.buffer.read(); root=Path('/srv/app/data'); "
            "found=any(needle in path.read_bytes() for path in root.iterdir() "
            "if path.is_file() and not path.is_symlink()); "
            "print('FOUND' if found else 'CLEAR')"
        ),
        input_bytes=marker,
    )
    require(database_probe.stdout.strip() == b"CLEAR", "password marker found in SQLite")

    remote_probe = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        (
            "if grep -R -q -F -f - /etc/moblin-relay /etc/adojapan-relay-agent "
            "/opt/moblin-relay /var/lib/moblin-relay "
            "/var/lib/adojapan-relay-agent /usr/local/lib/adojapan-relay-agent "
            "2>/dev/null; then printf 'FOUND\\n'; else printf 'CLEAR\\n'; fi"
        ),
        input_bytes=marker,
    )
    require(remote_probe.stdout.strip() == b"CLEAR", "password marker found in remote files")

    for location, result in (
        (
            "service logs",
            compose(
                "logs",
                "--no-color",
                "backend",
                "bootstrap",
                "ci-ssh-target",
                max_capture_bytes=MAX_RETAINED_LOG_BYTES,
            ),
        ),
        ("effective Compose model", compose("config")),
        ("remote process arguments", compose("exec", "-T", "ci-ssh-target", "ps", "-eo", "args=")),
    ):
        require_marker_absent(result.stdout + result.stderr, location)

    identifiers = compose("ps", "-a", "-q").stdout.splitlines()
    require(bool(identifiers), "CI project has no containers to inspect")
    for identifier in identifiers:
        decoded = identifier.decode("ascii", errors="strict")
        inspection = subprocess.run(  # noqa: S603 - fixed inspect and Compose-owned id
            (  # noqa: S607 - fixed executable and Compose-owned id
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Env}} {{json .Config.Cmd}} {{json .Path}} {{json .Args}}",
                decoded,
            ),
            capture_output=True,
            check=False,
            timeout=10,
        )
        if inspection.returncode != 0:
            raise SmokeFailure("container secret-boundary inspect failed")
        require_marker_absent(
            inspection.stdout + inspection.stderr,
            "container environment or process arguments",
        )


def require_relay_quiescent_after_revoke(client: APIClient, node_id: str) -> None:
    def probe() -> bool | None:
        result = compose(
            "exec",
            "-T",
            "ci-ssh-target",
            "test",
            "-f",
            "/run/adojapan-ci-relay-agent/quiescent.ok",
            expected=(0, 1),
        )
        return True if result.returncode == 0 else None

    wait_for("relay credential rejection", 15, probe)
    nodes = safe_api_payload(client.request("GET", "/api/nodes"), "revoked node list API")
    require(isinstance(nodes, dict), "revoked node list response is invalid")
    items = nodes.get("items")
    require(isinstance(items, list) and len(items) == 1, "revoked node list changed")
    require(items[0].get("id") == node_id, "revoked node identity changed")
    require(items[0].get("status") == "revoked", "relay node was not revoked")


def main() -> None:
    set_smoke_stage("host_fingerprint")
    expected_host_fingerprint = ci_host_fingerprint()
    client = APIClient()
    set_smoke_stage("login")
    client.login()
    request_payload: dict[str, Any] = {
        "address": "ci-ssh-target",
        "port": 22,
        "username": "ci-node",
        "password": PASSWORD_MARKER,
        "expected_host_fingerprint": None,
    }
    set_smoke_stage("create_job")
    try:
        accepted = safe_api_payload(
            client.request(
                "POST",
                "/api/nodes/bootstrap",
                request_payload,
                csrf=True,
                expected=(202,),
            ),
            "bootstrap create API",
        )
    finally:
        request_payload["password"] = ""
    require(isinstance(accepted, dict), "bootstrap acceptance response is invalid")
    require(accepted.get("install_profile") == "moblin_relay", "public API selected wrong profile")
    job_id = str(accepted.get("job_id", ""))
    require(bool(job_id) and accepted.get("state") == "queued", "bootstrap job was not queued")

    set_smoke_stage("bootstrap_job")
    completed = poll_job(client, job_id)
    print_self_test_progress(job_id)
    set_smoke_stage("relay_ready")
    try:
        node, _relay = ready_relay(client)
        node_id = str(node.get("id", ""))
        require(bool(node_id) and completed.get("node_id") == node_id, "node id changed")
        require(node.get("host_key_fingerprint") == expected_host_fingerprint, "host key changed")
        require(node.get("host_key_trust_mode") == "tofu", "TOFU was not persisted")
        require(bool(node.get("resolved_ip")), "resolved SSH target IP was not persisted")
    except SmokeFailure:
        print_relay_readiness_diagnostic(
            client,
            expected_node_id=str(completed.get("node_id", "")),
            expected_host_fingerprint=expected_host_fingerprint,
        )
        raise

    set_smoke_stage("remote_lifecycle")
    verify_remote_lifecycle()
    logs = compose(
        "logs",
        "--no-color",
        "backend",
        "bootstrap",
        "ci-ssh-target",
        max_capture_bytes=MAX_RETAINED_LOG_BYTES,
    )
    set_smoke_stage("credential_boundary")
    scan_credential_boundary(logs.stdout + logs.stderr)

    set_smoke_stage("revoke")
    revoked = safe_api_payload(
        client.request("POST", f"/api/nodes/{node_id}/revoke", {}, csrf=True),
        "node revoke API",
    )
    require(isinstance(revoked, dict) and revoked.get("status") == "revoked", "revoke failed")
    set_smoke_stage("revoke_quiescence")
    require_relay_quiescent_after_revoke(client, node_id)
    set_smoke_stage("password_non_persistence")
    scan_password_non_persistence()

    print("Public SSH bootstrap selected native moblin_relay and completed")
    print("Pinned MediaMTX, native media self-test, relay heartbeat, and revoke verified")
    print("SSH password and relay credential boundaries verified")


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure:
        print(f"Native relay onboarding smoke failed safely at stage: {_current_stage}")
        raise SystemExit(1) from None
