from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from uuid import uuid4

import pytest
import yaml
from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError
from bootstrap_worker.installer import (
    COMPOSE_PROJECT,
    DOCKER_COMPOSE,
    AgentProcessState,
    DockerBootstrap,
    InstallReceipt,
    PrivilegeContext,
    RemoteNodeInstaller,
    detect_privilege,
    parse_system_facts,
    probe_system,
    render_agent_compose,
    validate_operating_system,
    validate_resources,
    verify_sudo_password,
)
from bootstrap_worker.models import (
    BootstrapRequest,
    DockerDisposition,
    InstallOwnership,
    PrivilegeMode,
    SystemFacts,
    TimeoutPolicy,
)
from bootstrap_worker.ssh import RemoteResult

IMAGE = f"ghcr.io/andreykutenkikh-byte/restream-node@sha256:{'b' * 64}"
CI_IMAGE = (
    "ghcr.io/adojapan/ci-node-agent@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)
ENROLLMENT_TOKEN = SecretStr("token-marker-which-must-not-leak-1234567890")


class FakeSession:
    def __init__(
        self,
        responder: Callable[[str, SecretStr | None], RemoteResult] | None = None,
    ) -> None:
        self.responder = responder or (lambda command, stdin: RemoteResult(0))
        self.commands: list[tuple[str, SecretStr | None, float]] = []
        self.uploads: dict[str, tuple[bytes, int]] = {}
        self.closed = False

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
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def request(**updates: object) -> BootstrapRequest:
    values: dict[str, object] = {
        "node_id": uuid4(),
        "address": "node.example.com",
        "username": "root",
        "password": "ssh-private",
        "control_url": "https://restream.example.com",
        "node_agent_image": IMAGE,
    }
    values.update(updates)
    return BootstrapRequest.model_validate(values)


def facts(**updates: object) -> SystemFacts:
    values: dict[str, object] = {
        "hostname": "edge-node-01",
        "os_name": "ubuntu",
        "os_version": "24.04",
        "architecture": "amd64",
        "cpu_count": 2,
        "memory_total_bytes": 4 * 1024**3,
        "memory_available_bytes": 2 * 1024**3,
        "disk_total_bytes": 40 * 1024**3,
        "disk_free_bytes": 20 * 1024**3,
    }
    values.update(updates)
    return SystemFacts.model_validate(values)


def test_bootstrap_image_runs_non_root_and_owns_only_uds_directory() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile.bootstrap").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "ENVIRONMENT=production" in dockerfile
    assert "install -d -o 10001 -g 10001 -m 0700 /run/adojapan-bootstrap" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert 'CMD ["python", "-m", "bootstrap_worker"]' in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "COPY app " not in dockerfile
    assert "COPY node_agent " not in dockerfile
    assert "chown -R 10001:10001 /srv/bootstrap" not in dockerfile


async def test_sudo_password_is_only_sent_through_stdin() -> None:
    secret = SecretStr("sudo-private")
    context = PrivilegeContext(PrivilegeMode.PASSWORD_SUDO, secret)
    session = FakeSession()
    await context.run(session, "install -d /opt/example", timeout=60)
    command, stdin, _ = session.commands[0]
    assert "sudo-private" not in command
    assert "sudo-private" not in repr(session.commands)
    assert stdin is secret


async def test_privilege_detection_uses_ssh_password_then_can_request_sudo() -> None:
    ssh_password = SecretStr("ssh-private")

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        if command == "id -u":
            return RemoteResult(0, "1000\n")
        if command == "sudo -n -p '' true":
            return RemoteResult(1)
        if command == "sudo -S -p '' true":
            assert stdin is ssh_password
            return RemoteResult(1)
        raise AssertionError("unexpected command")

    session = FakeSession(responder)
    assert await detect_privilege(session, ssh_password, timeout=10) is None

    sudo_password = SecretStr("separate-sudo")
    session.responder = lambda command, stdin: RemoteResult(
        0 if command == "sudo -S -p '' true" and stdin is sudo_password else 1
    )
    context = await verify_sudo_password(session, sudo_password, timeout=10)
    assert context is not None
    assert context.mode is PrivilegeMode.PASSWORD_SUDO


def test_system_probe_parser_and_supported_resource_gates() -> None:
    output = """hostname=edge-node-01
os_id=ubuntu
os_version=22.04
architecture=x86_64
cpu_count=2
memory_total_bytes=2147483648
memory_available_bytes=1073741824
disk_total_bytes=34359738368
disk_free_bytes=17179869184
"""
    parsed = validate_resources(validate_operating_system(parse_system_facts(output)))
    assert parsed.architecture == "amd64"
    assert parsed.hostname == "edge-node-01"
    assert parsed.memory_total_bytes == 2147483648
    assert parsed.disk_total_bytes == 34359738368

    with pytest.raises(BootstrapError) as unsupported:
        validate_operating_system(facts(os_name="alpine", os_version="3.21"))
    assert unsupported.value.code == "unsupported_operating_system"
    with pytest.raises(BootstrapError) as low_memory:
        validate_resources(facts(memory_available_bytes=699 * 1024**2))
    assert low_memory.value.code == "insufficient_memory"
    with pytest.raises(BootstrapError) as low_disk:
        validate_resources(facts(disk_free_bytes=8 * 1024**3 - 1))
    assert low_disk.value.code == "insufficient_disk"
    with pytest.raises(ValueError):
        facts(memory_total_bytes=1024, memory_available_bytes=2048)
    with pytest.raises(ValueError):
        facts(disk_total_bytes=1024, disk_free_bytes=2048)
    with pytest.raises(ValueError):
        facts(hostname="edge node")


async def test_system_probe_requires_no_preinstalled_downloader_or_docker_repository() -> None:
    output = """hostname=minimal-node
os_id=debian
os_version=12
architecture=x86_64
cpu_count=1
memory_total_bytes=2147483648
memory_available_bytes=1073741824
disk_total_bytes=34359738368
disk_free_bytes=17179869184
"""

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        assert "curl" not in command
        assert "wget" not in command
        assert "download.docker.com" not in command
        return RemoteResult(0, output)

    parsed = await probe_system(FakeSession(responder), timeout=10)
    assert parsed.hostname == "minimal-node"
    assert parsed.os_id == "debian"


@pytest.mark.parametrize(
    ("os_name", "version", "repository"),
    [
        ("ubuntu", "22.04", "download.docker.com/linux/ubuntu jammy stable"),
        ("ubuntu", "24.04", "download.docker.com/linux/ubuntu noble stable"),
        ("debian", "12", "download.docker.com/linux/debian bookworm stable"),
    ],
)
def test_docker_plan_uses_official_apt_repository_without_convenience_script(
    os_name: str,
    version: str,
    repository: str,
) -> None:
    plan = DockerBootstrap().install_plan(facts(os_name=os_name, os_version=version))
    commands = "\n".join(command for command, _ in plan)
    assert repository in commands
    assert "docker-ce" in commands
    assert "docker-compose-plugin" in commands
    assert "curl" in commands
    assert "| sh" not in commands
    assert "get.docker.com" not in commands
    assert "dist-upgrade" not in commands
    assert "docker prune" not in commands
    assert "remove docker" not in commands
    steps = [command for command, _ in plan]
    prerequisite = steps.index("apt-get install -y --no-install-recommends ca-certificates curl")
    repository_probe = next(
        index for index, command in enumerate(steps) if "--output /dev/null" in command
    )
    key_download = next(
        index
        for index, command in enumerate(steps)
        if "docker.asc.adojapan-tmp" in command and "curl " in command
    )
    assert prerequisite < repository_probe < key_download
    assert "--connect-timeout 10 --max-time 20" in steps[repository_probe]


def test_docker_plan_never_manages_firewall_or_daemon_configuration() -> None:
    commands = "\n".join(
        command
        for command, _ in DockerBootstrap().install_plan(facts(os_name="debian", os_version="12"))
    ).lower()
    forbidden = (
        "uf" + "w",
        "ip" + "tables",
        "ip6" + "tables",
        "nf" + "t",
        "firewall-" + "cmd",
        "/etc/docker/" + "daemon.json",
        "firewall-" + "backend",
        "--ip" + "tables",
        "--ip6" + "tables",
    )
    assert all(item not in commands for item in forbidden)


async def test_docker_absent_reports_safe_egress_error_after_installing_downloader() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "--output /dev/null" in command:
            return RemoteResult(28)
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await DockerBootstrap().install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            facts(os_name="debian", os_version="12"),
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "outbound_https_unavailable"
    commands = [command for command, _, _ in session.commands]
    prerequisite = next(
        index
        for index, command in enumerate(commands)
        if "apt-get install -y --no-install-recommends ca-certificates curl" in command
    )
    repository_probe = next(
        index for index, command in enumerate(commands) if "--output /dev/null" in command
    )
    assert prerequisite < repository_probe
    assert all("printf '%s\\n'" not in command for command in commands)


async def test_docker_ready_inspection_has_no_docker_repository_dependency() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        assert "download.docker.com" not in command
        assert "curl" not in command
        assert "wget" not in command
        return RemoteResult(0)

    session = FakeSession(responder)
    disposition = await DockerBootstrap().inspect(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        timeout=60,
    )
    assert disposition is DockerDisposition.READY
    commands = [command.lower() for command, _, _ in session.commands]
    assert all("apt-get" not in command for command in commands)
    assert all("systemctl enable" not in command for command in commands)
    assert all("systemctl start" not in command for command in commands)
    assert all("systemctl restart" not in command for command in commands)
    assert all("/etc/docker/" + "daemon.json" not in command for command in commands)


async def test_docker_absence_detection_fails_closed_on_existing_installation_state() -> None:
    def clean_responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if command == "command -v docker >/dev/null 2>&1":
            return RemoteResult(1)
        assert "dpkg-query" in command
        assert "/var/lib/docker" in command
        assert "download.docker.com" in command
        return RemoteResult(0)

    clean = FakeSession(clean_responder)
    disposition = await DockerBootstrap().inspect(
        clean,
        PrivilegeContext(PrivilegeMode.ROOT),
        timeout=60,
    )
    assert disposition is DockerDisposition.ABSENT

    collision = FakeSession(
        lambda command, stdin: RemoteResult(
            1 if command == "command -v docker >/dev/null 2>&1" else 9
        )
    )
    disposition = await DockerBootstrap().inspect(
        collision,
        PrivilegeContext(PrivilegeMode.ROOT),
        timeout=60,
    )
    assert disposition is DockerDisposition.UNSUPPORTED


async def test_functional_non_official_docker_installation_is_unsupported() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if command == "command -v docker >/dev/null 2>&1":
            return RemoteResult(0)
        assert "dpkg-query" in command
        return RemoteResult(1)

    disposition = await DockerBootstrap().inspect(
        FakeSession(responder),
        PrivilegeContext(PrivilegeMode.ROOT),
        timeout=60,
    )
    assert disposition is DockerDisposition.UNSUPPORTED


async def test_docker_install_rechecks_absence_before_any_package_change() -> None:
    session = FakeSession(lambda command, stdin: RemoteResult(7))
    with pytest.raises(BootstrapError) as captured:
        await DockerBootstrap().install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            facts(),
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "unsupported_docker_installation"
    assert len(session.commands) == 1
    assert "dpkg-query" in session.commands[0][0]
    assert "apt-get" not in session.commands[0][0]


def test_agent_compose_is_secret_free_and_isolated() -> None:
    payload = request(node_agent_environment="test", control_url="http://backend:8000")
    rendered = render_agent_compose(payload, facts())
    assert ENROLLMENT_TOKEN.get_secret_value() not in rendered
    assert payload.ssh_password.get_secret_value() not in rendered
    assert 'NODE_CONTROL_URL: "http://backend:8000"' in rendered
    assert "NODE_DATA_DIR: /var/lib/adojapan-node" in rendered
    assert "NODE_AGENT_ENVIRONMENT: test" in rendered
    assert 'NODE_HOSTNAME: "edge-node-01"' in rendered
    assert 'NODE_OS_NAME: "ubuntu"' in rendered
    assert 'NODE_OS_VERSION: "24.04"' in rendered
    assert 'NODE_ARCHITECTURE: "amd64"' in rendered
    assert "stop_grace_period: 45s" in rendered
    assert "read_only: true" in rendered
    assert "no-new-privileges:true" in rendered
    assert "cap_drop:\n      - ALL" in rendered
    assert "privileged:" not in rendered
    assert "network_mode:" not in rendered
    assert "/var/run/" + "docker.sock" not in rendered
    assert "ports:" not in rendered


def test_development_agent_compose_preserves_explicit_environment_and_local_image() -> None:
    payload = request(
        node_agent_environment="development",
        node_agent_image="adojapan-restream-node:dev",
        control_url="http://backend:8000",
    )
    rendered = render_agent_compose(payload, facts())
    assert 'image: "adojapan-restream-node:dev"' in rendered
    assert "NODE_AGENT_ENVIRONMENT: development" in rendered


def test_ci_compose_validator_accepts_the_worker_renderer_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    validator = run_path(str(root / "ci" / "ssh-target" / "validate-node-compose.py"))
    rendered = render_agent_compose(
        request(
            node_agent_environment="test",
            node_agent_image=CI_IMAGE,
            control_url="http://backend:8000",
        ),
        facts(
            hostname="ci-ssh-target",
            os_name="debian",
            os_version="12",
        ),
    )
    validator["validate_document"](yaml.safe_load(rendered))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ports", ["9000:9000"]),
        ("network_mode", "host"),
    ],
)
def test_ci_compose_validator_rejects_agent_host_exposure(key: str, value: object) -> None:
    root = Path(__file__).resolve().parents[2]
    validator = run_path(str(root / "ci" / "ssh-target" / "validate-node-compose.py"))
    model = yaml.safe_load(
        render_agent_compose(
            request(
                node_agent_environment="test",
                node_agent_image=CI_IMAGE,
                control_url="http://backend:8000",
            ),
            facts(hostname="ci-ssh-target", os_name="debian", os_version="12"),
        )
    )
    model["services"]["agent"][key] = value

    with pytest.raises(SystemExit):
        validator["validate_document"](model)


