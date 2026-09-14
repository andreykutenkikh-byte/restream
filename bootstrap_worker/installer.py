"""Fixed, idempotent remote preflight and Node Agent installation workflow."""

from __future__ import annotations

import json
import shlex
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError, safe_failure
from bootstrap_worker.models import (
    BootstrapRequest,
    DockerDisposition,
    InstallOwnership,
    PackageManager,
    PlatformFamily,
    PrivilegeMode,
    SELinuxMode,
    SystemFacts,
    TargetIdentity,
    TimeoutPolicy,
)
from bootstrap_worker.ssh import RemoteResult, RemoteSession

MANAGED_ROOT = "/opt/adojapan-restream-node"
MANAGED_MARKER = f"{MANAGED_ROOT}/.managed-by-adojapan"
NODE_ID_PATH = f"{MANAGED_ROOT}/.node-id"
COMPOSE_PATH = f"{MANAGED_ROOT}/compose.yml"
DATA_ROOT = f"{MANAGED_ROOT}/data"
ENROLLMENT_TOKEN_PATH = f"{DATA_ROOT}/enrollment.token"
PERMANENT_TOKEN_PATH = f"{DATA_ROOT}/node.token"
COMPOSE_PROJECT = "adojapan-restream-node"
COMPOSE_NETWORK = f"{COMPOSE_PROJECT}_default"
COMPOSE_DATA_VOLUME = f"{COMPOSE_PROJECT}_data"
DOCKER_COMPOSE = "docker " + "compose"
MARKER_CONTENT = "adojapan-restream-node:v1"
NODE_UID = 10_001
MIN_CPU_COUNT = 1
MIN_AVAILABLE_MEMORY_BYTES = 700 * 1024 * 1024
MIN_FREE_DISK_BYTES = 8 * 1024 * 1024 * 1024
DOCKER_GPG_FINGERPRINT = "060A61C51B558A7F742B77AAC52FEB6B621E9F35"

_SYSTEM_PROBE = r"""set -eu
test -r /etc/os-release
printf 'hostname='; hostname
os_id=$(sed -n 's/^ID=//p' /etc/os-release | head -n 1 | tr -d '"')
os_version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | head -n 1 | tr -d '"')
id_like=$(sed -n 's/^ID_LIKE=//p' /etc/os-release | head -n 1 | tr -d '"')
version_codename=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | head -n 1 | tr -d '"')
printf 'os_id=%s\n' "$os_id"
printf 'os_version=%s\n' "$os_version"
printf 'id_like=%s\n' "$id_like"
printf 'version_codename=%s\n' "$version_codename"
printf 'architecture='; uname -m
for capability in apt-get dpkg-query dnf rpm systemctl; do
  key=$(printf '%s' "$capability" | tr '-' '_')
  if command -v "$capability" >/dev/null 2>&1; then
    printf '%s_available=1\n' "$key"
  else
    printf '%s_available=0\n' "$key"
  fi
done
if command -v getenforce >/dev/null 2>&1; then
  printf 'selinux_mode='; getenforce | tr '[:upper:]' '[:lower:]'
elif [ -r /sys/fs/selinux/enforce ]; then
  if [ "$(cat /sys/fs/selinux/enforce)" = 1 ]; then
    printf 'selinux_mode=enforcing\n'
  else
    printf 'selinux_mode=permissive\n'
  fi
else
  printf 'selinux_mode=disabled\n'
fi
printf 'cpu_count='; getconf _NPROCESSORS_ONLN
printf 'memory_total_bytes='; awk '/^MemTotal:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo
printf 'memory_available_bytes='; awk '/^MemAvailable:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo
printf 'disk_total_bytes='; df -PB1 / | awk 'NR == 2 {print $2}'
printf 'disk_free_bytes='; df -PB1 / | awk 'NR == 2 {print $4}'
"""

_COMMON_DOCKER_ABSENCE_CHECK = (
    "if command -v docker >/dev/null 2>&1; then exit 1; fi; "
    "if systemctl list-unit-files docker.service --no-legend 2>/dev/null "
    "| grep -q '^docker.service'; then exit 1; fi; "
    "test ! -e /etc/docker && test ! -e /var/lib/docker && "
    "test ! -e /etc/containerd && test ! -e /var/lib/containerd && "
    "test ! -e /run/docker.sock"
)

_APT_OFFICIAL_PACKAGES = (
    "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
)
_APT_CONFLICTING_PACKAGES = "docker.io containerd runc podman-docker"
_RPM_OFFICIAL_PACKAGES = _APT_OFFICIAL_PACKAGES
_RPM_CONFLICTING_PACKAGES = (
    "docker docker-client docker-common docker-engine podman-docker containerd runc"
)


@dataclass(slots=True)
class PrivilegeContext:
    mode: PrivilegeMode
    password: SecretStr | None = field(default=None, repr=False)

    async def run(
        self,
        session: RemoteSession,
        command: str,
        *,
        timeout: float,
    ) -> RemoteResult:
        quoted = shlex.quote(command)
        if self.mode is PrivilegeMode.ROOT:
            invocation = f"env LC_ALL=C sh -c {quoted}"
            secret = None
        elif self.mode is PrivilegeMode.PASSWORDLESS_SUDO:
            invocation = f"sudo -n -p '' -- env LC_ALL=C sh -c {quoted}"
            secret = None
        else:
            if self.password is None:
                raise safe_failure("sudo_password_invalid")
            invocation = f"sudo -S -p '' -- env LC_ALL=C sh -c {quoted}"
            secret = self.password
        return await session.run(invocation, stdin=secret, timeout=timeout)

    def clear(self) -> None:
        self.password = None


async def detect_privilege(
    session: RemoteSession,
    ssh_password: SecretStr,
    *,
    timeout: float,
) -> PrivilegeContext | None:
    identity = await session.run("id -u", timeout=timeout)
    if identity.exit_status != 0:
        raise safe_failure("remote_command_failed")
    if identity.stdout.strip() == "0":
        return PrivilegeContext(PrivilegeMode.ROOT)

    passwordless = await session.run("sudo -n -p '' true", timeout=timeout)
    if passwordless.exit_status == 0:
        return PrivilegeContext(PrivilegeMode.PASSWORDLESS_SUDO)

    password_sudo = await session.run(
        "sudo -S -p '' true",
        stdin=ssh_password,
        timeout=timeout,
    )
    if password_sudo.exit_status == 0:
        return PrivilegeContext(PrivilegeMode.PASSWORD_SUDO, ssh_password)
    return None


async def verify_sudo_password(
    session: RemoteSession,
    sudo_password: SecretStr,
    *,
    timeout: float,
) -> PrivilegeContext | None:
    result = await session.run(
        "sudo -S -p '' true",
        stdin=sudo_password,
        timeout=timeout,
    )
    if result.exit_status != 0:
        return None
    return PrivilegeContext(PrivilegeMode.PASSWORD_SUDO, sudo_password)


