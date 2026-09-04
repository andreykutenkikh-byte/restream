"""Idempotent native Moblin relay bootstrap over an already verified SSH session.

The relay profile is intentionally separate from the legacy Docker Node Agent
installer.  It owns a small, marker-guarded set of paths, never changes the
firewall or networking stack, and receives the JIT relay credential only as a
mode-0600 SFTP payload.
"""

from __future__ import annotations

import json
import re
import shlex
import stat
from contextlib import suppress
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError, safe_failure
from bootstrap_worker.installer import PrivilegeContext
from bootstrap_worker.models import (
    BootstrapRequest,
    InstallOwnership,
    PackageManager,
    PlatformFamily,
    SystemFacts,
    TargetIdentity,
    TimeoutPolicy,
)
from bootstrap_worker.ssh import RemoteSession

RELAY_RELEASE = "2026.09.04.1"
RELAY_CONTROL_ORIGIN = "https://restream.adojapan.ru"
RELAY_MARKER_CONTENT = "adojapan-moblin-relay:v1"
RELAY_ETC_ROOT = "/etc/moblin-relay"
RELAY_MARKER = f"{RELAY_ETC_ROOT}/.managed-by-adojapan"
RELAY_NODE_ID = f"{RELAY_ETC_ROOT}/.node-id"
RELAY_RELEASE_FILE = f"{RELAY_ETC_ROOT}/release"
RELAY_NODE_CONFIG = f"{RELAY_ETC_ROOT}/node.json"
RELAY_SECRET_FILE = f"{RELAY_ETC_ROOT}/secrets.json"
RELAY_OPT_ROOT = "/opt/moblin-relay"
RELAY_STATE_ROOT = "/var/lib/moblin-relay"
AGENT_ETC_ROOT = "/etc/adojapan-relay-agent"
AGENT_STATE_ROOT = "/var/lib/adojapan-relay-agent"
AGENT_TOKEN_PATH = f"{AGENT_ETC_ROOT}/node.token"
MEDIA_MTX_VERSION = "v1.20.1"
MEDIA_MTX_ARCHIVE = "mediamtx_v1.20.1_linux_amd64.tar.gz"
MEDIA_MTX_URL = (
    "https://github.com/bluenviron/mediamtx/releases/download/"
    f"{MEDIA_MTX_VERSION}/{MEDIA_MTX_ARCHIVE}"
)
MEDIA_MTX_ARCHIVE_SHA256 = "81b143f55a5d23d4a8c028d52869c14ea4a59919900528698fcc97a747fd69c6"