async def test_installer_refuses_unknown_directory_before_upload() -> None:
    session = FakeSession(lambda command, stdin: RemoteResult(0, "conflict\n"))
    installer = RemoteNodeInstaller()
    with pytest.raises(BootstrapError) as captured:
        await installer.prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(recover_failed_install=True),
            facts(),
            enrollment_token=ENROLLMENT_TOKEN,
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "remote_directory_conflict"
    assert session.uploads == {}


@pytest.mark.parametrize(
    "collision_fragment",
    [
        "docker ps -aq --filter label=com.docker.compose.project=adojapan-restream-node",
        "docker network inspect adojapan-restream-node_default",
    ],
)
async def test_fresh_project_collision_fails_before_claim_or_compose_mutation(
    collision_fragment: str,
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        if "containers=$(docker ps" in command:
            assert collision_fragment in command
            return RemoteResult(1)
        raise AssertionError(f"unexpected command after collision: {command}")

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteNodeInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(),
            facts(),
            enrollment_token=ENROLLMENT_TOKEN,
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
        )

    assert captured.value.code == "remote_directory_conflict"
    commands = [command for command, _, _ in session.commands]
    assert all(" compose " not in command for command in commands)
    assert all("rm -rf -- /opt/adojapan-restream-node" not in command for command in commands)
    assert session.uploads == {}


