"""Secret-safe native Moblin relay onboarding smoke for the public API.

The disposable SSH target runs the real native installer, pinned MediaMTX,
FFmpeg slate generation, and the installer's quick media self-test. A tiny
CI-only protocol peer sends the first relay heartbeat because production relay
code intentionally refuses non-HTTPS/non-production control origins.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

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
        "relay_self_test_auth_live_failed",
        "relay_self_test_auth_scan_failed",
        "relay_self_test_auth_exclusivity_failed",
        "relay_self_test_live_ingest_failed",
        "relay_self_test_live_normalize_failed",
        "relay_self_test_normalizer_hook_failed",
        "relay_self_test_normalizer_child_failed",
        "relay_self_test_normalizer_publish_failed",
        "relay_self_test_normalizer_flap_failed",
        "relay_self_test_outages_failed",
        "relay_self_test_outage_slate_failed",
        "relay_self_test_outage_normal_failed",
        "relay_self_test_outage_hold_failed",
        "relay_self_test_outage_live_failed",
        "relay_self_test_continuity_failed",
        "relay_self_test_decode_failed",
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
        "relay_self_test_secrets_failed",
        "relay_self_test_cleanup_failed",
        "relay_slate_generation_failed",
        "remote_command_failed",
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
SMOKE_STAGES = frozenset(
    {
        "bootstrap_job",
        "create_job",
        "credential_boundary",
        "host_fingerprint",
        "login",
        "password_non_persistence",
        "relay_ready",
        "remote_lifecycle",
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


def verify_remote_lifecycle() -> None:
    lifecycle = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        """
set -eu
test "$(cat /etc/moblin-relay/.managed-by-adojapan)" = 'adojapan-moblin-relay:v1'
test "$(cat /etc/moblin-relay/release)" = '2026.09.04.1'
test "$(stat -c '%U:%G:%a' /etc/moblin-relay/secrets.json)" = 'root:root:600'
test "$(stat -c '%U:%G:%a' /etc/adojapan-relay-agent/node.token)" = \
  'restream-agent:restream-agent:600'
test "$(stat -c '%U:%G:%a' /etc/adojapan-relay-agent/preview-reader.token)" = \
  'restream-agent:restream-agent:600'
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
result = json.loads(
    Path('/var/lib/moblin-relay/tests/last-quick-result.json').read_text(encoding='utf-8')
)
assert result['status'] == 'PASS'
assert result['mode'] == 'quick'
assert result['outage_targets_seconds'] == [15, 17, 19]
assert result['same_session_stall']['srt_connection_preserved'] is True
assert result['same_session_stall']['normalizer_reconnected'] is True
assert result['supervisor_crash_recovery']['ffmpeg_parent_death_passed'] is True
assert result['supervisor_crash_recovery']['srt_connection_preserved'] is True
assert len(result['srt_idle_expiry_seconds']) == 3
assert all(8.0 <= value <= 13.0 for value in result['srt_idle_expiry_seconds'])
assert all(value <= 1.0 for value in result['outage_max_capture_no_growth_seconds'])
PY
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