_CORE_PAYLOADS = (
    "initialize-secrets",
    "moblin-relay-normalize",
    "moblin-relay-render-config",
    "moblin-relay.service",
    "relayctl",
    "self-test",
    "slate.txt",
)
_AGENT_DEPLOY_PAYLOADS = (
    "adojapan-relay-agent.service",
    "adojapan-relay-agent.sysusers",
    "adojapan-relay-agent.tmpfiles",
    "adojapan-relay-broker.service",
    "adojapan-relay-broker.socket",
    "agent-entry.py",
    "broker-entry.py",
    "install-preview-token.py",
    "install-token.py",
    "install.sh",
    "journal-rollback.py",
)
_AGENT_MODULE_PAYLOADS = (
    "__init__.py",
    "__main__.py",
    "broker_client.py",
    "broker.py",
    "client.py",
    "errors.py",
    "journal.py",
    "metrics.py",
    "models.py",
    "preview.py",
    "processor.py",
    "security.py",
    "service.py",
)
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_RELAY_TOKEN = re.compile(rb"[A-Za-z0-9._~+/=-]{32,512}\Z")
_REMOTE_TEMP_PREFIX = "/tmp/adojapan-relay-bootstrap-"  # noqa: S108
_AGENT_INSTALL_STAGE_CODES = {
    "preflight": "relay_agent_preflight_failed",
    "accounts": "relay_agent_accounts_failed",
    "sysusers": "relay_agent_sysusers_failed",
    "tmpfiles": "relay_agent_tmpfiles_failed",
    "journal": "relay_agent_journal_failed",
    "copy": "relay_agent_copy_failed",
    "units": "relay_agent_units_failed",
    "broker": "relay_agent_broker_failed",
}
_SELF_TEST_STAGE_CODES = {
    "startup": "relay_self_test_startup_failed",
    "assets": "relay_self_test_assets_failed",
    "topology": "relay_self_test_topology_failed",
    "auth": "relay_self_test_auth_failed",
    "auth-source": "relay_self_test_auth_source_failed",
    "auth-src-help": "relay_self_test_auth_source_helper_failed",
    "auth-src-bind": "relay_self_test_auth_source_publisher_bind_failed",
    "auth-src-feed": "relay_self_test_auth_source_feeder_failed",
    "auth-src-path": "relay_self_test_auth_source_path_failed",
    "auth-live": "relay_self_test_auth_live_failed",
    "auth-scan": "relay_self_test_auth_scan_failed",
    "auth-exclusive": "relay_self_test_auth_exclusivity_failed",
    "auth-x-core": "relay_self_test_auth_exclusivity_core_failed",
    "auth-x-second": "relay_self_test_auth_exclusivity_candidate_failed",
    "auth-x-primary": "relay_self_test_auth_exclusivity_primary_failed",
    "auth-x-live": "relay_self_test_auth_exclusivity_live_failed",
    "auth-x-ingest": "relay_self_test_auth_exclusivity_ingest_failed",
    "auth-x-norm": "relay_self_test_auth_exclusivity_normalizer_failed",
    "auth-x-sink": "relay_self_test_auth_exclusivity_downstream_failed",
    "auth-x-bytes": "relay_self_test_auth_exclusivity_progress_failed",
    "auth-x-blind": "relay_self_test_auth_exclusivity_observability_failed",
    "auth-x-attempt": "relay_self_test_auth_exclusivity_proof_failed",
    "live-ingest": "relay_self_test_live_ingest_failed",
    "live-normalize": "relay_self_test_live_normalize_failed",
    "norm-hook": "relay_self_test_normalizer_hook_failed",
    "norm-child": "relay_self_test_normalizer_child_failed",
    "norm-publish": "relay_self_test_normalizer_publish_failed",
    "norm-flap": "relay_self_test_normalizer_flap_failed",
    "stall-slate": "relay_self_test_stall_slate_failed",
    "stall-pre": "relay_self_test_stall_precondition_failed",
    "stall-pause": "relay_self_test_stall_pause_failed",
    "stall-switch": "relay_self_test_stall_switch_failed",
    "stall-capture": "relay_self_test_stall_capture_failed",
    "stall-resume": "relay_self_test_stall_resume_failed",
    "stall-live": "relay_self_test_stall_live_failed",
    "stall-core": "relay_self_test_stall_core_failed",
    "stall-source": "relay_self_test_stall_source_failed",
    "stall-ingest": "relay_self_test_stall_ingest_failed",
    "stall-i-off": "relay_self_test_stall_ingest_offline_failed",
    "stall-i-id": "relay_self_test_stall_ingest_identity_failed",
    "stall-id-pre": "relay_self_test_stall_ingest_identity_pre_resume_failed",
    "stall-id-rec": "relay_self_test_stall_ingest_identity_recovery_failed",
    "stall-i-byte": "relay_self_test_stall_ingest_progress_failed",
    "stall-h-blind": "relay_self_test_stall_helper_observability_failed",
    "stall-h-path": "relay_self_test_stall_helper_path_failed",
    "stall-h-error": "relay_self_test_stall_helper_forward_failed",
    "stall-h-state": "relay_self_test_stall_helper_state_failed",
    "stall-norm": "relay_self_test_stall_normalizer_failed",
    "stall-sink": "relay_self_test_stall_downstream_failed",
    "stall-blind": "relay_self_test_stall_observability_failed",
    "stall-ident": "relay_self_test_stall_identity_failed",
    "stall-cont": "relay_self_test_stall_continuity_failed",
    "crash-death": "relay_self_test_crash_death_failed",
    "crash-live": "relay_self_test_crash_live_failed",
    "crash-cont": "relay_self_test_crash_continuity_failed",
    "outages": "relay_self_test_outages_failed",
    "outage-slate": "relay_self_test_outage_slate_failed",
    "outage-normal": "relay_self_test_outage_normal_failed",
    "outage-hold": "relay_self_test_outage_hold_failed",
    "outage-live": "relay_self_test_outage_live_failed",
    "continuity": "relay_self_test_continuity_failed",
    "decode": "relay_self_test_decode_failed",
    "streams": "relay_self_test_decode_streams_failed",
    "format": "relay_self_test_decode_format_failed",
    "gop": "relay_self_test_decode_gop_failed",
    "decoder": "relay_self_test_decode_decoder_failed",
    "frames": "relay_self_test_decode_frames_failed",
    "timestamps": "relay_self_test_decode_timestamps_failed",
    "ts-probe-pts": "relay_self_test_timestamp_probe_pts_failed",
    "ts-packet-dts": "relay_self_test_timestamp_packet_dts_failed",
    "ts-video-pts": "relay_self_test_timestamp_video_pts_failed",
    "ts-audio-pts": "relay_self_test_timestamp_audio_pts_failed",
    "ts-gaps": "relay_self_test_timestamp_gaps_failed",
    "ts-av-sync": "relay_self_test_timestamp_av_sync_failed",
    "secrets": "relay_self_test_secrets_failed",
    "cleanup": "relay_self_test_cleanup_failed",
}


@dataclass(slots=True)
class RelayInstallReceipt:
    temp_root: str
    job_id: UUID
    ownership: InstallOwnership
    docker_installed: bool
    node_id: UUID
    public_srt_host: str
    managed_scope_acquired: bool = False
    files_applied: bool = False
    agent_start_attempted: bool = False
    enrollment_token_applied: bool = False
    enrollment_completed: bool = False
    workflow_committed: bool = False
    rollback_succeeded: bool = False


def validate_relay_platform(facts: SystemFacts) -> None:
    """Keep the native media profile on the package matrix tested in CI."""

    supported = {
        ("ubuntu", "22.04"),
        ("ubuntu", "24.04"),
        ("debian", "12"),
        ("debian", "13"),
    }
    if (
        facts.platform_family is not PlatformFamily.DEBIAN
        or facts.package_manager is not PackageManager.APT
        or facts.architecture != "amd64"
        or (facts.os_id, facts.os_version) not in supported
    ):
        raise safe_failure("unsupported_relay_operating_system")