async def test_fresh_project_is_rechecked_immediately_before_first_up() -> None:
    guard_calls = 0

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        nonlocal guard_calls
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        if "containers=$(docker ps" in command:
            guard_calls += 1
            return RemoteResult(0 if guard_calls == 1 else 1)
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    with pytest.raises(BootstrapError) as captured:
        await installer.install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt,
            timeouts=TimeoutPolicy(),
        )
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )

    assert captured.value.code == "remote_directory_conflict"
    assert guard_calls == 2
    commands = [command for command, _, _ in session.commands]
    assert all(" up -d " not in command for command in commands)
    assert all(" down" not in command for command in commands)


async def test_installer_uploads_token_only_to_dedicated_file_and_rolls_back_own_scope() -> None:
    marker = "token-marker-which-must-not-leak-1234567890"

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=SecretStr(marker),
        job_id=uuid4(),
        docker_installed=True,
        timeouts=TimeoutPolicy(),
    )
    token_uploads = [path for path in session.uploads if path.endswith("enrollment.token")]
    assert len(token_uploads) == 1
    assert session.uploads[token_uploads[0]] == (marker.encode(), 0o600)
    compose_upload = next(
        content for path, (content, _) in session.uploads.items() if path.endswith("compose.yml")
    )
    assert marker.encode() not in compose_upload
    assert all(marker not in command for command, _, _ in session.commands)

    receipt.files_applied = True
    receipt.managed_scope_acquired = True
    receipt.agent_start_attempted = True
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    rollback_commands = [command for command, _, _ in session.commands[-3:]]
    assert COMPOSE_PROJECT in rollback_commands[1]
    assert "/opt/adojapan-restream-node" in rollback_commands[2]
    assert ".managed-by-adojapan" in rollback_commands[2]
    assert all("docker prune" not in command for command in rollback_commands)
    assert all("apt-get" not in command for command in rollback_commands)
    assert all("systemctl" not in command for command in rollback_commands)