def parse_system_facts(output: str) -> SystemFacts:
    if len(output) > 8192:
        raise safe_failure("remote_command_failed")
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "hostname",
            "os_id",
            "os_version",
            "id_like",
            "version_codename",
            "architecture",
            "apt_get_available",
            "dpkg_query_available",
            "dnf_available",
            "rpm_available",
            "systemctl_available",
            "selinux_mode",
            "cpu_count",
            "memory_total_bytes",
            "memory_available_bytes",
            "disk_total_bytes",
            "disk_free_bytes",
        }:
            values[key] = value.strip()
    try:
        os_id = values["os_id"].lower()
        os_version = values["os_version"]
        os_major_version = os_version.split(".", 1)[0]
        selinux_value = values.get("selinux_mode", SELinuxMode.UNKNOWN.value).lower()
        try:
            selinux_mode = SELinuxMode(selinux_value)
        except ValueError:
            selinux_mode = SELinuxMode.UNKNOWN
        return SystemFacts(
            hostname=values["hostname"],
            os_name=os_id,
            os_id=os_id,
            os_version=os_version,
            os_major_version=os_major_version,
            id_like=tuple(item.lower() for item in values.get("id_like", "").split()),
            version_codename=values.get("version_codename") or None,
            architecture=values["architecture"].lower(),
            platform_family=None,
            package_manager=None,
            selinux_mode=selinux_mode,
            apt_get_available=values["apt_get_available"] == "1",
            dpkg_query_available=values["dpkg_query_available"] == "1",
            dnf_available=values["dnf_available"] == "1",
            rpm_available=values["rpm_available"] == "1",
            systemctl_available=values["systemctl_available"] == "1",
            cpu_count=int(values["cpu_count"]),
            memory_total_bytes=int(values["memory_total_bytes"]),
            memory_available_bytes=int(values["memory_available_bytes"]),
            disk_total_bytes=int(values["disk_total_bytes"]),
            disk_free_bytes=int(values["disk_free_bytes"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise safe_failure("remote_command_failed") from exc


async def probe_system(session: RemoteSession, *, timeout: float) -> SystemFacts:
    result = await session.run(_SYSTEM_PROBE, timeout=timeout)
    if result.exit_status != 0:
        raise safe_failure("remote_command_failed")
    return parse_system_facts(result.stdout)


def validate_operating_system(facts: SystemFacts) -> SystemFacts:
    supported_exact_versions = {
        "ubuntu": {"22.04", "24.04", "26.04"},
        "debian": {"12", "13"},
    }
    supported_major_versions = {
        "almalinux": {"8", "9"},
        "rocky": {"8", "9"},
        "rhel": {"8", "9"},
        "centos": {"9"},
    }
    if (
        (
            facts.os_id in supported_exact_versions
            and facts.os_version not in supported_exact_versions[facts.os_id]
        )
        or (
            facts.os_id in supported_major_versions
            and facts.os_major_version not in supported_major_versions[facts.os_id]
        )
        or facts.os_id not in supported_exact_versions | supported_major_versions
        or facts.architecture not in {"x86_64", "amd64"}
    ):
        raise safe_failure("unsupported_operating_system")

    if facts.os_id in {"ubuntu", "debian"}:
        if not (facts.dpkg_query_available and facts.systemctl_available):
            raise safe_failure("unsupported_package_manager")
        platform_family = PlatformFamily.DEBIAN
        package_manager = PackageManager.APT
    else:
        if not (facts.rpm_available and facts.systemctl_available):
            raise safe_failure("unsupported_package_manager")
        platform_family = PlatformFamily.RHEL
        package_manager = PackageManager.DNF

    return facts.model_copy(
        update={
            "architecture": "amd64",
            "platform_family": platform_family,
            "package_manager": package_manager,
        }
    )


def validate_resources(facts: SystemFacts) -> SystemFacts:
    if facts.cpu_count < MIN_CPU_COUNT:
        raise safe_failure("insufficient_cpu")
    if facts.memory_available_bytes < MIN_AVAILABLE_MEMORY_BYTES:
        available_mib = facts.memory_available_bytes // (1024 * 1024)
        raise BootstrapError(
            "insufficient_memory",
            f"Недостаточно свободной памяти. Доступно {available_mib} МБ, требуется 700 МБ.",
        )
    if facts.disk_free_bytes < MIN_FREE_DISK_BYTES:
        available_gib = facts.disk_free_bytes / (1024**3)
        raise BootstrapError(
            "insufficient_disk",
            f"Недостаточно свободного места. Доступно {available_gib:.1f} ГБ, требуется 8 ГБ.",
        )
    return facts


def validate_supported_system(facts: SystemFacts) -> SystemFacts:
    """Compatibility helper which applies both ordered preflight gates."""

    return validate_resources(validate_operating_system(facts))


@dataclass(frozen=True, slots=True)
class DockerInstallStep:
    command: str
    package_operation: bool = False
    failure_code: str = "docker_install_failed"


class DockerPlatformAdapter(Protocol):
    platform_family: PlatformFamily
    package_manager: PackageManager

    def conflicting_runtime_check(self) -> str: ...

    def clean_absence_check(self) -> str: ...

    def supported_packages_check(self) -> str: ...

    def repository_probe_command(self, facts: SystemFacts) -> str: ...

    def install_plan(self, facts: SystemFacts) -> tuple[DockerInstallStep, ...]: ...


class AptDockerAdapter:
    platform_family = PlatformFamily.DEBIAN
    package_manager = PackageManager.APT

    @staticmethod
    def conflicting_runtime_check() -> str:
        return (
            f"for package in {_APT_CONFLICTING_PACKAGES}; do "
            "if dpkg-query -W -f='${db:Status-Abbrev}\\n' \"$package\" 2>/dev/null "
            "| grep -q '^ii '; then exit 1; fi; done"
        )

    @staticmethod
    def clean_absence_check() -> str:
        return (
            f"for package in {_APT_OFFICIAL_PACKAGES}; do "
            "if dpkg-query -W -f='${db:Status-Abbrev}\\n' \"$package\" 2>/dev/null "
            "| grep -q '^ii '; then exit 1; fi; done; "
            "if grep -Rqs 'download.docker.com' /etc/apt/sources.list "
            "/etc/apt/sources.list.d 2>/dev/null; then exit 1; fi; "
            "if find /etc/apt/sources.list.d -maxdepth 1 -type f "
            "-iname '*docker*.list' -print -quit 2>/dev/null | grep -q .; then exit 1; fi; "
            f"{_COMMON_DOCKER_ABSENCE_CHECK} && "
            "test ! -e /etc/apt/sources.list.d/docker.list && "
            "test ! -e /etc/apt/keyrings/docker.asc && "
            "test ! -e /etc/apt/keyrings/docker.asc.adojapan-tmp"
        )

    @staticmethod
    def supported_packages_check() -> str:
        return (
            f"for package in {_APT_OFFICIAL_PACKAGES}; do "
            "dpkg-query -W -f='${db:Status-Abbrev}\\n' \"$package\" 2>/dev/null "
            "| grep -q '^ii ' || exit 1; done"
        )

    @staticmethod
    def _repository_coordinates(facts: SystemFacts) -> tuple[str, str]:
        codename_by_release = {
            ("ubuntu", "22.04"): "jammy",
            ("ubuntu", "24.04"): "noble",
            ("ubuntu", "26.04"): "resolute",
            ("debian", "12"): "bookworm",
            ("debian", "13"): "trixie",
        }
        try:
            codename = codename_by_release[(facts.os_id, facts.os_version)]
        except KeyError as exc:
            raise safe_failure("unsupported_operating_system") from exc
        return facts.os_id, codename

    def repository_probe_command(self, facts: SystemFacts) -> str:
        distribution, _ = self._repository_coordinates(facts)
        gpg_url = f"https://download.docker.com/linux/{distribution}/gpg"
        return (
            "curl --fail --silent --show-error --proto '=https' --tlsv1.2 "
            "--connect-timeout 10 --max-time 20 "
            f"{shlex.quote(gpg_url)} --output /dev/null"
        )

    def install_plan(self, facts: SystemFacts) -> tuple[DockerInstallStep, ...]:
        distribution, codename = self._repository_coordinates(facts)
        gpg_url = f"https://download.docker.com/linux/{distribution}/gpg"
        repository = (
            "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] "
            f"https://download.docker.com/linux/{distribution} {codename} stable"
        )
        temporary_key = "/etc/apt/keyrings/docker.asc.adojapan-tmp"
        verify_key = (
            f"fingerprints=$(gpg --batch --with-colons --show-keys {temporary_key} "
            '2>/dev/null | awk -F: \'$1 == "pub" {primary=1; next} '
            'primary && $1 == "fpr" {print $10; primary=0}\') && '
            f'if [ "$fingerprints" = {DOCKER_GPG_FINGERPRINT} ]; then '
            f"install -m 0644 {temporary_key} /etc/apt/keyrings/docker.asc && "
            f"rm -f {temporary_key}; else rm -f {temporary_key}; exit 1; fi"
        )
        return (
            DockerInstallStep("apt-get update", package_operation=True),
            DockerInstallStep(
                "apt-get install -y --no-install-recommends ca-certificates curl gnupg",
                package_operation=True,
            ),
            DockerInstallStep(
                self.repository_probe_command(facts),
                failure_code="docker_repository_unavailable",
            ),
            DockerInstallStep("install -d -m 0755 /etc/apt/keyrings"),
            DockerInstallStep(
                f"rm -f {temporary_key} && "
                "curl --fail --silent --show-error --proto '=https' --tlsv1.2 "
                "--connect-timeout 10 --max-time 20 "
                f"{shlex.quote(gpg_url)} --output {temporary_key}",
                failure_code="docker_repository_unavailable",
            ),
            DockerInstallStep(verify_key, failure_code="docker_repository_key_invalid"),
            DockerInstallStep(
                f"printf '%s\\n' {shlex.quote(repository)} > /etc/apt/sources.list.d/docker.list"
            ),
            DockerInstallStep("apt-get update", package_operation=True),
            DockerInstallStep(
                "apt-get install -y --no-install-recommends docker-ce docker-ce-cli "
                "containerd.io docker-buildx-plugin docker-compose-plugin",
                package_operation=True,
            ),
            *DockerBootstrap.daemon_activation_steps(),
        )


class DnfDockerAdapter:
    platform_family = PlatformFamily.RHEL
    package_manager = PackageManager.DNF

    @staticmethod
    def conflicting_runtime_check() -> str:
        return (
            f"for package in {_RPM_CONFLICTING_PACKAGES}; do "
            'if rpm -q "$package" >/dev/null 2>&1; then exit 1; fi; done'
        )

    @staticmethod
    def clean_absence_check() -> str:
        return (
            f"for package in {_RPM_OFFICIAL_PACKAGES}; do "
            'if rpm -q "$package" >/dev/null 2>&1; then exit 1; fi; done; '
            "if grep -Rqs 'download.docker.com' /etc/yum.repos.d 2>/dev/null; "
            "then exit 1; fi; "
            "if find /etc/yum.repos.d -maxdepth 1 -type f "
            "-iname '*docker*.repo' -print -quit 2>/dev/null | grep -q .; then exit 1; fi; "
            f"{_COMMON_DOCKER_ABSENCE_CHECK} && "
            "test ! -e /etc/yum.repos.d/docker-ce.repo && "
            "test ! -e /etc/pki/rpm-gpg/docker-ce.asc && "
            "test ! -e /etc/pki/rpm-gpg/docker-ce.asc.adojapan-tmp"
        )

    @staticmethod
    def supported_packages_check() -> str:
        return (
            f"for package in {_RPM_OFFICIAL_PACKAGES}; do "
            'rpm -q "$package" >/dev/null 2>&1 || exit 1; done'
        )

    @staticmethod
    def _repository_distribution(facts: SystemFacts) -> str:
        distribution_by_os = {
            "almalinux": "alma",
            "rocky": "rocky",
            "rhel": "rhel",
            "centos": "centos",
        }
        try:
            return distribution_by_os[facts.os_id]
        except KeyError as exc:
            raise safe_failure("unsupported_operating_system") from exc

    def repository_probe_command(self, facts: SystemFacts) -> str:
        distribution = self._repository_distribution(facts)
        repository_url = f"https://download.docker.com/linux/{distribution}/docker-ce.repo"
        return (
            "curl --fail --silent --show-error --proto '=https' --tlsv1.2 "
            "--connect-timeout 10 --max-time 20 "
            f"{repository_url} --output /dev/null"
        )

    def install_plan(self, facts: SystemFacts) -> tuple[DockerInstallStep, ...]:
        if facts.platform_family is not PlatformFamily.RHEL:
            raise safe_failure("unsupported_operating_system")
        distribution = self._repository_distribution(facts)
        gpg_url = f"https://download.docker.com/linux/{distribution}/gpg"
        repository = "\n".join(
            (
                "[docker-ce-stable]",
                "name=Docker CE Stable",
                f"baseurl=https://download.docker.com/linux/{distribution}/"
                "$releasever/$basearch/stable",
                "enabled=1",
                "gpgcheck=1",
                "gpgkey=file:///etc/pki/rpm-gpg/docker-ce.asc",
            )
        )
        temporary_key = "/etc/pki/rpm-gpg/docker-ce.asc.adojapan-tmp"
        verify_key = (
            f"fingerprints=$(gpg2 --batch --with-colons --show-keys {temporary_key} "
            '2>/dev/null | awk -F: \'$1 == "pub" {primary=1; next} '
            'primary && $1 == "fpr" {print $10; primary=0}\') && '
            f'if [ "$fingerprints" = {DOCKER_GPG_FINGERPRINT} ]; then '
            f"install -m 0644 {temporary_key} /etc/pki/rpm-gpg/docker-ce.asc && "
            f"rm -f {temporary_key}; else rm -f {temporary_key}; exit 1; fi"
        )
        return (
            DockerInstallStep(
                "dnf -y --setopt=install_weak_deps=False install ca-certificates curl gnupg2",
                package_operation=True,
            ),
            DockerInstallStep(
                self.repository_probe_command(facts),
                failure_code="docker_repository_unavailable",
            ),
            DockerInstallStep("install -d -m 0755 /etc/pki/rpm-gpg"),
            DockerInstallStep(
                f"rm -f {temporary_key} && "
                "curl --fail --silent --show-error --proto '=https' --tlsv1.2 "
                "--connect-timeout 10 --max-time 20 "
                f"{gpg_url} --output {temporary_key}",
                failure_code="docker_repository_unavailable",
            ),
            DockerInstallStep(verify_key, failure_code="docker_repository_key_invalid"),
            DockerInstallStep(
                f"printf '%s\\n' {shlex.quote(repository)} > /etc/yum.repos.d/docker-ce.repo"
            ),
            DockerInstallStep(
                "dnf -q --disablerepo='*' --enablerepo=docker-ce-stable makecache",
                package_operation=True,
                failure_code="docker_repository_unavailable",
            ),
            DockerInstallStep(
                "dnf -y --setopt=install_weak_deps=False install docker-ce docker-ce-cli "
                "containerd.io docker-buildx-plugin docker-compose-plugin",
                package_operation=True,
            ),
            *DockerBootstrap.daemon_activation_steps(),
        )


class DockerBootstrap:
    """Select a strict platform adapter, inspect read-only, or install official Docker."""

    _adapters: dict[tuple[PlatformFamily, PackageManager], DockerPlatformAdapter] = {
        (PlatformFamily.DEBIAN, PackageManager.APT): AptDockerAdapter(),
        (PlatformFamily.RHEL, PackageManager.DNF): DnfDockerAdapter(),
    }

    @staticmethod
    def daemon_activation_steps() -> tuple[DockerInstallStep, ...]:
        return (
            DockerInstallStep("systemctl enable --now docker"),
            DockerInstallStep("docker version --format '{{.Server.Version}}' >/dev/null"),
            DockerInstallStep(f"{DOCKER_COMPOSE} version --short >/dev/null"),
            DockerInstallStep("systemctl is-active --quiet docker"),
        )

    def adapter_for(self, facts: SystemFacts) -> DockerPlatformAdapter:
        if facts.platform_family is None or facts.package_manager is None:
            raise safe_failure("unsupported_package_manager")
        try:
            return self._adapters[(facts.platform_family, facts.package_manager)]
        except KeyError as exc:
            raise safe_failure("unsupported_package_manager") from exc

    @staticmethod
    def assert_install_manager_available(facts: SystemFacts) -> None:
        availability = {
            PackageManager.APT: facts.apt_get_available,
            PackageManager.DNF: facts.dnf_available,
        }
        if facts.package_manager is None or not availability.get(facts.package_manager, False):
            raise safe_failure("unsupported_package_manager")

    async def _assert_no_conflicting_runtime(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        adapter: DockerPlatformAdapter,
        *,
        timeout: float,
    ) -> None:
        result = await privilege.run(
            session,
            adapter.conflicting_runtime_check(),
            timeout=timeout,
        )
        if result.exit_status != 0:
            raise safe_failure("conflicting_container_runtime")

    async def inspect(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        facts: SystemFacts,
        *,
        timeout: float,
    ) -> DockerDisposition:
        adapter = self.adapter_for(facts)
        await self._assert_no_conflicting_runtime(
            session,
            privilege,
            adapter,
            timeout=timeout,
        )
        present = await session.run("command -v docker >/dev/null 2>&1", timeout=timeout)
        if present.exit_status != 0:
            absence = await privilege.run(
                session,
                adapter.clean_absence_check(),
                timeout=timeout,
            )
            if absence.exit_status != 0:
                return DockerDisposition.UNSUPPORTED
            self.assert_install_manager_available(facts)
            return DockerDisposition.ABSENT
        checks = (
            adapter.supported_packages_check(),
            "docker version --format '{{.Server.Version}}' >/dev/null",
            f"{DOCKER_COMPOSE} version --short >/dev/null",
            "systemctl is-active --quiet docker",
        )
        for command in checks:
            result = await privilege.run(session, command, timeout=timeout)
            if result.exit_status != 0:
                return DockerDisposition.UNSUPPORTED
        return DockerDisposition.READY

    def repository_probe_command(self, facts: SystemFacts) -> str:
        return self.adapter_for(facts).repository_probe_command(facts)

    def install_plan(self, facts: SystemFacts) -> tuple[DockerInstallStep, ...]:
        return self.adapter_for(facts).install_plan(facts)

    async def install(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        facts: SystemFacts,
        *,
        timeouts: TimeoutPolicy,
    ) -> None:
        adapter = self.adapter_for(facts)
        self.assert_install_manager_available(facts)
        await self._assert_no_conflicting_runtime(
            session,
            privilege,
            adapter,
            timeout=timeouts.command_seconds,
        )
        absence = await privilege.run(
            session,
            adapter.clean_absence_check(),
            timeout=timeouts.command_seconds,
        )
        if absence.exit_status != 0:
            raise safe_failure("unsupported_docker_installation")
        for step in adapter.install_plan(facts):
            timeout = (
                timeouts.package_seconds if step.package_operation else timeouts.command_seconds
            )
            result = await privilege.run(session, step.command, timeout=timeout)
            if result.exit_status != 0:
                raise safe_failure(step.failure_code)


@dataclass(slots=True)
class InstallReceipt:
    temp_root: str
    ownership: InstallOwnership
    docker_installed: bool
    identity_claim_root: str | None = None
    backup_path: str | None = None
    backup_enrollment_path: str | None = None
    backup_permanent_path: str | None = None
    existing_enrolled: bool = False
    existing_enrollment_token: bool = False
    rotate_existing_credential: bool = False
    previous_agent_state: AgentProcessState | None = None
    adopted_empty_mountpoint: bool = False
    managed_scope_acquired: bool = False
    agent_start_attempted: bool = False
    enrollment_token_applied: bool = False
    files_applied: bool = False
    enrollment_completed: bool = False
    workflow_committed: bool = False
    rollback_succeeded: bool = False


class AgentProcessState(StrEnum):
    ABSENT = "absent"
    RUNNING = "running"
    STOPPED = "stopped"


def render_agent_compose(request: BootstrapRequest, facts: SystemFacts) -> str:
    image = json.dumps(request.node_agent_image)
    control_url = json.dumps(request.control_url)
    hostname = json.dumps(facts.hostname)
    os_name = json.dumps(facts.os_id)
    os_version = json.dumps(facts.os_version)
    architecture = json.dumps(facts.architecture)
    environment_line = (
        f"      NODE_AGENT_ENVIRONMENT: {request.node_agent_environment}\n"
        if request.node_agent_environment != "production"
        else ""
    )
    return f"""services:
  agent:
    image: {image}
    user: \"{NODE_UID}:{NODE_UID}\"
    restart: unless-stopped
    stop_grace_period: 45s
    read_only: true
    environment:
      NODE_CONTROL_URL: {control_url}
      NODE_DATA_DIR: /var/lib/adojapan-node
      NODE_HOSTNAME: {hostname}
      NODE_OS_NAME: {os_name}
      NODE_OS_VERSION: {os_version}
      NODE_ARCHITECTURE: {architecture}
{environment_line.rstrip()}
    volumes:
      - type: bind
        source: ./data
        target: /var/lib/adojapan-node
        bind:
          create_host_path: false
          selinux: Z
    tmpfs:
      - /tmp:size=32m,mode=1777
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    cpus: \"0.25\"
    mem_limit: 256m
    pids_limit: 128
"""


class RemoteNodeInstaller:
    """Install only the marker-owned Compose project and roll back that scope."""

    async def inspect_ownership(
        self,
        session: RemoteSession,
        *,
        timeout: float,
    ) -> InstallOwnership:
        marker = shlex.quote(MANAGED_MARKER)
        root = shlex.quote(MANAGED_ROOT)
        expected = shlex.quote(MARKER_CONTENT)
        command = (
            f"if [ ! -e {root} ] && [ ! -L {root} ]; then printf 'absent\\n'; "
            f"elif [ -d {root} ] && [ ! -L {root} ] && [ -f {marker} ] && "
            f'[ ! -L {marker} ] && [ "$(cat {marker})" = {expected} ]; '
            "then printf 'managed\\n'; else printf 'conflict\\n'; fi"
        )
        result = await session.run(command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("remote_command_failed")
        try:
            return InstallOwnership(result.stdout.strip())
        except ValueError as exc:
            raise safe_failure("remote_command_failed") from exc

    async def inspect_agent_process_state(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> AgentProcessState:
        compose = f"{DOCKER_COMPOSE} -p {COMPOSE_PROJECT} -f {shlex.quote(COMPOSE_PATH)}"
        command = (
            f"containers=$({compose} ps --all --quiet agent) || exit 1; "
            "set -- $containers; "
            "if [ \"$#\" -eq 0 ]; then printf 'absent\\n'; "
            "elif [ \"$#\" -ne 1 ]; then printf 'conflict\\n'; "
            "else state=$(docker inspect --format '{{.State.Status}}' \"$1\") || exit 1; "
            "case \"$state\" in running|restarting) printf 'running\\n' ;; "
            "exited|created) printf 'stopped\\n' ;; *) printf 'conflict\\n' ;; esac; fi"
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0 or result.stdout.strip() == "conflict":
            raise safe_failure("credential_rotation_unavailable")
        try:
            return AgentProcessState(result.stdout.strip())
        except ValueError as exc:
            raise safe_failure("credential_rotation_unavailable") from exc

    async def inspect_empty_managed_root(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> bool:
        root = shlex.quote(MANAGED_ROOT)
        result = await privilege.run(
            session,
            f"if test -d {root} && test ! -L {root} && "
            f'test -z "$(find {root} -mindepth 1 -maxdepth 1 -print -quit)"; '
            "then printf 'empty\\n'; else printf 'conflict\\n'; fi",
            timeout=timeout,
        )
        if result.exit_status != 0:
            raise safe_failure("remote_command_failed")
        return result.stdout.strip() == "empty"

    @staticmethod
    def fresh_project_guard_command() -> str:
        label = f"com.docker.compose.project={COMPOSE_PROJECT}"
        container_name = f"^/{COMPOSE_PROJECT}-agent-1$"
        return (
            f"containers=$(docker ps -aq --filter label={shlex.quote(label)}) && "
            f"networks=$(docker network ls -q --filter label={shlex.quote(label)}) && "
            f"volumes=$(docker volume ls -q --filter label={shlex.quote(label)}) && "
            f"reserved=$(docker ps -aq --filter name={shlex.quote(container_name)}) && "
            f"! docker network inspect {shlex.quote(COMPOSE_NETWORK)} >/dev/null 2>&1 && "
            f"! docker volume inspect {shlex.quote(COMPOSE_DATA_VOLUME)} >/dev/null 2>&1 && "
            'test -z "$containers$networks$volumes$reserved"'
        )

    async def assert_fresh_project_available(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        *,
        timeout: float,
    ) -> None:
        result = await privilege.run(
            session,
            self.fresh_project_guard_command(),
            timeout=timeout,
        )
        if result.exit_status != 0:
            raise safe_failure("remote_directory_conflict")

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
    ) -> InstallReceipt:
        del target
        ownership = await self.inspect_ownership(session, timeout=timeouts.command_seconds)
        adopted_empty_mountpoint = False
        if ownership is InstallOwnership.CONFLICT:
            if not request.adopt_empty_managed_root_for_test:
                raise safe_failure("remote_directory_conflict")
            adopted_empty_mountpoint = await self.inspect_empty_managed_root(
                session,
                privilege,
                timeout=timeouts.command_seconds,
            )
            if not adopted_empty_mountpoint:
                raise safe_failure("remote_directory_conflict")
            ownership = InstallOwnership.ABSENT
        if ownership is InstallOwnership.ABSENT:
            await self.assert_fresh_project_available(
                session,
                privilege,
                timeout=timeouts.command_seconds,
            )
        temp_root = f"/tmp/adojapan-bootstrap-{job_id}"  # noqa: S108 - UUID-scoped remote dir
        quoted_temp = shlex.quote(temp_root)
        created = await session.run(
            f"rm -rf -- {quoted_temp} && install -d -m 0700 -- {quoted_temp}",
            timeout=timeouts.command_seconds,
        )
        if created.exit_status != 0:
            raise safe_failure("remote_command_failed")
        receipt = InstallReceipt(
            temp_root,
            ownership,
            docker_installed,
            identity_claim_root=(
                f"{MANAGED_ROOT}/.identity.claim-{job_id}"
                if ownership is InstallOwnership.ABSENT and adopted_empty_mountpoint
                else f"/opt/.adojapan-restream-node.claim-{job_id}"
                if ownership is InstallOwnership.ABSENT
                else None
            ),
            adopted_empty_mountpoint=adopted_empty_mountpoint,
        )
        try:
            await self._stage_files(
                session,
                privilege,
                request,
                facts,
                receipt,
                enrollment_token=enrollment_token,
                job_id=job_id,
                timeouts=timeouts,
            )
        except BaseException:
            await self.cleanup_temp(
                session,
                privilege,
                receipt,
                timeout=timeouts.command_seconds,
            )
            raise
        return receipt

    async def _stage_files(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        request: BootstrapRequest,
        facts: SystemFacts,
        receipt: InstallReceipt,
        *,
        enrollment_token: SecretStr,
        job_id: UUID,
        timeouts: TimeoutPolicy,
    ) -> None:
        if receipt.ownership is InstallOwnership.MANAGED:
            backup_path = f"{MANAGED_ROOT}/.compose.rollback-{job_id}"
            enrollment_backup_path = f"{MANAGED_ROOT}/.enrollment.rollback-{job_id}"
            permanent_backup_path = f"{MANAGED_ROOT}/.node-token.rollback-{job_id}"
            identity = await privilege.run(
                session,
                f"test -f {shlex.quote(NODE_ID_PATH)} && "
                f"test ! -L {shlex.quote(NODE_ID_PATH)} && "
                f'test "$(cat {shlex.quote(NODE_ID_PATH)})" = '
                f"{shlex.quote(str(request.node_id))} && "
                f"if test -f {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)}; then "
                "printf 'enrolled\\n'; "
                f"elif test ! -e {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test -f {shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
                f"test ! -L {shlex.quote(ENROLLMENT_TOKEN_PATH)}; then "
                "printf 'pending_present\\n'; "
                f"elif test ! -e {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)}; then "
                "printf 'pending_absent\\n'; else printf 'conflict\\n'; fi",
                timeout=timeouts.command_seconds,
            )
            if identity.exit_status != 0 or identity.stdout.strip() == "conflict":
                raise safe_failure("remote_directory_conflict")
            if identity.stdout.strip() not in {
                "enrolled",
                "pending_absent",
                "pending_present",
            }:
                raise safe_failure("remote_directory_conflict")
            receipt.existing_enrolled = identity.stdout.strip() == "enrolled"
            receipt.existing_enrollment_token = identity.stdout.strip() == "pending_present"
            receipt.rotate_existing_credential = (
                request.recover_failed_install and receipt.existing_enrolled
            )
            if receipt.rotate_existing_credential or not receipt.existing_enrolled:
                receipt.previous_agent_state = await self.inspect_agent_process_state(
                    session,
                    privilege,
                    timeout=timeouts.command_seconds,
                )

            backup_preconditions: list[str] = []
            backup_copies: list[str] = []
            backup_paths = [backup_path]
            if receipt.existing_enrollment_token:
                backup_paths.append(enrollment_backup_path)
                backup_preconditions.extend(
                    (
                        f"test -f {shlex.quote(ENROLLMENT_TOKEN_PATH)}",
                        f"test ! -L {shlex.quote(ENROLLMENT_TOKEN_PATH)}",
                        f"test ! -e {shlex.quote(enrollment_backup_path)}",
                        f"test ! -L {shlex.quote(enrollment_backup_path)}",
                    )
                )
                backup_copies.append(
                    f"install -m 0600 {shlex.quote(ENROLLMENT_TOKEN_PATH)} "
                    f"{shlex.quote(enrollment_backup_path)}"
                )
            if receipt.rotate_existing_credential:
                backup_paths.append(permanent_backup_path)
                backup_preconditions.extend(
                    (
                        f"test -f {shlex.quote(PERMANENT_TOKEN_PATH)}",
                        f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)}",
                        f"test ! -e {shlex.quote(permanent_backup_path)}",
                        f"test ! -L {shlex.quote(permanent_backup_path)}",
                    )
                )
                backup_copies.append(
                    f"install -m 0600 {shlex.quote(PERMANENT_TOKEN_PATH)} "
                    f"{shlex.quote(permanent_backup_path)}"
                )
            precondition_command = " && ".join(backup_preconditions)
            if precondition_command:
                precondition_command += " && "
            copy_command = " && ".join(backup_copies)
            if copy_command:
                copy_command += " && "
            cleanup_command = "rm -f -- " + " ".join(shlex.quote(path) for path in backup_paths)
            backup = await privilege.run(
                session,
                f"test -f {shlex.quote(COMPOSE_PATH)} && "
                f"test ! -L {shlex.quote(COMPOSE_PATH)} && "
                f"test ! -e {shlex.quote(backup_path)} && "
                f"test ! -L {shlex.quote(backup_path)} && "
                f"{precondition_command}"
                f"trap {shlex.quote(cleanup_command)} EXIT HUP INT TERM && "
                f"install -m 0600 {shlex.quote(COMPOSE_PATH)} "
                f"{shlex.quote(backup_path)} && {copy_command}"
                "trap - EXIT HUP INT TERM",
                timeout=timeouts.command_seconds,
            )
            if backup.exit_status != 0:
                raise safe_failure("remote_directory_conflict")
            receipt.backup_path = backup_path
            if receipt.existing_enrollment_token:
                receipt.backup_enrollment_path = enrollment_backup_path
            if receipt.rotate_existing_credential:
                receipt.backup_permanent_path = permanent_backup_path

        token = enrollment_token.get_secret_value().encode("utf-8")
        try:
            await session.put(
                f"{receipt.temp_root}/compose.yml",
                render_agent_compose(request, facts).encode("utf-8"),
                mode=0o600,
                timeout=timeouts.command_seconds,
            )
            await session.put(
                f"{receipt.temp_root}/managed-marker",
                f"{MARKER_CONTENT}\n".encode(),
                mode=0o600,
                timeout=timeouts.command_seconds,
            )
            await session.put(
                f"{receipt.temp_root}/node-id",
                f"{request.node_id}\n".encode(),
                mode=0o600,
                timeout=timeouts.command_seconds,
            )
            if not receipt.existing_enrolled or receipt.rotate_existing_credential:
                await session.put(
                    f"{receipt.temp_root}/enrollment.token",
                    token,
                    mode=0o600,
                    timeout=timeouts.command_seconds,
                )
        finally:
            token = b""

    async def install(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: InstallReceipt,
        *,
        timeouts: TimeoutPolicy,
    ) -> None:
        root = shlex.quote(MANAGED_ROOT)
        marker = shlex.quote(MANAGED_MARKER)
        temp = shlex.quote(receipt.temp_root)
        compose = f"{DOCKER_COMPOSE} -p {COMPOSE_PROJECT} -f {shlex.quote(COMPOSE_PATH)}"
        identity_guard = (
            f"test -d {root} && test ! -L {root} && test -f {marker} && "
            f'test ! -L {marker} && test "$(cat {marker})" = '
            f"{shlex.quote(MARKER_CONTENT)} && "
            f"test -f {shlex.quote(NODE_ID_PATH)} && "
            f"test ! -L {shlex.quote(NODE_ID_PATH)} && "
            f'test "$(cat {shlex.quote(NODE_ID_PATH)})" = '
            f'"$(cat {temp}/node-id)"'
        )
        empty_root_guard = (
            f"test -d {root} && test ! -L {root} && "
            f'test -z "$(find {root} -mindepth 1 -maxdepth 1 -print -quit)"'
        )
        new_install = receipt.ownership is InstallOwnership.ABSENT
        if receipt.ownership is InstallOwnership.ABSENT:
            if receipt.identity_claim_root is None:
                raise safe_failure("remote_directory_conflict")
            claim_root = shlex.quote(receipt.identity_claim_root)
            claim_marker = shlex.quote(f"{receipt.identity_claim_root}/.managed-by-adojapan")
            claim_node_id = shlex.quote(f"{receipt.identity_claim_root}/.node-id")
            if receipt.adopted_empty_mountpoint:
                claim_cleanup = (
                    f"if test -f {marker} && test ! -L {marker} && "
                    f'test "$(cat {marker})" = {shlex.quote(MARKER_CONTENT)}; then '
                    f"rm -f -- {marker}; fi; "
                    f"if test -f {shlex.quote(NODE_ID_PATH)} && "
                    f"test ! -L {shlex.quote(NODE_ID_PATH)} && "
                    f'test "$(cat {shlex.quote(NODE_ID_PATH)})" = '
                    f'"$(cat {temp}/node-id)"; then '
                    f"rm -f -- {shlex.quote(NODE_ID_PATH)}; fi; "
                    f"rm -rf -- {claim_root}"
                )
                publish = (
                    f"{empty_root_guard} && mkdir -m 0700 -- {claim_root} && "
                    f"trap {shlex.quote(claim_cleanup)} EXIT HUP INT TERM && "
                    f"install -m 0644 {temp}/managed-marker {claim_marker} && "
                    f"install -m 0644 {temp}/node-id {claim_node_id} && "
                    f'test "$(cat {claim_marker})" = {shlex.quote(MARKER_CONTENT)} && '
                    f'test "$(cat {claim_node_id})" = "$(cat {temp}/node-id)" && '
                    f"ln -- {claim_marker} {marker} && "
                    f"ln -- {claim_node_id} {shlex.quote(NODE_ID_PATH)} && "
                    f"{identity_guard} && rm -rf -- {claim_root} && "
                    "trap - EXIT HUP INT TERM"
                )
            else:
                claim_cleanup = (
                    f"if test ! -e {claim_root} && {identity_guard}; then "
                    f"rm -rf -- {root}; fi; rm -rf -- {claim_root}"
                )
                publish = (
                    f"test ! -e {root} && test ! -L {root} && "
                    f"test ! -e {claim_root} && test ! -L {claim_root} && "
                    f"mkdir -m 0755 -- {claim_root} && "
                    f"trap {shlex.quote(claim_cleanup)} EXIT HUP INT TERM && "
                    f"install -m 0644 {temp}/managed-marker {claim_marker} && "
                    f"install -m 0644 {temp}/node-id {claim_node_id} && "
                    f'test "$(cat {claim_marker})" = {shlex.quote(MARKER_CONTENT)} && '
                    f'test "$(cat {claim_node_id})" = "$(cat {temp}/node-id)" && '
                    f"mv -Tn -- {claim_root} {root} && test ! -e {claim_root} && "
                    f"{identity_guard} && trap - EXIT HUP INT TERM"
                )
            acquired = await privilege.run(
                session,
                publish,
                timeout=timeouts.command_seconds,
            )
            if acquired.exit_status != 0:
                raise safe_failure("remote_directory_conflict")
            receipt.managed_scope_acquired = True
            receipt.files_applied = True
            guard = identity_guard
        else:
            guard = (
                f"{identity_guard} && "
                f"test -f {shlex.quote(COMPOSE_PATH)} && "
                f"test ! -L {shlex.quote(COMPOSE_PATH)} && "
                f"test -d {shlex.quote(DATA_ROOT)} && test ! -L {shlex.quote(DATA_ROOT)}"
            )
        if receipt.rotate_existing_credential:
            if receipt.backup_permanent_path is None or receipt.previous_agent_state is None:
                raise safe_failure("credential_rotation_unavailable")
            permanent_backup = shlex.quote(receipt.backup_permanent_path)
            token_action = (
                f"test -f {permanent_backup} && test ! -L {permanent_backup} && "
                f"test -f {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
                f"install -o {NODE_UID} -g {NODE_UID} -m 0600 {temp}/enrollment.token "
                f"{shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
                f"rm -f -- {shlex.quote(PERMANENT_TOKEN_PATH)}"
            )
        elif receipt.existing_enrolled:
            token_action = (
                f"test -f {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)}"
            )
        else:
            token_action = (
                f"test ! -e {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                f"(test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)} || "
                f"(test -f {shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
                f"test ! -L {shlex.quote(ENROLLMENT_TOKEN_PATH)})) && "
                f"install -o {NODE_UID} -g {NODE_UID} -m 0600 {temp}/enrollment.token "
                f"{shlex.quote(ENROLLMENT_TOKEN_PATH)}"
            )
        apply_commands: tuple[str, ...]
        if new_install:
            apply_commands = (
                f"{guard} && install -d -o {NODE_UID} -g {NODE_UID} -m 0700 "
                f"{shlex.quote(DATA_ROOT)}",
                f"{guard} && test -d {shlex.quote(DATA_ROOT)} && "
                f"test ! -L {shlex.quote(DATA_ROOT)} && "
                f"install -m 0644 {temp}/compose.yml {shlex.quote(COMPOSE_PATH)}",
                f"{guard} && test -f {shlex.quote(COMPOSE_PATH)} && "
                f"test ! -L {shlex.quote(COMPOSE_PATH)} && {token_action}",
            )
        else:
            apply_commands = (
                f"{guard} && "
                f"install -m 0644 {temp}/managed-marker {marker} && "
                f"install -d -o {NODE_UID} -g {NODE_UID} -m 0700 "
                f"{shlex.quote(DATA_ROOT)} && "
                f"install -m 0644 {temp}/compose.yml {shlex.quote(COMPOSE_PATH)} && "
                f"install -m 0644 {temp}/node-id {shlex.quote(NODE_ID_PATH)} && "
                f"{token_action}",
            )
        managed_token_restart = receipt.ownership is InstallOwnership.MANAGED and (
            not receipt.existing_enrolled or receipt.rotate_existing_credential
        )
        if managed_token_restart:
            if receipt.previous_agent_state is None:
                raise safe_failure("credential_rotation_unavailable")
            stopped = await privilege.run(
                session,
                f"{compose} stop -t 45 agent",
                timeout=timeouts.command_seconds,
            )
            if stopped.exit_status != 0:
                if receipt.previous_agent_state is AgentProcessState.RUNNING:
                    with suppress(BootstrapError):
                        await privilege.run(
                            session,
                            f"{compose} up -d agent",
                            timeout=timeouts.command_seconds,
                        )
                raise safe_failure("agent_install_failed")
            if receipt.rotate_existing_credential:
                token_precondition = (
                    f"test -f {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                    f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                    f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)}"
                )
            elif receipt.existing_enrollment_token:
                token_precondition = (
                    f"test ! -e {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                    f"test -f {shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
                    f"test ! -L {shlex.quote(ENROLLMENT_TOKEN_PATH)}"
                )
            else:
                token_precondition = (
                    f"test ! -e {shlex.quote(PERMANENT_TOKEN_PATH)} && "
                    f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)}"
                )
            unchanged = await privilege.run(
                session,
                token_precondition,
                timeout=timeouts.command_seconds,
            )
            if unchanged.exit_status != 0:
                if receipt.previous_agent_state is AgentProcessState.RUNNING:
                    with suppress(BootstrapError):
                        await privilege.run(
                            session,
                            f"{compose} up -d agent",
                            timeout=timeouts.command_seconds,
                        )
                raise safe_failure("credential_rotation_unavailable")
        if not new_install:
            receipt.files_applied = True
        receipt.enrollment_token_applied = (
            not receipt.existing_enrolled or receipt.rotate_existing_credential
        )
        for command in apply_commands:
            result = await privilege.run(session, command, timeout=timeouts.command_seconds)
            if result.exit_status != 0:
                raise safe_failure("agent_install_failed")
        if receipt.adopted_empty_mountpoint:
            receipt.managed_scope_acquired = True

        start_command = (
            f"{compose} up -d --force-recreate agent"
            if managed_token_restart
            else f"{compose} up -d agent"
        )
        result = await privilege.run(
            session,
            f"{compose} config --quiet",
            timeout=timeouts.package_seconds,
        )
        if result.exit_status != 0:
            raise safe_failure("agent_install_failed")
        if new_install:
            # Recheck immediately before the first Compose mutation. This
            # catches a project collision which appeared after staging but
            # before the reserved project name would be claimed.
            await self.assert_fresh_project_available(
                session,
                privilege,
                timeout=timeouts.command_seconds,
            )
        receipt.agent_start_attempted = True
        result = await privilege.run(session, start_command, timeout=timeouts.package_seconds)
        if result.exit_status != 0:
            raise safe_failure("agent_install_failed")

    async def final_check(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: InstallReceipt,
        *,
        timeout: float,
    ) -> None:
        command = (
            f"test -f {shlex.quote(MANAGED_MARKER)} && "
            f'test "$(cat {shlex.quote(MANAGED_MARKER)})" = '
            f"{shlex.quote(MARKER_CONTENT)} && "
            f"test ! -e {shlex.quote(ENROLLMENT_TOKEN_PATH)} && "
            f"test -f {shlex.quote(PERMANENT_TOKEN_PATH)} && "
            f"test ! -L {shlex.quote(PERMANENT_TOKEN_PATH)} && "
            f'test -n "$({DOCKER_COMPOSE} -p {COMPOSE_PROJECT} '
            f'-f {shlex.quote(COMPOSE_PATH)} ps --status running --quiet agent)"'
        )
        result = await privilege.run(session, command, timeout=timeout)
        if result.exit_status != 0:
            raise safe_failure("agent_enrollment_failed")
        receipt.enrollment_completed = True

    async def rollback(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: InstallReceipt,
        *,
        timeout: float,
    ) -> None:
        if not receipt.files_applied:
            return
        marker_guard = (
            f"test -f {shlex.quote(MANAGED_MARKER)} && "
            f"test ! -L {shlex.quote(MANAGED_MARKER)} && "
            f'test "$(cat {shlex.quote(MANAGED_MARKER)})" = '
            f"{shlex.quote(MARKER_CONTENT)}"
        )
        staged_node_id = shlex.quote(f"{receipt.temp_root}/node-id")
        managed_guard = (
            f"{marker_guard} && test -f {shlex.quote(NODE_ID_PATH)} && "
            f"test ! -L {shlex.quote(NODE_ID_PATH)} && "
            f"test -f {staged_node_id} && test ! -L {staged_node_id} && "
            f'test "$(cat {shlex.quote(NODE_ID_PATH)})" = "$(cat {staged_node_id})"'
        )
        compose = f"{DOCKER_COMPOSE} -p {COMPOSE_PROJECT} -f {shlex.quote(COMPOSE_PATH)}"
        if receipt.ownership is InstallOwnership.ABSENT:
            if not receipt.managed_scope_acquired:
                return
            try:
                ownership_check = await privilege.run(
                    session,
                    managed_guard,
                    timeout=timeout,
                )
            except BootstrapError:
                return
            if ownership_check.exit_status != 0:
                return
            if receipt.agent_start_attempted or receipt.enrollment_completed:
                stop_command = f"if {managed_guard}; then {compose} down; else exit 1; fi"
                try:
                    stopped = await privilege.run(session, stop_command, timeout=timeout)
                except BootstrapError:
                    return
                if stopped.exit_status != 0:
                    return
            if receipt.enrollment_completed:
                # Enrollment is the deletion boundary. The backend revokes the
                # credential, while the exact managed files remain as evidence
                # and as the recoverable source for a controlled retry.
                receipt.rollback_succeeded = True
                return
            if receipt.adopted_empty_mountpoint:
                unexpected = (
                    f"find {shlex.quote(MANAGED_ROOT)} -mindepth 1 -maxdepth 1 "
                    f"! -name {shlex.quote('.managed-by-adojapan')} "
                    f"! -name {shlex.quote('.node-id')} "
                    f"! -name {shlex.quote('compose.yml')} "
                    f"! -name {shlex.quote('data')} -print -quit"
                )
                command = (
                    f"if {managed_guard} && "
                    f'test -z "$({unexpected})"; then '
                    f"rm -rf -- {shlex.quote(DATA_ROOT)}; "
                    f"rm -f -- {shlex.quote(COMPOSE_PATH)} "
                    f"{shlex.quote(NODE_ID_PATH)} {shlex.quote(MANAGED_MARKER)}; "
                    "else exit 1; fi"
                )
            else:
                command = (
                    f"if {managed_guard}; then rm -rf -- {shlex.quote(MANAGED_ROOT)}; "
                    "else exit 1; fi"
                )
        else:
            if receipt.backup_path is None:
                return
            previous = shlex.quote(receipt.backup_path)
            if receipt.rotate_existing_credential:
                if receipt.backup_permanent_path is None or receipt.previous_agent_state is None:
                    return
                permanent_backup = shlex.quote(receipt.backup_permanent_path)
                permanent = shlex.quote(PERMANENT_TOKEN_PATH)
                enrollment = shlex.quote(ENROLLMENT_TOKEN_PATH)
                if receipt.previous_agent_state is AgentProcessState.RUNNING:
                    restore_process = f"{compose} up -d --force-recreate agent"
                elif receipt.previous_agent_state is AgentProcessState.STOPPED:
                    restore_process = f"{compose} rm -f -s agent && {compose} create agent"
                else:
                    restore_process = f"{compose} rm -f -s agent"
                command = (
                    f"if {managed_guard} && test -f {previous} && test ! -L {previous} && "
                    f"test -f {permanent_backup} && test ! -L {permanent_backup}; then "
                    f"{compose} stop -t 45 agent || exit 1; "
                    f"rm -f -- {enrollment} {permanent} || exit 1; "
                    f"install -o {NODE_UID} -g {NODE_UID} -m 0600 "
                    f"{permanent_backup} {permanent} || exit 1; "
                    f"install -m 0644 {previous} {shlex.quote(COMPOSE_PATH)} || exit 1; "
                    f"{restore_process}; else exit 1; fi"
                )
            elif receipt.enrollment_token_applied:
                if receipt.previous_agent_state is None:
                    return
                permanent = shlex.quote(PERMANENT_TOKEN_PATH)
                enrollment = shlex.quote(ENROLLMENT_TOKEN_PATH)
                if receipt.existing_enrollment_token:
                    if receipt.backup_enrollment_path is None:
                        return
                    enrollment_backup = shlex.quote(receipt.backup_enrollment_path)
                    token_rollback = (
                        f"test -f {enrollment_backup} && "
                        f"test ! -L {enrollment_backup} && "
                        f"rm -f -- {permanent} {enrollment} && "
                        f"install -o {NODE_UID} -g {NODE_UID} -m 0600 "
                        f"{enrollment_backup} {enrollment} || exit 1; "
                    )
                else:
                    token_rollback = f"rm -f -- {permanent} {enrollment} || exit 1; "
                if receipt.previous_agent_state is AgentProcessState.RUNNING:
                    restore_process = f"{compose} up -d --force-recreate agent"
                elif receipt.previous_agent_state is AgentProcessState.STOPPED:
                    restore_process = f"{compose} rm -f -s agent && {compose} create agent"
                else:
                    restore_process = f"{compose} rm -f -s agent"
                command = (
                    f"if {managed_guard} && test -f {previous} && test ! -L {previous}; then "
                    + token_rollback
                    + f"install -m 0644 {previous} {shlex.quote(COMPOSE_PATH)} || exit 1; "
                    f"{restore_process}; else exit 1; fi"
                )
            else:
                token_rollback = ""
            if not receipt.rotate_existing_credential and not receipt.enrollment_token_applied:
                command = (
                    f"if {managed_guard} && test -f {previous} && test ! -L {previous}; then "
                    + token_rollback
                    + f"install -m 0644 {previous} {shlex.quote(COMPOSE_PATH)} || exit 1; "
                    f"{compose} up -d agent; else exit 1; fi"
                )
        # Rollback is best effort, deliberately scoped by the marker. Its raw
        # output is discarded and never replaces the original safe failure.
        try:
            result = await privilege.run(session, command, timeout=timeout)
        except BootstrapError:
            return
        if result.exit_status == 0:
            receipt.rollback_succeeded = True

    async def cleanup_temp(
        self,
        session: RemoteSession,
        privilege: PrivilegeContext,
        receipt: InstallReceipt,
        *,
        timeout: float,
    ) -> None:
        with suppress(BootstrapError):
            await session.run(
                f"rm -rf -- {shlex.quote(receipt.temp_root)}",
                timeout=timeout,
            )
        cleanup_backups = (
            receipt.workflow_committed or not receipt.files_applied or receipt.rollback_succeeded
        )
        if cleanup_backups and receipt.backup_path is not None:
            with suppress(BootstrapError):
                await privilege.run(
                    session,
                    f"if test -f {shlex.quote(MANAGED_MARKER)} && "
                    f"test ! -L {shlex.quote(MANAGED_MARKER)} && "
                    f'test "$(cat {shlex.quote(MANAGED_MARKER)})" = '
                    f"{shlex.quote(MARKER_CONTENT)}; then "
                    f"rm -f -- {shlex.quote(receipt.backup_path)}; fi",
                    timeout=timeout,
                )
        if cleanup_backups and receipt.backup_enrollment_path is not None:
            with suppress(BootstrapError):
                await privilege.run(
                    session,
                    f"if test -f {shlex.quote(MANAGED_MARKER)} && "
                    f"test ! -L {shlex.quote(MANAGED_MARKER)} && "
                    f'test "$(cat {shlex.quote(MANAGED_MARKER)})" = '
                    f"{shlex.quote(MARKER_CONTENT)}; then "
                    f"rm -f -- {shlex.quote(receipt.backup_enrollment_path)}; fi",
                    timeout=timeout,
                )
        if cleanup_backups and receipt.backup_permanent_path is not None:
            with suppress(BootstrapError):
                await privilege.run(
                    session,
                    f"if test -f {shlex.quote(MANAGED_MARKER)} && "
                    f"test ! -L {shlex.quote(MANAGED_MARKER)} && "
                    f'test "$(cat {shlex.quote(MANAGED_MARKER)})" = '
                    f"{shlex.quote(MARKER_CONTENT)}; then "
                    f"rm -f -- {shlex.quote(receipt.backup_permanent_path)}; fi",
                    timeout=timeout,
                )


__all__ = [
    "AptDockerAdapter",
    "COMPOSE_PATH",
    "COMPOSE_PROJECT",
    "DATA_ROOT",
    "DOCKER_COMPOSE",
    "DOCKER_GPG_FINGERPRINT",
    "DnfDockerAdapter",
    "DockerBootstrap",
    "DockerInstallStep",
    "ENROLLMENT_TOKEN_PATH",
    "InstallReceipt",
    "AgentProcessState",
    "MANAGED_MARKER",
    "MANAGED_ROOT",
    "MARKER_CONTENT",
    "NODE_ID_PATH",
    "PERMANENT_TOKEN_PATH",
    "PrivilegeContext",
    "RemoteNodeInstaller",
    "detect_privilege",
    "parse_system_facts",
    "probe_system",
    "render_agent_compose",
    "validate_operating_system",
    "validate_resources",
    "validate_supported_system",
    "verify_sudo_password",
]