def _system_account_validation_command() -> str:
    return (
        "command -v getent >/dev/null 2>&1 && command -v cut >/dev/null 2>&1 && "
        "for spec in "
        "'moblin-relay|/var/lib/moblin-relay' "
        "'restream-agent|/var/lib/adojapan-relay-agent'; do "
        "name=${spec%%|*}; expected_home=${spec#*|}; "
        'passwd_entry=$(getent passwd "$name" || true); '
        'group_entry=$(getent group "$name" || true); '
        'if test -z "$passwd_entry" && test -z "$group_entry"; then continue; fi; '
        'test -n "$passwd_entry" && test -n "$group_entry" || exit 1; '
        "passwd_name=$(printf '%s\\n' \"$passwd_entry\" | cut -d: -f1); "
        "account_uid=$(printf '%s\\n' \"$passwd_entry\" | cut -d: -f3); "
        "account_gid=$(printf '%s\\n' \"$passwd_entry\" | cut -d: -f4); "
        "account_home=$(printf '%s\\n' \"$passwd_entry\" | cut -d: -f6); "
        "account_shell=$(printf '%s\\n' \"$passwd_entry\" | cut -d: -f7); "
        "group_name=$(printf '%s\\n' \"$group_entry\" | cut -d: -f1); "
        "group_gid=$(printf '%s\\n' \"$group_entry\" | cut -d: -f3); "
        "group_members=$(printf '%s\\n' \"$group_entry\" | cut -d: -f4); "
        'case "$account_uid:$account_gid:$group_gid" in *[!0-9:]*) exit 1;; esac; '
        'test "$passwd_name" = "$name" && test "$group_name" = "$name" && '
        'test "$account_uid" -gt 0 && test "$account_uid" -le 999 && '
        'test "$account_gid" = "$group_gid" && test "$group_gid" -gt 0 && '
        'test "$group_gid" -le 999 && test "$account_home" = "$expected_home" && '
        'test "$account_shell" = /usr/sbin/nologin && '
        'test -z "$group_members" || exit 1; done'
    )