async def test_concurrent_fresh_root_acquisition_fails_without_overwrite_or_rollback() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        if "mkdir -m 0755 -- /opt/.adojapan-restream-node.claim-" in command:
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    with pytest.raises(BootstrapError) as captured:
        await installer.install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "remote_directory_conflict"
    assert receipt.managed_scope_acquired is False
    assert receipt.files_applied is False

    commands_before_rollback = len(session.commands)
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    assert len(commands) == commands_before_rollback
    assert (
        sum(
            "mkdir -m 0755 -- /opt/.adojapan-restream-node.claim-" in command
            for command in commands
        )
        == 1
    )
    publish = next(command for command in commands if "mv -Tn --" in command)
    assert f"{receipt.temp_root}/managed-marker" in publish
    assert f"{receipt.temp_root}/node-id" in publish
    assert "test ! -e /opt/.adojapan-restream-node.claim-" in publish
    assert all(" down" not in command for command in commands)


@pytest.mark.parametrize(
    "failure_fragment",
    ["/managed-marker", "/node-id", "mv -Tn --", "test ! -e /opt/.adojapan"],
)
async def test_atomic_fresh_identity_publish_failure_is_immediately_retryable(
    failure_fragment: str,
) -> None:
    publish_attempts = 0

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        nonlocal publish_attempts
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        if "mv -Tn -- /opt/.adojapan-restream-node.claim-" in command:
            publish_attempts += 1
            assert failure_fragment in command
            if publish_attempts == 1:
                return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    first = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    with pytest.raises(BootstrapError) as captured:
        await installer.install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            first,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "remote_directory_conflict"
    assert first.managed_scope_acquired is False
    assert first.files_applied is False
    failed_publish = next(command for command, _, _ in session.commands if "mv -Tn --" in command)
    assert "trap " in failed_publish
    assert "test ! -e /opt/.adojapan-restream-node.claim-" in failed_publish
    assert "test -f /opt/adojapan-restream-node/.node-id" in failed_publish

    retried = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        retried,
        timeouts=TimeoutPolicy(),
    )
    assert publish_attempts == 2
    assert retried.managed_scope_acquired is True
    assert retried.agent_start_attempted is True


