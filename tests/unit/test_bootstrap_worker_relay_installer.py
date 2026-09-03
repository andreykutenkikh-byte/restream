from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError
from bootstrap_worker.installer import PrivilegeContext
from bootstrap_worker.models import (
    BootstrapRequest,
    InstallOwnership,
    PackageManager,
    PlatformFamily,
    PrivilegeMode,
    SELinuxMode,
    SystemFacts,
    TargetIdentity,
    TimeoutPolicy,
)
from bootstrap_worker.relay_installer import (
    MEDIA_MTX_ARCHIVE_SHA256,
    MEDIA_MTX_URL,
    RELAY_CONTROL_ORIGIN,
    RelayInstallReceipt,
    RemoteRelayInstaller,
    load_relay_bundle,
    validate_relay_platform,
)
from bootstrap_worker.ssh import RemoteResult

IMAGE = f"ghcr.io/andreykutenkikh-byte/restream-node@sha256:{'a' * 64}"
TOKEN_VALUE = "relay-token-marker-123456789012345678901234567890"


class FakeSession:
    def __init__(
        self,
        responder: Callable[[str, SecretStr | None], RemoteResult] | None = None,
    ) -> None:
        self.responder = responder or (lambda command, stdin: RemoteResult(0))
        self.commands: list[tuple[str, SecretStr | None, float]] = []
        self.uploads: dict[str, tuple[bytes, int]] = {}

    async def run(
        self,
        command: str,
        *,
        stdin: SecretStr | None = None,
        timeout: float,
    ) -> RemoteResult:
        self.commands.append((command, stdin, timeout))
        return self.responder(command, stdin)

    async def put(self, path: str, content: bytes, *, mode: int, timeout: float) -> None:
        del timeout
        self.uploads[path] = (content, mode)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def request(**updates: object) -> BootstrapRequest:
    values: dict[str, object] = {
        "node_id": uuid4(),
        "address": "203.0.113.10",
        "username": "root",
        "password": "ssh-secret",
        "control_url": RELAY_CONTROL_ORIGIN,
        "node_agent_image": IMAGE,
    }
    values.update(updates)
    return BootstrapRequest.model_validate(values)


def facts(**updates: object) -> SystemFacts:
    values: dict[str, object] = {
        "hostname": "relay-01",
        "os_name": "ubuntu",
        "os_id": "ubuntu",
        "os_version": "24.04",
        "os_major_version": "24",
        "id_like": ("debian",),
        "version_codename": "noble",
        "architecture": "amd64",
        "platform_family": PlatformFamily.DEBIAN,
        "package_manager": PackageManager.APT,
        "selinux_mode": SELinuxMode.DISABLED,
        "apt_get_available": True,
        "dpkg_query_available": True,
        "dnf_available": False,
        "rpm_available": False,
        "systemctl_available": True,
        "cpu_count": 2,
        "memory_total_bytes": 4 * 1024**3,
        "memory_available_bytes": 2 * 1024**3,
        "disk_total_bytes": 40 * 1024**3,
        "disk_free_bytes": 20 * 1024**3,
    }
    values.update(updates)
    if "os_id" in updates:
        values["os_name"] = values["os_id"]
    if "os_version" in updates:
        values["os_major_version"] = str(values["os_version"]).split(".", 1)[0]
    return SystemFacts.model_validate(values)


def target(address: str = "203.0.113.10") -> TargetIdentity:
    return TargetIdentity(
        address=address,
        port=22,
        resolved_ip=address,
        resolution_set=(address,),
    )


def absent_responder(command: str, stdin: SecretStr | None) -> RemoteResult:
    del stdin
    if ".managed-by-adojapan" in command and "printf" in command:
        return RemoteResult(0, "absent\n")
    return RemoteResult(0)


