from __future__ import annotations

import ast
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
    _SELF_TEST_STAGE_CODES,
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
    assert "deploy/moblin-relay/moblin-relay-normalize" in names
    assert "deploy/moblin-relay/moblin-relay-render-config" in names
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
    assert "22BA 216A 078F 22B8 270D 270E" in commands


async def test_control_api_port_collision_fails_closed_before_upload() -> None:
    observed: list[str] = []

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        observed.append(command)
        return RemoteResult(1)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller._assert_ports_free(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            timeout=1,
        )

    assert captured.value.code == "relay_port_conflict"
    assert len(observed) == 1
    assert "270D" in observed[0]
    assert "270E" in observed[0]


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
    assert "-b:v 8M -minrate 8M -maxrate 8M -bufsize 16M" in commands
    assert "-x264-params nal-hrd=cbr:force-cfr=1:filler=1:bframes=0" in commands
    assert "-c:a aac" in commands and "-ar 48000 -ac 2" in commands
    assert "'drawtext=" in commands and "x=(w-text_w)/2" in commands
    assert "install -o root -g moblin-relay -m 0640" in commands
    assert "/slate.mp4" in commands
    assert "systemctl disable --now moblin-relay.service" in commands
    assert "systemctl enable --now adojapan-relay-agent.service" in commands
    assert "/opt/moblin-relay/libexec/self-test --quick" in commands
    assert "MOBLIN_RELAY_SELF_TEST_STAGE_FILE=/run/moblin-relay-self-test." in commands
    assert "self_test_status" in commands
    assert "account_shell" in commands and "group_members" in commands
    assert "if test -e /etc/moblin-relay/secrets.json" in commands
    assert "stat -c" in commands and "%u:%a" in commands and "0:600" in commands
    assert "else /opt/moblin-relay/libexec/initialize-secrets; fi" in commands
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


def test_installer_maps_every_declared_self_test_stage_exactly() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    assignment = next(
        statement
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SELF_TEST_STAGES"
            for target in statement.targets
        )
    )
    assert isinstance(assignment.value, ast.Call)
    declared = set(ast.literal_eval(assignment.value.args[0]))

    assert set(_SELF_TEST_STAGE_CODES) == declared