@pytest.mark.parametrize(
    ("failure_fragment", "start_was_attempted"),
    [
        ("install -d -o 10001 -g 10001 -m 0700", False),
        ("/compose.yml /opt/adojapan-restream-node/compose.yml", False),
        ("/data/enrollment.token", False),
        (" config --quiet", False),
        (" up -d agent", True),
    ],
)
async def test_each_fresh_apply_substep_failure_rolls_back_and_is_immediately_retryable(
    failure_fragment: str,
    start_was_attempted: bool,
) -> None:
    injected = False

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        nonlocal injected
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        if not injected and failure_fragment in command:
            injected = True
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    first = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    with pytest.raises(BootstrapError) as captured:
        await installer.install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            first,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "agent_install_failed"
    assert injected is True
    assert first.managed_scope_acquired is True
    assert first.agent_start_attempted is start_was_attempted

    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        first,
        timeout=60,
    )
    assert first.rollback_succeeded is True
    commands = [command for command, _, _ in session.commands]
    assert any("rm -rf -- /opt/adojapan-restream-node" in command for command in commands)
    assert any(" down" in command for command in commands) is start_was_attempted

    retried = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        retried,
        timeouts=TimeoutPolicy(),
    )
    assert retried.managed_scope_acquired is True
    assert retried.agent_start_attempted is True


@pytest.mark.parametrize("adopted_empty_mountpoint", [False, True])
async def test_absent_rollback_rejects_mismatched_staged_node_id_before_destructive_command(
    adopted_empty_mountpoint: bool,
) -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.ABSENT,
        docker_installed=False,
        adopted_empty_mountpoint=adopted_empty_mountpoint,
        managed_scope_acquired=True,
        agent_start_attempted=True,
        files_applied=True,
        enrollment_completed=False,
    )

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "test -f /opt/adojapan-restream-node/.managed-by-adojapan" in command:
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    await RemoteNodeInstaller().rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    assert receipt.rollback_succeeded is False
    assert len(commands) == 1
    assert "/opt/adojapan-restream-node/.node-id" in commands[0]
    assert f"{receipt.temp_root}/node-id" in commands[0]
    assert " down" not in commands[0]
    assert "rm -rf -- /opt/adojapan-restream-node" not in commands[0]


@pytest.mark.parametrize("adopted_empty_mountpoint", [False, True])
async def test_absent_rollback_removes_only_exact_matching_staged_node_scope(
    adopted_empty_mountpoint: bool,
) -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.ABSENT,
        docker_installed=False,
        adopted_empty_mountpoint=adopted_empty_mountpoint,
        managed_scope_acquired=True,
        agent_start_attempted=True,
        files_applied=True,
        enrollment_completed=False,
    )
    session = FakeSession()
    await RemoteNodeInstaller().rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    assert receipt.rollback_succeeded is True
    assert len(commands) == 3
    stop, rollback = commands[1:]
    assert "/opt/adojapan-restream-node/.node-id" in stop
    assert f"{receipt.temp_root}/node-id" in stop
    assert " down" in stop
    assert "/opt/adojapan-restream-node/.node-id" in rollback
    assert f"{receipt.temp_root}/node-id" in rollback
    assert " down" not in rollback
    if adopted_empty_mountpoint:
        assert "rm -rf -- /opt/adojapan-restream-node/data" in rollback
        assert "rm -rf -- /opt/adojapan-restream-node;" not in rollback
    else:
        assert "rm -rf -- /opt/adojapan-restream-node;" in rollback