def receipt(**updates: object) -> RelayInstallReceipt:
    job_id = updates.pop("job_id", uuid4())
    assert isinstance(job_id, UUID)
    values: dict[str, object] = {
        "temp_root": f"/tmp/adojapan-relay-bootstrap-{job_id}",  # noqa: S108
        "job_id": job_id,
        "ownership": InstallOwnership.ABSENT,
        "docker_installed": False,
        "node_id": uuid4(),
        "public_srt_host": "203.0.113.10",
    }
    values.update(updates)
    return RelayInstallReceipt(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("os_id", "version"),
    (("ubuntu", "22.04"), ("ubuntu", "24.04"), ("debian", "12"), ("debian", "13")),
)
def test_relay_platform_matrix_is_deliberately_narrow(os_id: str, version: str) -> None:
    validate_relay_platform(
        facts(
            os_id=os_id,
            os_version=version,
            version_codename="bookworm" if os_id == "debian" else "noble",
        )
    )


@pytest.mark.parametrize(
    "unsupported",
    (
        facts(os_version="26.04"),
        facts(architecture="arm64"),
        facts(os_id="almalinux", os_version="9", platform_family=PlatformFamily.RHEL),
    ),
)
def test_relay_platform_rejects_untested_operating_systems(unsupported: SystemFacts) -> None:
    with pytest.raises(BootstrapError) as captured:
        validate_relay_platform(unsupported)
    assert captured.value.code == "unsupported_relay_operating_system"


def test_bundle_loader_has_a_fixed_reviewed_allowlist() -> None:
    root = Path(__file__).resolve().parents[2]
    bundle = load_relay_bundle(root)
    names = {str(path) for path in bundle}
    assert "deploy/moblin-relay/relayctl" in names
    assert "deploy/moblin-relay/self-test" in names
    assert "deploy/hk-relay-agent/install.sh" in names
    assert "relay_agent/__init__.py" in names
    assert "relay_agent/broker.py" in names
    assert "deploy/moblin-relay/README.md" not in names
    assert "deploy/moblin-relay/node.json.example" not in names
    assert "deploy/moblin-relay/test-render-config.py" not in names
    assert all(payload for payload in bundle.values())


async def test_prepare_stages_token_only_as_a_mode_0600_sftp_payload() -> None:
    session = FakeSession(absent_responder)
    installer = RemoteRelayInstaller()
    selected_request = request()
    job_id = uuid4()

    prepared = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        selected_request,
        facts(),
        enrollment_token=SecretStr(TOKEN_VALUE),
        job_id=job_id,
        docker_installed=False,
        timeouts=TimeoutPolicy(),
        target=target(),
    )

    assert prepared.temp_root == f"/tmp/adojapan-relay-bootstrap-{job_id}"  # noqa: S108
    assert prepared.job_id == job_id
    assert prepared.public_srt_host == "203.0.113.10"
    assert all(mode == 0o600 for _, mode in session.uploads.values())
    token_path = f"{prepared.temp_root}/node.token"
    assert session.uploads[token_path] == ((TOKEN_VALUE + "\n").encode("ascii"), 0o600)
    node_config = json.loads(session.uploads[f"{prepared.temp_root}/node.json"][0])
    assert node_config == {
        "fallback_srt_hosts": [],
        "public_srt_host": "203.0.113.10",
        "schema": 1,
        "srt_path": "iphone-live",
        "srt_port": 8890,
    }
    commands = "\n".join(command for command, _, _ in session.commands)
    assert TOKEN_VALUE not in commands
    assert TOKEN_VALUE not in repr(session.commands)
    assert "docker" not in commands.lower()
    assert "/proc/net/tcp" in commands
    assert "22BA 216A 078F 22B8 270E" in commands


@pytest.mark.parametrize(
    ("control_url", "token_value", "code"),
    (
        ("https://relay.invalid", TOKEN_VALUE, "invalid_relay_control_origin"),
        (RELAY_CONTROL_ORIGIN, "not long enough", "invalid_enrollment_token"),
        (RELAY_CONTROL_ORIGIN, "ü" * 40, "invalid_enrollment_token"),
    ),
)
async def test_prepare_rejects_wrong_origin_and_unsafe_tokens_before_upload(
    control_url: str,
    token_value: str,
    code: str,
) -> None:
    session = FakeSession(absent_responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(control_url=control_url),
            facts(),
            enrollment_token=SecretStr(token_value),
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
            target=target(),
        )
    assert captured.value.code == code
    assert not session.uploads