@pytest.mark.parametrize(
    ("diagnostic", "expected_code"),
    [
        ("startup", "relay_self_test_startup_failed"),
        ("assets", "relay_self_test_assets_failed"),
        ("topology", "relay_self_test_topology_failed"),
        ("auth", "relay_self_test_auth_failed"),
        ("auth-source", "relay_self_test_auth_source_failed"),
        ("auth-src-help", "relay_self_test_auth_source_helper_failed"),
        ("auth-src-bind", "relay_self_test_auth_source_publisher_bind_failed"),
        ("auth-src-feed", "relay_self_test_auth_source_feeder_failed"),
        ("auth-src-path", "relay_self_test_auth_source_path_failed"),
        ("auth-scan", "relay_self_test_auth_scan_failed"),
        ("auth-exclusive", "relay_self_test_auth_exclusivity_failed"),
        ("auth-x-core", "relay_self_test_auth_exclusivity_core_failed"),
        ("auth-x-second", "relay_self_test_auth_exclusivity_candidate_failed"),
        ("auth-x-primary", "relay_self_test_auth_exclusivity_primary_failed"),
        ("auth-x-live", "relay_self_test_auth_exclusivity_live_failed"),
        ("auth-x-ingest", "relay_self_test_auth_exclusivity_ingest_failed"),
        ("auth-x-norm", "relay_self_test_auth_exclusivity_normalizer_failed"),
        (
            "auth-n-child",
            "relay_self_test_auth_exclusivity_normalizer_child_exit_failed",
        ),
        (
            "auth-n-start",
            "relay_self_test_auth_exclusivity_normalizer_start_timeout_failed",
        ),
        (
            "auth-n-blind",
            "relay_self_test_auth_exclusivity_normalizer_metrics_blind_failed",
        ),
        (
            "auth-n-out-id",
            "relay_self_test_auth_exclusivity_normalizer_output_identity_failed",
        ),
        (
            "auth-n-out-reg",
            "relay_self_test_auth_exclusivity_normalizer_output_regression_failed",
        ),
        (
            "auth-n-fallback",
            "relay_self_test_auth_exclusivity_normalizer_output_fallback_failed",
        ),
        (
            "auth-n-in-time",
            "relay_self_test_auth_exclusivity_normalizer_ingest_timing_failed",
        ),
        (
            "auth-n-in-miss",
            "relay_self_test_auth_exclusivity_normalizer_ingest_missing_failed",
        ),
        (
            "auth-n-in-id",
            "relay_self_test_auth_exclusivity_normalizer_ingest_identity_failed",
        ),
        (
            "auth-n-in-reg",
            "relay_self_test_auth_exclusivity_normalizer_ingest_regression_failed",
        ),
        (
            "auth-n-stall",
            "relay_self_test_auth_exclusivity_normalizer_verified_stall_failed",
        ),
        (
            "auth-n-confirm",
            "relay_self_test_auth_exclusivity_normalizer_confirmed_input_stall_failed",
        ),
        (
            "auth-n-unknown",
            "relay_self_test_auth_exclusivity_normalizer_watchdog_unknown_failed",
        ),
        ("auth-x-sink", "relay_self_test_auth_exclusivity_downstream_failed"),
        ("auth-x-bytes", "relay_self_test_auth_exclusivity_progress_failed"),
        ("auth-x-blind", "relay_self_test_auth_exclusivity_observability_failed"),
        ("auth-x-attempt", "relay_self_test_auth_exclusivity_proof_failed"),
        ("live-ingest", "relay_self_test_live_ingest_failed"),
        ("live-normalize", "relay_self_test_live_normalize_failed"),
        ("norm-hook", "relay_self_test_normalizer_hook_failed"),
        ("norm-child", "relay_self_test_normalizer_child_failed"),
        ("norm-publish", "relay_self_test_normalizer_publish_failed"),
        ("norm-flap", "relay_self_test_normalizer_flap_failed"),
        ("dts-regression", "relay_self_test_dts_regression_failed"),
        ("stall-slate", "relay_self_test_stall_slate_failed"),
        ("stall-pre", "relay_self_test_stall_precondition_failed"),
        ("stall-pause", "relay_self_test_stall_pause_failed"),
        ("stall-switch", "relay_self_test_stall_switch_failed"),
        ("stall-capture", "relay_self_test_stall_capture_failed"),
        ("stall-resume", "relay_self_test_stall_resume_failed"),
        ("stall-live", "relay_self_test_stall_live_failed"),
        ("stall-core", "relay_self_test_stall_core_failed"),
        ("stall-source", "relay_self_test_stall_source_failed"),
        ("stall-ingest", "relay_self_test_stall_ingest_failed"),
        ("stall-i-off", "relay_self_test_stall_ingest_offline_failed"),
        ("stall-i-id", "relay_self_test_stall_ingest_identity_failed"),
        ("stall-id-pre", "relay_self_test_stall_ingest_identity_pre_resume_failed"),
        ("stall-id-rec", "relay_self_test_stall_ingest_identity_recovery_failed"),
        ("stall-i-byte", "relay_self_test_stall_ingest_progress_failed"),
        ("stall-h-blind", "relay_self_test_stall_helper_observability_failed"),
        ("stall-h-path", "relay_self_test_stall_helper_path_failed"),
        ("stall-h-error", "relay_self_test_stall_helper_forward_failed"),
        ("stall-h-state", "relay_self_test_stall_helper_state_failed"),
        ("stall-norm", "relay_self_test_stall_normalizer_failed"),
        ("stall-sink", "relay_self_test_stall_downstream_failed"),
        ("stall-blind", "relay_self_test_stall_observability_failed"),
        ("stall-ident", "relay_self_test_stall_identity_failed"),
        ("stall-cont", "relay_self_test_stall_continuity_failed"),
        ("stuck-start", "relay_self_test_persistent_stall_precondition_failed"),
        ("stuck-slate", "relay_self_test_persistent_stall_slate_failed"),
        ("stuck-open", "relay_self_test_persistent_stall_confirmation_failed"),
        ("stuck-kicked", "relay_self_test_persistent_stall_reset_failed"),
        ("stuck-live", "relay_self_test_persistent_stall_reconnect_failed"),
        ("stuck-source", "relay_self_test_persistent_stall_source_failed"),
        ("stuck-cont", "relay_self_test_persistent_stall_continuity_failed"),
        ("crash-death", "relay_self_test_crash_death_failed"),
        ("crash-live", "relay_self_test_crash_live_failed"),
        ("crash-cont", "relay_self_test_crash_continuity_failed"),
        ("reset-start", "relay_self_test_reset_precondition_failed"),
        ("reset-kill", "relay_self_test_reset_injection_failed"),
        ("reset-slate", "relay_self_test_reset_slate_failed"),
        ("reset-open", "relay_self_test_reset_circuit_failed"),
        ("reset-kicked", "relay_self_test_reset_kick_failed"),
        ("reset-session", "relay_self_test_reset_reconnect_failed"),
        ("reset-source", "relay_self_test_reset_source_failed"),
        ("reset-cont", "relay_self_test_reset_continuity_failed"),
        ("outages", "relay_self_test_outages_failed"),
        ("outage-slate", "relay_self_test_outage_slate_failed"),
        ("outage-normal", "relay_self_test_outage_normal_failed"),
        ("outage-hold", "relay_self_test_outage_hold_failed"),
        ("outage-live", "relay_self_test_outage_live_failed"),
        ("continuity", "relay_self_test_continuity_failed"),
        ("decode", "relay_self_test_decode_failed"),
        ("streams", "relay_self_test_decode_streams_failed"),
        ("format", "relay_self_test_decode_format_failed"),
        ("fmt-v-codec", "relay_self_test_decode_format_video_codec_failed"),
        ("fmt-v-prof", "relay_self_test_decode_format_video_profile_failed"),
        ("fmt-v-level", "relay_self_test_decode_format_video_level_failed"),
        ("fmt-v-bframes", "relay_self_test_decode_format_video_b_frames_failed"),
        ("fmt-v-size", "relay_self_test_decode_format_video_dimensions_failed"),
        ("fmt-v-pixfmt", "relay_self_test_decode_format_video_pixel_format_failed"),
        ("fmt-v-rfps", "relay_self_test_decode_format_video_r_frame_rate_failed"),
        ("fmt-a-codec", "relay_self_test_decode_format_audio_codec_failed"),
        ("fmt-a-prof", "relay_self_test_decode_format_audio_profile_failed"),
        ("fmt-a-rate", "relay_self_test_decode_format_audio_sample_rate_failed"),
        ("fmt-a-chans", "relay_self_test_decode_format_audio_channels_failed"),
        ("fmt-a-layout", "relay_self_test_decode_format_audio_layout_failed"),
        ("gop", "relay_self_test_decode_gop_failed"),
        ("decoder", "relay_self_test_decode_decoder_failed"),
        ("frames", "relay_self_test_decode_frames_failed"),
        ("timestamps", "relay_self_test_decode_timestamps_failed"),
        ("ts-probe-pts", "relay_self_test_timestamp_probe_pts_failed"),
        ("ts-packet-dts", "relay_self_test_timestamp_packet_dts_failed"),
        ("ts-video-pts", "relay_self_test_timestamp_video_pts_failed"),
        ("ts-v-offset", "relay_self_test_timestamp_video_pts_offset_failed"),
        ("ts-v-order", "relay_self_test_timestamp_video_pts_order_failed"),
        ("ts-v-fps", "relay_self_test_timestamp_video_frame_rate_failed"),
        ("ts-audio-pts", "relay_self_test_timestamp_audio_pts_failed"),
        ("ts-gaps", "relay_self_test_timestamp_gaps_failed"),
        ("ts-g-vdts", "relay_self_test_timestamp_gap_video_dts_failed"),
        ("ts-g-adts", "relay_self_test_timestamp_gap_audio_dts_failed"),
        ("ts-g-vpts", "relay_self_test_timestamp_gap_video_pts_failed"),
        ("ts-g-apts", "relay_self_test_timestamp_gap_audio_pts_failed"),
        ("ts-g-vdec", "relay_self_test_timestamp_gap_decoded_video_failed"),
        ("ts-g-adec", "relay_self_test_timestamp_gap_decoded_audio_failed"),
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


@pytest.mark.parametrize(
    "operation,expected_code",
    [
        ("apt-get -qq update", "relay_dependency_install_failed"),
        ("curl --fail --location", "mediamtx_download_failed"),
        ("ffmpeg -nostdin", "relay_slate_generation_failed"),
    ],
)
@pytest.mark.parametrize("failure_kind", ["nonzero", "transport", "timeout"])
async def test_install_failure_preserves_exact_operation_and_timeout(
    operation: str, expected_code: str, failure_kind: str
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if operation in command:
            if failure_kind == "nonzero":
                return RemoteResult(1, "private-command-output", "private-command-error")
            raise BootstrapError(
                "remote_command_timeout" if failure_kind == "timeout" else "remote_command_failed",
                "private-transport-detail",
            )
        return RemoteResult(0)

    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            FakeSession(responder),
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == expected_code + ("_timeout" if failure_kind == "timeout" else "")
    assert "private-" not in str(captured.value)


@pytest.mark.parametrize("operation", ["agent", "self-test"])
@pytest.mark.parametrize("failure_kind", ["nonzero", "transport", "timeout"])
@pytest.mark.parametrize("diagnostic_kind", ["known", "untrusted", "read-failed", "nonzero"])
async def test_install_remote_failure_still_reads_only_its_bounded_stage(
    operation: str, failure_kind: str, diagnostic_kind: str
) -> None:
    self_test = operation == "self-test"
    command_marker = (
        "self-test --quick >/dev/null 2>&1" if self_test else "sh deploy/hk-relay-agent/install.sh"
    )
    diagnostic_marker = "self_test_stage_value=$(cat" if self_test else "stage_value=$(cat"
    known_stage = "stuck-kicked" if self_test else "copy"
    known_code = (
        "relay_self_test_persistent_stall_reset_failed" if self_test else "relay_agent_copy_failed"
    )
    fallback_code = "relay_self_test_failed" if self_test else "relay_agent_install_failed"

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if command_marker in command:
            if failure_kind == "nonzero":
                return RemoteResult(1)
            raise BootstrapError(
                "remote_command_timeout" if failure_kind == "timeout" else "remote_command_failed",
                "private-remote-error",
            )
        if diagnostic_marker in command:
            if diagnostic_kind == "read-failed":
                raise BootstrapError("remote_command_failed", "private-diagnostic-error")
            if diagnostic_kind == "nonzero":
                return RemoteResult(1, known_stage)
            return RemoteResult(0, known_stage if diagnostic_kind == "known" else "private-stage")
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteRelayInstaller().install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt(),
            timeouts=TimeoutPolicy(),
        )
    expected = known_code if diagnostic_kind == "known" else fallback_code
    assert captured.value.code == expected + ("_timeout" if failure_kind == "timeout" else "")
    assert "private-" not in str(captured.value)
    diagnostics = [item for item in session.commands if diagnostic_marker in item[0]]
    assert len(diagnostics) == 1
    assert diagnostics[0][2] == TimeoutPolicy().command_seconds
    assert '"$(wc -c <' in diagnostics[0][0] and "-le 16" in diagnostics[0][0]
    assert "test ! -L" in diagnostics[0][0]
    assert len(session.commands) == session.commands.index(diagnostics[0]) + 1


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