@pytest.mark.parametrize("adopted_empty_mountpoint", [False, True])
async def test_post_enrollment_absent_rollback_stops_agent_and_retains_exact_evidence(
    adopted_empty_mountpoint: bool,
) -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.ABSENT,
        docker_installed=False,
        adopted_empty_mountpoint=adopted_empty_mountpoint,
        managed_scope_acquired=True,
        agent_start_attempted=True,
        files_applied=True,
        enrollment_completed=True,
    )
    session = FakeSession()
    await RemoteNodeInstaller().rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    assert receipt.rollback_succeeded is True
    assert len(commands) == 2
    assert " down" in commands[1]
    assert all("rm -rf -- /opt/adojapan-restream-node" not in command for command in commands)
    assert all(
        "rm -f -- /opt/adojapan-restream-node/compose.yml" not in command for command in commands
    )


@pytest.mark.parametrize("adopted_empty_mountpoint", [False, True])
async def test_absent_rollback_preserves_evidence_when_scoped_compose_down_fails(
    adopted_empty_mountpoint: bool,
) -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.ABSENT,
        docker_installed=False,
        adopted_empty_mountpoint=adopted_empty_mountpoint,
        managed_scope_acquired=True,
        files_applied=True,
        enrollment_completed=True,
    )

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        return RemoteResult(1 if " down" in command else 0)

    session = FakeSession(responder)
    await RemoteNodeInstaller().rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    assert receipt.rollback_succeeded is False
    assert len(commands) == 2
    assert " down" in commands[1]
    assert "rm -rf -- /opt/adojapan-restream-node/data" not in commands[1]
    assert "rm -rf -- /opt/adojapan-restream-node;" not in commands[1]
    assert "rm -f -- /opt/adojapan-restream-node/compose.yml" not in commands[1]


async def test_managed_install_is_idempotent_for_same_already_enrolled_node() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "managed\n")
        if ".node-id" in command and "node.token" in command:
            return RemoteResult(0, "enrolled\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.ownership is InstallOwnership.MANAGED
    assert receipt.existing_enrolled is True
    assert receipt.rotate_existing_credential is False
    assert receipt.backup_permanent_path is None
    assert all(not path.endswith("enrollment.token") for path in session.uploads)

    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.enrollment_token_applied is False
    install_command = next(
        command for command, _, _ in session.commands if "managed-marker" in command
    )
    assert "node.token" in install_command
    assert "/enrollment.token" in install_command
    assert "install -o 10001 -g 10001 -m 0600" not in install_command
    assert '"$(cat /opt/adojapan-restream-node/.node-id)"' in install_command
    assert '/node-id)"' in install_command
    assert all("--force-recreate agent" not in command for command, _, _ in session.commands)


async def test_managed_install_rollback_restores_previous_compose_without_deleting_root() -> None:
    session = FakeSession()
    installer = RemoteNodeInstaller()
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.MANAGED,
        docker_installed=False,
        backup_path="/opt/adojapan-restream-node/.compose.rollback-test",
        files_applied=True,
    )
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    command = session.commands[-1][0]
    assert ".compose.rollback-test" in command
    assert "rm -rf -- /opt/adojapan-restream-node" not in command
    assert "rm -f -- /opt/adojapan-restream-node/data/enrollment.token" not in command
    assert "rm -f -- /opt/adojapan-restream-node/data/node.token" not in command
    assert f"{DOCKER_COMPOSE} -p {COMPOSE_PROJECT}" in command
    assert "/opt/adojapan-restream-node/.node-id" in command
    assert f"{receipt.temp_root}/node-id" in command


async def test_managed_pending_rollback_restores_prior_enrollment_token() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "managed\n")
        if ".node-id" in command and "node.token" in command:
            return RemoteResult(0, "pending_present\n")
        if "State.Status" in command:
            return RemoteResult(0, "running\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(recover_failed_install=True),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.existing_enrollment_token is True
    assert receipt.rotate_existing_credential is False
    assert receipt.backup_path is not None
    assert receipt.backup_enrollment_path is not None
    backup_command = next(
        command for command, _, _ in session.commands if ".compose.rollback-" in command
    )
    assert ".enrollment.rollback-" in backup_command
    assert "/data/enrollment.token" in backup_command

    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeouts=TimeoutPolicy(),
    )
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    command = session.commands[-1][0]
    assert ".enrollment.rollback-" in command
    assert "install -o 10001 -g 10001 -m 0600" in command
    assert "/opt/adojapan-restream-node/data/enrollment.token" in command
    assert "rm -rf -- /opt/adojapan-restream-node" not in command
    assert "rm -f -- /opt/adojapan-restream-node/data/node.token" in command
    assert "up -d --force-recreate agent" in command