async def test_prepare_fails_closed_on_foreign_named_system_account() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        ownership = absent_responder(command, stdin)
        if ownership.stdout:
            return ownership
        if "for spec in" in command and "restream-agent" in command:
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(),
            facts(),
            enrollment_token=SecretStr(TOKEN_VALUE),
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
            target=target(),
        )
    assert captured.value.code == "remote_relay_account_conflict"
    assert not session.uploads


async def test_managed_retry_rejects_symlink_or_special_file_in_owned_paths() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if ".managed-by-adojapan" in command and "printf" in command:
            return RemoteResult(0, "managed\n")
        if "adojapan-relay-install-preview-token" in command and "if test -e" in command:
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(),
            facts(),
            enrollment_token=SecretStr(TOKEN_VALUE),
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
            target=target(),
        )
    assert captured.value.code == "remote_relay_conflict"
    assert not session.uploads


async def test_install_uses_pinned_mediamtx_and_never_mutates_host_networking() -> None:
    session = FakeSession()
    prepared = receipt()
    await RemoteRelayInstaller().install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        prepared,
        timeouts=TimeoutPolicy(),
    )
    commands = "\n".join(command for command, _, _ in session.commands)
    lowered = commands.lower()

    assert MEDIA_MTX_URL in commands
    assert MEDIA_MTX_ARCHIVE_SHA256 in commands
    assert "--retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 240" in commands
    assert "sha256sum --check --status" in commands
    assert "color=c=0x111827:s=1080x1920:r=30:d=12" in commands
    assert "-profile:v main" in commands
    assert "-pix_fmt yuv420p" in commands
    assert "-g 60" in commands
    assert "-c:a aac" in commands and "-ar 48000 -ac 2" in commands
    assert "'drawtext=" in commands and "x=(w-text_w)/2" in commands
    assert "systemctl disable --now moblin-relay.service" in commands
    assert "systemctl enable --now adojapan-relay-agent.service" in commands
    assert "/opt/moblin-relay/libexec/self-test --quick" in commands
    assert "MOBLIN_RELAY_SELF_TEST_STAGE_FILE=/run/moblin-relay-self-test." in commands
    assert "self_test_status" in commands
    assert "account_shell" in commands and "group_members" in commands
    assert TOKEN_VALUE not in commands
    for forbidden in (
        "docker",
        "iptables",
        "nft ",
        "ufw ",
        "firewall-cmd",
        "ip route",
        "ip link",
        "sysctl",
        "amnezia",
    ):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    ("failure_fragment", "expected_code"),
    [
        ("curl --fail --location", "mediamtx_download_failed"),
        ("sha256sum --check --status", "mediamtx_checksum_failed"),
        ("tar -xzf", "mediamtx_archive_invalid"),
        ("test -f /tmp/adojapan-relay-bootstrap-", "mediamtx_license_missing"),
        ("stat -c", "mediamtx_binary_invalid"),
    ],
)
async def test_install_reports_exact_safe_mediamtx_stage(
    failure_fragment: str,
    expected_code: str,
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        return RemoteResult(1 if failure_fragment in command else 0)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == expected_code


async def test_staged_mediamtx_validation_supports_a_noexec_temp_mount() -> None:
    session = FakeSession()

    await RemoteRelayInstaller().install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt(),
        timeouts=TimeoutPolicy(),
    )

    validation = next(command for command, _, _ in session.commands if "stat -c" in command)
    assert "test ! -L" in validation
    assert '" = 755' in validation
    assert "test -x" not in validation