def _regular_payload(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OSError("payload is not a regular file")
        payload = path.read_bytes()
    except OSError as exc:
        raise safe_failure("relay_bundle_invalid") from exc
    if not payload or len(payload) > _MAX_BUNDLE_BYTES:
        raise safe_failure("relay_bundle_invalid")
    return payload


def load_relay_bundle(root: Path) -> dict[PurePosixPath, bytes]:
    """Load only the reviewed source allowlist shipped in the bootstrap image."""

    payloads: dict[PurePosixPath, bytes] = {}
    total = 0
    for name in _CORE_PAYLOADS:
        source = root / "deploy" / "moblin-relay" / name
        payloads[PurePosixPath("deploy/moblin-relay") / name] = _regular_payload(source)
    for name in _AGENT_DEPLOY_PAYLOADS:
        source = root / "deploy" / "hk-relay-agent" / name
        payloads[PurePosixPath("deploy/hk-relay-agent") / name] = _regular_payload(source)
    agent_root = root / "relay_agent"
    for name in _AGENT_MODULE_PAYLOADS:
        source = agent_root / name
        payloads[PurePosixPath("relay_agent") / name] = _regular_payload(source)
    total = sum(len(payload) for payload in payloads.values())
    if total > _MAX_BUNDLE_BYTES:
        raise safe_failure("relay_bundle_invalid")
    return payloads


class RemoteRelayInstaller:
    """Install and validate one native relay without touching host networking."""

    def __init__(self, bundle_root: Path | None = None) -> None:
        self._bundle_root = bundle_root or Path(__file__).resolve().parents[1]

    async def _ownership(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        request: BootstrapRequest,
        *,
        timeout: float,
    ) -> InstallOwnership:
        marker = shlex.quote(RELAY_MARKER)
        node_id = shlex.quote(RELAY_NODE_ID)
        etc_root = shlex.quote(RELAY_ETC_ROOT)
        expected_node = shlex.quote(str(request.node_id))
        command = (
            f"if test ! -e {etc_root} && test ! -L {etc_root}; then printf 'absent\\n'; "
            f"elif test -d {etc_root} && test ! -L {etc_root} && "
            f"test -f {marker} && test ! -L {marker} && "
            f'test "$(cat {marker})" = {shlex.quote(RELAY_MARKER_CONTENT)} && '
            f"test -f {node_id} && test ! -L {node_id} && "
            f"test \"$(cat {node_id})\" = {expected_node}; then printf 'managed\\n'; "
            "else printf 'conflict\\n'; fi"
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("remote_command_failed")
        try:
            return InstallOwnership(result.stdout.strip())
        except ValueError as exc:
            raise safe_failure("remote_command_failed") from exc

    @staticmethod
    async def _assert_fresh_paths(
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        paths = (
            RELAY_OPT_ROOT,
            RELAY_STATE_ROOT,
            AGENT_ETC_ROOT,
            AGENT_STATE_ROOT,
            "/usr/local/lib/adojapan-relay-agent",
            "/usr/local/sbin/relayctl",
            "/usr/local/sbin/adojapan-relay-install-token",
            "/usr/local/sbin/adojapan-relay-install-preview-token",
            "/usr/local/sbin/adojapan-relay-restore-v1-journal",
            "/usr/local/libexec/moblin-relay-render-config",
            "/etc/sysusers.d/adojapan-relay-agent.conf",
            "/etc/tmpfiles.d/adojapan-relay-agent.conf",
            "/etc/systemd/system/moblin-relay.service",
            "/etc/systemd/system/adojapan-relay-agent.service",
            "/etc/systemd/system/adojapan-relay-broker.service",
            "/etc/systemd/system/adojapan-relay-broker.socket",
        )
        checks = " && ".join(
            f"test ! -e {shlex.quote(path)} && test ! -L {shlex.quote(path)}" for path in paths
        )
        result = await privilege.run(session, checks, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("remote_relay_conflict")

    @staticmethod
    async def _assert_managed_paths_safe(
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        directories = (
            RELAY_OPT_ROOT,
            f"{RELAY_OPT_ROOT}/bin",
            f"{RELAY_OPT_ROOT}/libexec",
            RELAY_STATE_ROOT,
            AGENT_ETC_ROOT,
            AGENT_STATE_ROOT,
            "/usr/local/lib/adojapan-relay-agent",
        )
        files = (
            RELAY_RELEASE_FILE,
            RELAY_NODE_CONFIG,
            RELAY_SECRET_FILE,
            f"{RELAY_ETC_ROOT}/slate.txt",
            f"{RELAY_OPT_ROOT}/bin/mediamtx",
            f"{RELAY_OPT_ROOT}/LICENSE",
            f"{RELAY_OPT_ROOT}/libexec/moblin-relay-normalize",
            f"{RELAY_OPT_ROOT}/libexec/self-test",
            f"{RELAY_OPT_ROOT}/libexec/initialize-secrets",
            f"{RELAY_STATE_ROOT}/slate.mp4",
            AGENT_TOKEN_PATH,
            f"{AGENT_ETC_ROOT}/preview-reader.token",
            "/usr/local/sbin/relayctl",
            "/usr/local/sbin/adojapan-relay-install-token",
            "/usr/local/sbin/adojapan-relay-install-preview-token",
            "/usr/local/sbin/adojapan-relay-restore-v1-journal",
            "/usr/local/libexec/moblin-relay-render-config",
            "/etc/sysusers.d/adojapan-relay-agent.conf",
            "/etc/tmpfiles.d/adojapan-relay-agent.conf",
            "/etc/systemd/system/moblin-relay.service",
            "/etc/systemd/system/adojapan-relay-agent.service",
            "/etc/systemd/system/adojapan-relay-broker.service",
            "/etc/systemd/system/adojapan-relay-broker.socket",
        )
        checks = [
            f"if test -e {shlex.quote(path)} || test -L {shlex.quote(path)}; then "
            f"test -d {shlex.quote(path)} && test ! -L {shlex.quote(path)} || exit 1; fi"
            for path in directories
        ]
        checks.extend(
            f"if test -e {shlex.quote(path)} || test -L {shlex.quote(path)}; then "
            f"test -f {shlex.quote(path)} && test ! -L {shlex.quote(path)} || exit 1; fi"
            for path in files
        )
        result = await privilege.run(session, "; ".join(checks), timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("remote_relay_conflict")

    @staticmethod
    async def _assert_relay_stopped(
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        command = (
            "! systemctl is-active --quiet moblin-relay.service 2>/dev/null && "
            "! systemctl is-enabled --quiet moblin-relay.service 2>/dev/null"
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("relay_active_during_install")

    @staticmethod
    async def _assert_system_accounts_safe(
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        # systemd-sysusers intentionally preserves existing accounts.  Fail
        # closed rather than silently adopting an interactive/foreign account
        # which would inherit access to relay runtime secrets or the broker.
        command = _system_account_validation_command()
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("remote_relay_account_conflict")

    @staticmethod
    async def _assert_ports_free(
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        # No firewall commands are used here.  This is only a local collision
        # check for the one public UDP listener and four loopback listeners.
        # Read the kernel socket tables directly so this check also works on a
        # minimal image before iproute2/ss has been installed.  Any socket bound
        # to one of the fixed relay ports is treated conservatively as a clash.
        command = (
            "for port in 22BA 216A 078F 22B8 270E; do "
            'if awk -v wanted="$port" \'NR > 1 { split($2, local, ":"); '
            "if (toupper(local[2]) == wanted) { found=1; exit } } "
            "END { exit(found ? 0 : 1) }' "
            "/proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; "
            "then exit 1; fi; done"
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("relay_port_conflict")

    async def prepare(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        request: BootstrapRequest,
        facts: SystemFacts,
        *,
        enrollment_token: SecretStr,
        job_id: UUID,
        docker_installed: bool,
        timeouts: TimeoutPolicy,
        target: TargetIdentity | None = None,
    ) -> RelayInstallReceipt:
        del docker_installed
        validate_relay_platform(facts)
        if request.control_url != RELAY_CONTROL_ORIGIN:
            raise safe_failure("invalid_relay_control_origin")
        if target is None:
            raise safe_failure("invalid_relay_target")
        try:
            public_ip = ip_address(target.resolved_ip)
        except ValueError as exc:
            raise safe_failure("invalid_relay_target") from exc
        if public_ip.version != 4:
            raise safe_failure("invalid_relay_target")

        try:
            token_bytes = enrollment_token.get_secret_value().encode("ascii")
        except UnicodeEncodeError as exc:
            raise safe_failure("invalid_enrollment_token") from exc
        if _RELAY_TOKEN.fullmatch(token_bytes) is None:
            token_bytes = b""
            raise safe_failure("invalid_enrollment_token")

        payloads = load_relay_bundle(self._bundle_root)
        ownership = await self._ownership(
            session,
            privilege,
            request,
            timeout=timeouts.command_seconds,
        )
        if ownership is InstallOwnership.CONFLICT:
            raise safe_failure("remote_relay_conflict")
        await self._assert_relay_stopped(
            session,
            privilege,
            timeout=timeouts.command_seconds,
        )
        if ownership is InstallOwnership.ABSENT:
            await self._assert_fresh_paths(
                session,
                privilege,
                timeout=timeouts.command_seconds,
            )
        else:
            await self._assert_managed_paths_safe(
                session,
                privilege,
                timeout=timeouts.command_seconds,
            )
        await self._assert_system_accounts_safe(
            session,
            privilege,
            timeout=timeouts.command_seconds,
        )
        await self._assert_ports_free(
            session,
            privilege,
            timeout=timeouts.command_seconds,
        )

        temp_root = f"{_REMOTE_TEMP_PREFIX}{job_id}"
        quoted_temp = shlex.quote(temp_root)
        created = await session.run(
            f"test ! -e {quoted_temp} && test ! -L {quoted_temp} && "
            f"install -d -m 0700 -- {quoted_temp} && "
            f"install -d -m 0700 -- {quoted_temp}/repo {quoted_temp}/repo/deploy "
            f"{quoted_temp}/repo/deploy/moblin-relay "
            f"{quoted_temp}/repo/deploy/hk-relay-agent {quoted_temp}/repo/relay_agent",
            timeout=timeouts.command_seconds,
        )
        if created.exit_status != 0:
            raise safe_failure("remote_command_failed")

        receipt = RelayInstallReceipt(
            temp_root=temp_root,
            job_id=job_id,
            ownership=ownership,
            docker_installed=False,
            node_id=request.node_id,
            public_srt_host=str(public_ip),
        )
        try:
            generated = {
                PurePosixPath("managed-marker"): f"{RELAY_MARKER_CONTENT}\n".encode(),
                PurePosixPath("node-id"): f"{request.node_id}\n".encode(),
                PurePosixPath("release"): f"{RELAY_RELEASE}\n".encode(),
                PurePosixPath("node.json"): (
                    json.dumps(
                        {
                            "schema": 1,
                            "public_srt_host": str(public_ip),
                            "fallback_srt_hosts": [],
                            "srt_port": 8890,
                            "srt_path": "iphone-live",
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("ascii"),
                PurePosixPath("node.token"): token_bytes + b"\n",
                PurePosixPath("moblin-relay.sysusers"): (
                    b'u moblin-relay - "Moblin native relay" '
                    b"/var/lib/moblin-relay /usr/sbin/nologin\n"
                ),
            }
            for relative, payload in {**payloads, **generated}.items():
                destination = (
                    f"{temp_root}/repo/{relative}"
                    if relative in payloads
                    else (f"{temp_root}/{relative}")
                )
                await session.put(
                    destination,
                    payload,
                    mode=0o600,
                    timeout=timeouts.command_seconds,
                )
        except BaseException:
            await self.cleanup_temp(
                session,
                privilege,
                receipt,
                timeout=timeouts.command_seconds,
            )
            raise
        finally:
            token_bytes = b""
        return receipt

    async def _run_checked(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        command: str,
        *,
        timeout: float,
        code: str,
    ) -> None:
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure(code)

    async def install(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: RelayInstallReceipt,
        *,
        timeouts: TimeoutPolicy,
    ) -> None:
        temp = shlex.quote(receipt.temp_root)
        repo = f"{temp}/repo"
        await self._run_checked(
            session,
            privilege,
            "export DEBIAN_FRONTEND=noninteractive; apt-get -qq update && "
            "apt-get -qq install -y --no-install-recommends "
            "ca-certificates curl ffmpeg fonts-dejavu-core iproute2 python3",
            timeout=timeouts.package_seconds,
            code="relay_dependency_install_failed",
        )
        await self._run_checked(
            session,
            privilege,
            "command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null && "
            "command -v curl >/dev/null && command -v systemd-analyze >/dev/null && "
            "ffmpeg -hide_banner -encoders 2>/dev/null | "
            "grep -q '[[:space:]]libx264[[:space:]]' && "
            "ffmpeg -hide_banner -protocols 2>/dev/null | "
            "grep -Eq '^[[:space:]]+srt$'",
            timeout=timeouts.command_seconds,
            code="relay_dependency_check_failed",
        )

        media_url = shlex.quote(MEDIA_MTX_URL)
        archive_hash = shlex.quote(MEDIA_MTX_ARCHIVE_SHA256)
        await self._run_checked(
            session,
            privilege,
            f"rm -f -- {temp}/mediamtx.tar.gz && rm -rf -- {temp}/mediamtx && "
            f"install -d -m 0700 -- {temp}/mediamtx && "
            "curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 "
            "--connect-timeout 15 --max-time 180 --retry 4 --retry-all-errors "
            "--retry-delay 2 --retry-max-time 240 "
            f"{media_url} --output {temp}/mediamtx.tar.gz",
            timeout=timeouts.package_seconds,
            code="mediamtx_download_failed",
        )
        await self._run_checked(
            session,
            privilege,
            f"printf '%s  %s\\n' {archive_hash} {temp}/mediamtx.tar.gz | "
            "sha256sum --check --status",
            timeout=timeouts.command_seconds,
            code="mediamtx_checksum_failed",
        )
        await self._run_checked(
            session,
            privilege,
            f"tar -xzf {temp}/mediamtx.tar.gz -C {temp}/mediamtx mediamtx LICENSE",
            timeout=timeouts.command_seconds,
            code="mediamtx_archive_invalid",
        )
        await self._run_checked(
            session,
            privilege,
            f"test -f {temp}/mediamtx/LICENSE",
            timeout=timeouts.command_seconds,
            code="mediamtx_license_missing",
        )
        await self._run_checked(
            session,
            privilege,
            f"test -f {temp}/mediamtx/mediamtx && "
            f"test ! -L {temp}/mediamtx/mediamtx && "
            f"test \"$(stat -c '%a' {temp}/mediamtx/mediamtx)\" = 755",
            timeout=timeouts.command_seconds,
            code="mediamtx_binary_invalid",
        )

        # Generate the immutable 12-second fallback before claiming any managed
        # path.  This keeps an encoder/package failure fully pre-mutation.
        drawtext = shlex.quote(
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"textfile={receipt.temp_root}/repo/deploy/moblin-relay/slate.txt:"
            "fontcolor=white:fontsize=72:line_spacing=28:"
            "x=(w-text_w)/2:y=(h-text_h)/2"
        )
        await self._run_checked(
            session,
            privilege,
            "ffmpeg -nostdin -hide_banner -loglevel error -y "
            "-f lavfi -i color=c=0x111827:s=1080x1920:r=30:d=12 "
            "-f lavfi -i anullsrc=r=48000:cl=stereo "
            f"-vf {drawtext} "
            "-map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -profile:v main "
            "-level:v 4.0 -pix_fmt yuv420p -r 30 -g 60 -keyint_min 60 -sc_threshold 0 "
            "-b:v 8M -minrate 8M -maxrate 8M -bufsize 16M "
            "-x264-params nal-hrd=cbr:force-cfr=1:filler=1 "
            "-c:a aac -profile:a aac_low "
            "-ar 48000 -ac 2 -b:a 128k -t 12 -shortest -movflags +faststart "
            f"{temp}/slate.mp4",
            timeout=timeouts.package_seconds,
            code="relay_slate_generation_failed",
        )

        if receipt.ownership is InstallOwnership.ABSENT:
            claim = shlex.quote(f"/etc/.moblin-relay.claim-{receipt.node_id}")
            await self._run_checked(
                session,
                privilege,
                f"if test -e {claim} || test -L {claim}; then "
                f"if test -d {claim} && test ! -L {claim} && "
                f"test -f {claim}/.managed-by-adojapan && "
                f"test ! -L {claim}/.managed-by-adojapan && "
                f'test "$(cat {claim}/.managed-by-adojapan)" = '
                f"{shlex.quote(RELAY_MARKER_CONTENT)} && "
                f"test -f {claim}/.node-id && test ! -L {claim}/.node-id && "
                f'test "$(cat {claim}/.node-id)" = {shlex.quote(str(receipt.node_id))}; '
                f"then rm -rf -- {claim}; else exit 1; fi; fi; "
                f"if test ! -e {shlex.quote(RELAY_ETC_ROOT)} && "
                f"test ! -L {shlex.quote(RELAY_ETC_ROOT)} && "
                f"test ! -e {claim} && test ! -L {claim} && "
                f"install -d -m 0700 -- {claim}; then :; else exit 1; fi; "
                "claim_status=0; "
                f"install -m 0644 {temp}/managed-marker {claim}/.managed-by-adojapan && "
                f"install -m 0644 {temp}/node-id {claim}/.node-id && "
                f"mv -Tn -- {claim} {shlex.quote(RELAY_ETC_ROOT)} || claim_status=$?; "
                'if test "$claim_status" -ne 0; then '
                f'rm -rf -- {claim}; exit "$claim_status"; fi',
                timeout=timeouts.command_seconds,
                code="remote_relay_conflict",
            )
            receipt.managed_scope_acquired = True

        guard = (
            f"test -d {shlex.quote(RELAY_ETC_ROOT)} && "
            f"test ! -L {shlex.quote(RELAY_ETC_ROOT)} && "
            f"test -f {shlex.quote(RELAY_MARKER)} && "
            f'test "$(cat {shlex.quote(RELAY_MARKER)})" = '
            f"{shlex.quote(RELAY_MARKER_CONTENT)} && "
            f"test -f {shlex.quote(RELAY_NODE_ID)} && "
            f'test "$(cat {shlex.quote(RELAY_NODE_ID)})" = '
            f"{shlex.quote(str(receipt.node_id))}"
        )
        await self._run_checked(
            session,
            privilege,
            f"{guard} && systemd-sysusers {temp}/moblin-relay.sysusers && "
            f"{_system_account_validation_command()} && "
            f"install -d -o root -g root -m 0755 {shlex.quote(RELAY_OPT_ROOT)} "
            f"{shlex.quote(RELAY_OPT_ROOT + '/bin')} "
            f"{shlex.quote(RELAY_OPT_ROOT + '/libexec')} && "
            "install -d -o moblin-relay -g moblin-relay -m 0750 "
            f"{shlex.quote(RELAY_STATE_ROOT)} && "
            "install -d -o root -g root -m 0755 /usr/local/libexec && "
            f"install -o root -g root -m 0755 {temp}/mediamtx/mediamtx "
            f"{shlex.quote(RELAY_OPT_ROOT + '/bin/mediamtx')} && "
            f"install -o root -g root -m 0644 {temp}/mediamtx/LICENSE "
            f"{shlex.quote(RELAY_OPT_ROOT + '/LICENSE')} && "
            f"install -o root -g root -m 0755 {repo}/deploy/moblin-relay/moblin-relay-normalize "
            f"{shlex.quote(RELAY_OPT_ROOT + '/libexec/moblin-relay-normalize')} && "
            f"install -o root -g root -m 0755 {repo}/deploy/moblin-relay/self-test "
            f"{shlex.quote(RELAY_OPT_ROOT + '/libexec/self-test')} && "
            f"install -o root -g root -m 0755 {repo}/deploy/moblin-relay/initialize-secrets "
            f"{shlex.quote(RELAY_OPT_ROOT + '/libexec/initialize-secrets')} && "
            f"install -o root -g root -m 0755 {repo}/deploy/moblin-relay/relayctl "
            "/usr/local/sbin/relayctl && "
            "install -o root -g root -m 0755 "
            f"{repo}/deploy/moblin-relay/moblin-relay-render-config "
            "/usr/local/libexec/moblin-relay-render-config && "
            f"install -o root -g root -m 0644 {repo}/deploy/moblin-relay/slate.txt "
            f"{shlex.quote(RELAY_ETC_ROOT + '/slate.txt')} && "
            f"install -o root -g root -m 0600 {temp}/node.json {shlex.quote(RELAY_NODE_CONFIG)} && "
            f"install -o root -g root -m 0644 {temp}/slate.mp4 "
            f"{shlex.quote(RELAY_STATE_ROOT + '/slate.mp4')} && "
            f"install -o root -g root -m 0644 {repo}/deploy/moblin-relay/moblin-relay.service "
            "/etc/systemd/system/moblin-relay.service && "
            f"if test -e {shlex.quote(RELAY_SECRET_FILE)} || "
            f"test -L {shlex.quote(RELAY_SECRET_FILE)}; "
            f"then test -f {shlex.quote(RELAY_SECRET_FILE)} && "
            f"test ! -L {shlex.quote(RELAY_SECRET_FILE)} && "
            f"test \"$(stat -c '%u:%a' {shlex.quote(RELAY_SECRET_FILE)})\" = '0:600'; "
            f"else {shlex.quote(RELAY_OPT_ROOT + '/libexec/initialize-secrets')}; fi",
            timeout=timeouts.package_seconds,
            code="relay_install_failed",
        )
        receipt.files_applied = True

        # The existing reviewed agent installer performs its own source/copy
        # manifest verification and preserves relay enabled/active state.
        receipt.agent_start_attempted = True
        agent_stage = shlex.quote(f"/run/adojapan-relay-install.{receipt.job_id}.stage")
        agent_install = await privilege.run(
            session,
            "systemctl stop adojapan-relay-agent.service 2>/dev/null || true; "
            "systemctl stop adojapan-relay-broker.service 2>/dev/null || true; "
            "agent_status=0; "
            f"rm -f -- {agent_stage} && cd {repo} && "
            f"ADOJAPAN_RELAY_INSTALL_STAGE_FILE={agent_stage} "
            "sh deploy/hk-relay-agent/install.sh || agent_status=$?; "
            'if test "$agent_status" -eq 0; then '
            f"rm -f -- {agent_stage}; fi; "
            'exit "$agent_status"',
            timeout=timeouts.package_seconds,
        )
        if agent_install.exit_status != 0:
            diagnostic = await privilege.run(
                session,
                "stage_value=; "
                f"if test -f {agent_stage} && test ! -L {agent_stage} && "
                f"test \"$(stat -c '%u:%a' {agent_stage})\" = '0:600' && "
                f'test "$(wc -c < {agent_stage})" -le 16; then '
                f"stage_value=$(cat {agent_stage}); fi; "
                f"rm -f -- {agent_stage}; "
                "printf '%s' \"$stage_value\"",
                timeout=timeouts.command_seconds,
            )
            stage_code = _AGENT_INSTALL_STAGE_CODES.get(diagnostic.stdout.strip())
            raise safe_failure(stage_code or "relay_agent_install_failed")
        await self._run_checked(
            session,
            privilege,
            f"if test ! -e {shlex.quote(AGENT_ETC_ROOT + '/preview-reader.token')}; then "
            "/usr/local/sbin/adojapan-relay-install-preview-token --generate; fi && "
            "systemctl daemon-reload && systemctl disable --now moblin-relay.service",
            timeout=timeouts.package_seconds,
            code="relay_self_test_startup_failed",
        )
        await self._run_checked(
            session,
            privilege,
            "systemd-analyze verify /etc/systemd/system/moblin-relay.service "
            "/etc/systemd/system/adojapan-relay-agent.service "
            "/etc/systemd/system/adojapan-relay-broker.service "
            "/etc/systemd/system/adojapan-relay-broker.socket",
            timeout=timeouts.package_seconds,
            code="relay_unit_verify_failed",
        )
        self_test_stage = shlex.quote(f"/run/moblin-relay-self-test.{receipt.job_id}.stage")
        self_test = await privilege.run(
            session,
            "self_test_status=0; "
            f"rm -f -- {self_test_stage} || exit $?; "
            f"MOBLIN_RELAY_SELF_TEST_STAGE_FILE={self_test_stage} "
            f"{shlex.quote(RELAY_OPT_ROOT + '/libexec/self-test')} "
            "--quick >/dev/null 2>&1 || self_test_status=$?; "
            'if test "$self_test_status" -eq 0; then '
            f"rm -f -- {self_test_stage}; fi; "
            'exit "$self_test_status"',
            timeout=timeouts.package_seconds,
        )
        if self_test.exit_status != 0:
            diagnostic = await privilege.run(
                session,
                "self_test_stage_value=; "
                f"if test -f {self_test_stage} && test ! -L {self_test_stage} && "
                f"test \"$(stat -c '%u:%a:%h' {self_test_stage})\" = '0:600:1' && "
                f'test "$(wc -c < {self_test_stage})" -le 16; then '
                f"self_test_stage_value=$(cat {self_test_stage}); fi; "
                f"rm -f -- {self_test_stage}; "
                "printf '%s' \"$self_test_stage_value\"",
                timeout=timeouts.command_seconds,
            )
            stage_code = _SELF_TEST_STAGE_CODES.get(diagnostic.stdout.strip())
            raise safe_failure(stage_code or "relay_self_test_failed")

        token_temp = shlex.quote(f"{AGENT_ETC_ROOT}/.node.token.{receipt.job_id}.tmp")
        await self._run_checked(
            session,
            privilege,
            f"test -f {temp}/node.token && test ! -L {temp}/node.token && "
            f"rm -f -- {token_temp} && "
            f"install -o restream-agent -g restream-agent -m 0600 {temp}/node.token "
            f"{token_temp} && mv -Tf -- {token_temp} {shlex.quote(AGENT_TOKEN_PATH)} && "
            f"rm -f -- {temp}/node.token && "
            "systemctl enable --now adojapan-relay-agent.service",
            timeout=timeouts.command_seconds,
            code="relay_agent_install_failed",
        )
        receipt.enrollment_token_applied = True

    async def final_check(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: RelayInstallReceipt,
        *,
        timeout: float,
    ) -> None:
        guard = (
            f"test -f {shlex.quote(RELAY_MARKER)} && "
            f'test "$(cat {shlex.quote(RELAY_MARKER)})" = '
            f"{shlex.quote(RELAY_MARKER_CONTENT)} && "
            f"test -f {shlex.quote(RELAY_NODE_ID)} && "
            f'test "$(cat {shlex.quote(RELAY_NODE_ID)})" = '
            f"{shlex.quote(str(receipt.node_id))}"
        )
        command = (
            f"{guard} && systemctl is-active --quiet adojapan-relay-agent.service && "
            "systemctl is-enabled --quiet adojapan-relay-agent.service && "
            "systemctl is-active --quiet adojapan-relay-broker.socket && "
            "systemctl is-enabled --quiet adojapan-relay-broker.socket && "
            "! systemctl is-active --quiet moblin-relay.service && "
            "! systemctl is-enabled --quiet moblin-relay.service && "
            f"test \"$(stat -c '%U:%G:%a' {shlex.quote(AGENT_TOKEN_PATH)})\" = "
            "'restream-agent:restream-agent:600' && "
            f"test \"$(stat -c '%U:%G:%a' {shlex.quote(RELAY_SECRET_FILE)})\" = "
            "'root:root:600' && "
            f"{_system_account_validation_command()} && "
            f"/usr/local/sbin/relayctl status >/dev/null && "
            f"install -o root -g root -m 0644 {shlex.quote(receipt.temp_root + '/release')} "
            f"{shlex.quote(RELAY_RELEASE_FILE)}"
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("relay_final_check_failed")
        receipt.enrollment_completed = True

    async def rollback(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: RelayInstallReceipt,
        *,
        timeout: float,
    ) -> None:
        if receipt.agent_start_attempted:
            with suppress(BootstrapError):
                await privilege.run(
                    session,
                    "systemctl disable --now adojapan-relay-agent.service 2>/dev/null || true; "
                    "systemctl stop adojapan-relay-broker.service 2>/dev/null || true",
                    timeout=timeout,
                )
        # Once the backend has observed the credential, keep exact managed
        # evidence for a same-node retry; its digest is revoked transactionally.
        if (
            receipt.ownership is not InstallOwnership.ABSENT
            or not receipt.managed_scope_acquired
            or receipt.enrollment_completed
        ):
            return
        marker_guard = (
            f"test -f {shlex.quote(RELAY_MARKER)} && "
            f"test ! -L {shlex.quote(RELAY_MARKER)} && "
            f'test "$(cat {shlex.quote(RELAY_MARKER)})" = '
            f"{shlex.quote(RELAY_MARKER_CONTENT)} && "
            f"test -f {shlex.quote(RELAY_NODE_ID)} && "
            f'test "$(cat {shlex.quote(RELAY_NODE_ID)})" = '
            f"{shlex.quote(str(receipt.node_id))}"
        )
        cleanup = (
            f"if {marker_guard}; then "
            "systemctl disable --now moblin-relay.service 2>/dev/null || true; "
            "systemctl disable --now adojapan-relay-agent.service 2>/dev/null || true; "
            "systemctl disable --now adojapan-relay-broker.socket 2>/dev/null || true; "
            "rm -f -- /etc/systemd/system/moblin-relay.service "
            "/etc/systemd/system/adojapan-relay-agent.service "
            "/etc/systemd/system/adojapan-relay-broker.service "
            "/etc/systemd/system/adojapan-relay-broker.socket "
            "/etc/sysusers.d/adojapan-relay-agent.conf "
            "/etc/tmpfiles.d/adojapan-relay-agent.conf "
            "/usr/local/sbin/relayctl /usr/local/sbin/adojapan-relay-install-token "
            "/usr/local/sbin/adojapan-relay-install-preview-token "
            "/usr/local/sbin/adojapan-relay-restore-v1-journal "
            "/usr/local/libexec/moblin-relay-render-config; "
            f"rm -rf -- {shlex.quote(RELAY_OPT_ROOT)} {shlex.quote(RELAY_STATE_ROOT)} "
            f"{shlex.quote(AGENT_ETC_ROOT)} {shlex.quote(AGENT_STATE_ROOT)} "
            "/usr/local/lib/adojapan-relay-agent; "
            f"rm -rf -- {shlex.quote(RELAY_ETC_ROOT)}; systemctl daemon-reload; "
            "else exit 1; fi"
        )
        with suppress(BootstrapError):
            result = await privilege.run(session, cleanup, timeout=timeout)
            receipt.rollback_succeeded = result.exit_status == 0

    async def cleanup_temp(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: RelayInstallReceipt,
        *,
        timeout: float,
    ) -> None:
        expected = f"{_REMOTE_TEMP_PREFIX}{receipt.job_id}"
        if receipt.temp_root != expected:
            return
        # Package extraction creates root-owned children when SSH uses sudo, so
        # cleanup must use the already verified privilege context as well.
        token_temp = shlex.quote(f"{AGENT_ETC_ROOT}/.node.token.{receipt.job_id}.tmp")
        agent_stage = shlex.quote(f"/run/adojapan-relay-install.{receipt.job_id}.stage")
        self_test_stage = shlex.quote(f"/run/moblin-relay-self-test.{receipt.job_id}.stage")
        with suppress(BootstrapError):
            await privilege.run(
                session,
                f"rm -f -- {token_temp} {agent_stage} {self_test_stage}; "
                f"rm -rf -- {shlex.quote(receipt.temp_root)}",
                timeout=timeout,
            )


__all__ = [
    "MEDIA_MTX_ARCHIVE_SHA256",
    "MEDIA_MTX_URL",
    "RELAY_RELEASE",
    "RelayInstallReceipt",
    "RemoteRelayInstaller",
    "load_relay_bundle",
    "validate_relay_platform",
]