async def test_managed_pending_state_change_fails_before_apply_and_restores_agent() -> None:
    stopped = False

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        nonlocal stopped
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "managed\n")
        if ".node-id" in command and "node.token" in command:
            return RemoteResult(0, "pending_present\n")
        if "State.Status" in command:
            return RemoteResult(0, "running\n")
        if "stop -t 45 agent" in command:
            stopped = True
            return RemoteResult(0)
        if (
            stopped
            and "test ! -e /opt/adojapan-restream-node/data/node.token" in command
            and "test -f /opt/adojapan-restream-node/data/enrollment.token" in command
            and "managed-marker" not in command
        ):
            return RemoteResult(1)
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    with pytest.raises(BootstrapError) as captured:
        await installer.install(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            receipt,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "credential_rotation_unavailable"
    assert receipt.files_applied is False
    assert any("up -d agent" in command for command, _, _ in session.commands)
    assert all("managed-marker" not in command for command, _, _ in session.commands)

    await installer.cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    assert receipt.backup_path is not None
    assert any(
        receipt.backup_path in command and "rm -f --" in command
        for command, _, _ in session.commands
    )


@pytest.mark.parametrize(
    ("previous_state", "restore_fragment", "forbidden_fragment"),
    [
        ("running", "up -d --force-recreate agent", " create agent"),
        ("stopped", " create agent", "up -d --force-recreate agent"),
        ("absent", "rm -f -s agent", "up -d --force-recreate agent"),
    ],
)
async def test_credential_rotation_restores_token_compose_and_process_state(
    previous_state: str,
    restore_fragment: str,
    forbidden_fragment: str,
) -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "managed\n")
        if ".node-id" in command and "node.token" in command:
            return RemoteResult(0, "enrolled\n")
        if "State.Status" in command:
            return RemoteResult(0, f"{previous_state}\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(recover_failed_install=True),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.rotate_existing_credential is True
    assert receipt.previous_agent_state is AgentProcessState(previous_state)
    assert receipt.backup_permanent_path is not None
    backup = next(
        command for command, _, _ in session.commands if ".node-token.rollback-" in command
    )
    assert "install -m 0600" in backup
    assert "/data/node.token" in backup
    assert any(path.endswith("enrollment.token") for path in session.uploads)

    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeouts=TimeoutPolicy(),
    )
    commands = [command for command, _, _ in session.commands]
    stop_index = next(
        index for index, command in enumerate(commands) if "stop -t 45 agent" in command
    )
    apply_index = next(
        index for index, command in enumerate(commands) if "managed-marker" in command
    )
    start_index = next(
        index for index, command in enumerate(commands) if "up -d --force-recreate agent" in command
    )
    assert stop_index < apply_index < start_index
    assert "rm -f -- /opt/adojapan-restream-node/data/node.token" in commands[apply_index]

    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    rollback = session.commands[-1][0]
    assert ".node-token.rollback-" in rollback
    assert ".compose.rollback-" in rollback
    assert "install -o 10001 -g 10001 -m 0600" in rollback
    assert restore_fragment in rollback
    if previous_state != "absent":
        assert forbidden_fragment not in rollback
    assert " down " not in rollback
    assert "prune" not in rollback

    await installer.cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    assert any(
        receipt.backup_permanent_path in command and "rm -f --" in command
        for command, _, _ in session.commands
    )


@pytest.mark.parametrize(
    "prior_failure",
    ["before_remote_install", "post_enrollment_rollback"],
)
async def test_failed_install_recovery_on_absent_root_uses_fresh_install(
    prior_failure: str,
) -> None:
    assert prior_failure

    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "absent\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(recover_failed_install=True),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.ownership is InstallOwnership.ABSENT
    assert receipt.rotate_existing_credential is False
    assert any(path.endswith("enrollment.token") for path in session.uploads)
    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeouts=TimeoutPolicy(),
    )
    assert any("up -d agent" in command for command, _, _ in session.commands)
    assert all("--force-recreate agent" not in command for command, _, _ in session.commands)
    assert all("stop -t 45 agent" not in command for command, _, _ in session.commands)