@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [
        ("preflight", "relay_agent_preflight_failed"),
        ("accounts", "relay_agent_accounts_failed"),
        ("sysusers", "relay_agent_sysusers_failed"),
        ("tmpfiles", "relay_agent_tmpfiles_failed"),
        ("journal", "relay_agent_journal_failed"),
        ("copy", "relay_agent_copy_failed"),
        ("units", "relay_agent_units_failed"),
        ("broker", "relay_agent_broker_failed"),
    ],
)
async def test_agent_install_failure_uses_only_allowlisted_safe_stage(
    stage: str,
    expected_code: str,
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "ADOJAPAN_RELAY_INSTALL_STAGE_FILE=" in command:
            return RemoteResult(1)
        if "stage_value=$(cat" in command:
            return RemoteResult(0, stage)
        return RemoteResult(0)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == expected_code


async def test_agent_install_failure_rejects_an_unknown_diagnostic_stage() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "ADOJAPAN_RELAY_INSTALL_STAGE_FILE=" in command:
            return RemoteResult(1)
        if "stage_value=$(cat" in command:
            return RemoteResult(0, "untrusted-stage\nextra")
        return RemoteResult(0)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == "relay_agent_install_failed"


@pytest.mark.parametrize(
    ("diagnostic", "expected_code"),
    [
        ("startup", "relay_self_test_startup_failed"),
        ("assets", "relay_self_test_assets_failed"),
        ("topology", "relay_self_test_topology_failed"),
        ("auth", "relay_self_test_auth_failed"),
        ("auth-source", "relay_self_test_auth_source_failed"),
        ("auth-live", "relay_self_test_auth_live_failed"),
        ("auth-scan", "relay_self_test_auth_scan_failed"),
        ("auth-exclusive", "relay_self_test_auth_exclusivity_failed"),
        ("live-ingest", "relay_self_test_live_ingest_failed"),
        ("live-normalize", "relay_self_test_live_normalize_failed"),
        ("norm-hook", "relay_self_test_normalizer_hook_failed"),
        ("norm-child", "relay_self_test_normalizer_child_failed"),
        ("norm-publish", "relay_self_test_normalizer_publish_failed"),
        ("norm-flap", "relay_self_test_normalizer_flap_failed"),
        ("stall-slate", "relay_self_test_stall_slate_failed"),
        ("stall-live", "relay_self_test_stall_live_failed"),
        ("stall-cont", "relay_self_test_stall_continuity_failed"),
        ("crash-death", "relay_self_test_crash_death_failed"),
        ("crash-live", "relay_self_test_crash_live_failed"),
        ("crash-cont", "relay_self_test_crash_continuity_failed"),
        ("outages", "relay_self_test_outages_failed"),
        ("outage-slate", "relay_self_test_outage_slate_failed"),
        ("outage-normal", "relay_self_test_outage_normal_failed"),
        ("outage-hold", "relay_self_test_outage_hold_failed"),
        ("outage-live", "relay_self_test_outage_live_failed"),
        ("continuity", "relay_self_test_continuity_failed"),
        ("decode", "relay_self_test_decode_failed"),
        ("streams", "relay_self_test_decode_streams_failed"),
        ("format", "relay_self_test_decode_format_failed"),
        ("gop", "relay_self_test_decode_gop_failed"),
        ("decoder", "relay_self_test_decode_decoder_failed"),
        ("frames", "relay_self_test_decode_frames_failed"),
        ("timestamps", "relay_self_test_decode_timestamps_failed"),
        ("ts-probe-pts", "relay_self_test_timestamp_probe_pts_failed"),
        ("ts-packet-dts", "relay_self_test_timestamp_packet_dts_failed"),
        ("ts-video-pts", "relay_self_test_timestamp_video_pts_failed"),
        ("ts-audio-pts", "relay_self_test_timestamp_audio_pts_failed"),
        ("ts-gaps", "relay_self_test_timestamp_gaps_failed"),
        ("ts-av-sync", "relay_self_test_timestamp_av_sync_failed"),
        ("secrets", "relay_self_test_secrets_failed"),
        ("cleanup", "relay_self_test_cleanup_failed"),
    ],
)
async def test_self_test_failure_uses_only_a_derived_allowlisted_diagnostic(
    diagnostic: str,
    expected_code: str,
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "self-test --quick >/dev/null 2>&1" in command:
            return RemoteResult(1)
        if "self_test_stage_value=$(cat" in command:
            return RemoteResult(0, diagnostic)
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == expected_code
    diagnostic_command = next(
        command for command, _, _ in session.commands if "self_test_stage_value=$(cat" in command
    )
    assert "/run/moblin-relay-self-test." in diagnostic_command
    assert "test ! -L" in diagnostic_command
    assert "'%u:%a:%h'" in diagnostic_command
    assert "'0:600:1'" in diagnostic_command
    assert '"$(wc -c <' in diagnostic_command


async def test_self_test_failure_rejects_an_unknown_diagnostic() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "self-test --quick >/dev/null 2>&1" in command:
            return RemoteResult(1)
        if "self_test_stage_value=$(cat" in command:
            return RemoteResult(0, "untrusted")
        return RemoteResult(0)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == "relay_self_test_failed"


async def test_final_check_requires_agent_and_broker_but_relay_inactive_disabled() -> None:
    session = FakeSession()
    prepared = receipt()
    await RemoteRelayInstaller().final_check(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        prepared,
        timeout=60,
    )
    command = session.commands[-1][0]
    assert "is-active --quiet adojapan-relay-agent.service" in command
    assert "is-enabled --quiet adojapan-relay-agent.service" in command
    assert "is-active --quiet adojapan-relay-broker.socket" in command
    assert "! systemctl is-active --quiet moblin-relay.service" in command
    assert "! systemctl is-enabled --quiet moblin-relay.service" in command
    assert "account_uid" in command and "group_members" in command
    assert "/etc/moblin-relay/release" in command
    assert prepared.enrollment_completed is True


async def test_fresh_rollback_is_marker_and_node_id_guarded() -> None:
    session = FakeSession()
    prepared = receipt(managed_scope_acquired=True, files_applied=True)
    await RemoteRelayInstaller().rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        prepared,
        timeout=60,
    )
    command = "\n".join(item[0] for item in session.commands)
    assert "/etc/moblin-relay/.managed-by-adojapan" in command
    assert str(prepared.node_id) in command
    assert "rm -rf -- /etc/moblin-relay" in command
    assert "rm -rf -- /opt/moblin-relay /var/lib/moblin-relay" in command
    assert prepared.rollback_succeeded is True


async def test_cleanup_uses_privilege_and_only_the_exact_job_path() -> None:
    installer = RemoteRelayInstaller()
    session = FakeSession()
    prepared = receipt()
    await installer.cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        prepared,
        timeout=60,
    )
    assert len(session.commands) == 1
    assert f"rm -rf -- {prepared.temp_root}" in session.commands[0][0]
    assert f"/run/adojapan-relay-install.{prepared.job_id}.stage" in session.commands[0][0]
    assert f"/run/moblin-relay-self-test.{prepared.job_id}.stage" in session.commands[0][0]
    assert session.commands[0][0].startswith("env LC_ALL=C sh -c")

    session.commands.clear()
    prepared.temp_root = "/tmp/not-the-exact-job-path"  # noqa: S108
    await installer.cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        prepared,
        timeout=60,
    )
    assert session.commands == []


def test_bootstrap_image_contains_only_the_required_native_relay_sources() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile.bootstrap").read_text(encoding="utf-8")
    assert "COPY relay_agent ./relay_agent" in dockerfile
    assert "COPY deploy/hk-relay-agent ./deploy/hk-relay-agent" in dockerfile
    assert "COPY deploy/moblin-relay ./deploy/moblin-relay" in dockerfile
    assert "COPY app " not in dockerfile
    assert "COPY node_agent " not in dockerfile