async def test_failed_install_recovery_rejects_managed_foreign_node_id() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "managed\n")
        if ".node-id" in command and "node.token" in command:
            return RemoteResult(0, "conflict\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteNodeInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(recover_failed_install=True),
            facts(),
            enrollment_token=ENROLLMENT_TOKEN,
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "remote_directory_conflict"
    assert session.uploads == {}


async def test_test_only_empty_mountpoint_can_be_adopted_and_is_preserved_on_rollback() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "conflict\n")
        if "find /opt/adojapan-restream-node -mindepth 1" in command:
            return RemoteResult(0, "empty\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    installer = RemoteNodeInstaller()
    receipt = await installer.prepare(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        request(
            node_agent_environment="test",
            control_url="http://backend:8000",
            adopt_empty_managed_root_for_test=True,
        ),
        facts(),
        enrollment_token=ENROLLMENT_TOKEN,
        job_id=uuid4(),
        docker_installed=False,
        timeouts=TimeoutPolicy(),
    )
    assert receipt.ownership is InstallOwnership.ABSENT
    assert receipt.adopted_empty_mountpoint is True
    await installer.install(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeouts=TimeoutPolicy(),
    )
    apply_command = next(
        command for command, _, _ in session.commands if "managed-marker" in command
    )
    assert "find /opt/adojapan-restream-node -mindepth 1 -maxdepth 1" in apply_command
    receipt.files_applied = True
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    rollback = session.commands[-1][0]
    assert "rm -rf -- /opt/adojapan-restream-node;" not in rollback
    assert "rm -rf -- /opt/adojapan-restream-node/data" in rollback
    assert "! -name data" in rollback


async def test_test_only_mountpoint_adoption_rejects_nonempty_or_symlink_root() -> None:
    def responder(command: str, stdin: SecretStr | None) -> RemoteResult:
        del stdin
        if "printf 'absent" in command:
            return RemoteResult(0, "conflict\n")
        if "find /opt/adojapan-restream-node -mindepth 1" in command:
            return RemoteResult(0, "conflict\n")
        return RemoteResult(0)

    session = FakeSession(responder)
    with pytest.raises(BootstrapError) as captured:
        await RemoteNodeInstaller().prepare(
            session,
            PrivilegeContext(PrivilegeMode.ROOT),
            request(
                node_agent_environment="test",
                control_url="http://backend:8000",
                adopt_empty_managed_root_for_test=True,
            ),
            facts(),
            enrollment_token=ENROLLMENT_TOKEN,
            job_id=uuid4(),
            docker_installed=False,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "remote_directory_conflict"
    assert session.uploads == {}


async def test_failed_rollback_preserves_secure_backups_for_manual_recovery() -> None:
    backup = "/opt/adojapan-restream-node/.compose.rollback-test"
    permanent_backup = "/opt/adojapan-restream-node/.node-token.rollback-test"
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.MANAGED,
        docker_installed=False,
        backup_path=backup,
        backup_permanent_path=permanent_backup,
        existing_enrolled=True,
        rotate_existing_credential=True,
        previous_agent_state=AgentProcessState.RUNNING,
        enrollment_token_applied=True,
        files_applied=True,
        enrollment_completed=True,
    )
    session = FakeSession(lambda command, stdin: RemoteResult(9))
    installer = RemoteNodeInstaller()
    await installer.rollback(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    assert receipt.rollback_succeeded is False
    await installer.cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    assert sum(backup in command for command, _, _ in session.commands) == 1
    assert sum(permanent_backup in command for command, _, _ in session.commands) == 1


async def test_committed_workflow_cleanup_removes_all_secure_backups() -> None:
    backup = "/opt/adojapan-restream-node/.compose.rollback-committed"
    enrollment_backup = "/opt/adojapan-restream-node/.enrollment.rollback-committed"
    permanent_backup = "/opt/adojapan-restream-node/.node-token.rollback-committed"
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - remote test path
        ownership=InstallOwnership.MANAGED,
        docker_installed=False,
        backup_path=backup,
        backup_enrollment_path=enrollment_backup,
        backup_permanent_path=permanent_backup,
        files_applied=True,
        enrollment_completed=True,
        workflow_committed=True,
    )
    session = FakeSession()
    await RemoteNodeInstaller().cleanup_temp(
        session,
        PrivilegeContext(PrivilegeMode.ROOT),
        receipt,
        timeout=60,
    )
    commands = [command for command, _, _ in session.commands]
    for path in (backup, enrollment_backup, permanent_backup):
        assert any(path in command and "rm -f --" in command for command in commands)
