from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError
from bootstrap_worker.installer import (
    AptDockerAdapter,
    DnfDockerAdapter,
    DockerBootstrap,
    PrivilegeContext,
)
from bootstrap_worker.models import (
    PackageManager,
    PlatformFamily,
    PrivilegeMode,
    SELinuxMode,
    SystemFacts,
    TimeoutPolicy,
)
from bootstrap_worker.ssh import RemoteResult

FIXTURE = Path(__file__).resolve().parents[2] / "ci" / "rpm-fixture"


def _facts(*, rhel: bool) -> SystemFacts:
    return SystemFacts(
        hostname="ci-platform",
        os_name="almalinux" if rhel else "ubuntu",
        os_id="almalinux" if rhel else "ubuntu",
        os_version="8.10" if rhel else "24.04",
        os_major_version="8" if rhel else "24",
        id_like=("rhel", "centos") if rhel else ("debian",),
        version_codename=None if rhel else "noble",
        architecture="amd64",
        platform_family=PlatformFamily.RHEL if rhel else PlatformFamily.DEBIAN,
        package_manager=PackageManager.DNF if rhel else PackageManager.APT,
        selinux_mode=SELinuxMode.ENFORCING if rhel else SELinuxMode.DISABLED,
        apt_get_available=not rhel,
        dpkg_query_available=not rhel,
        dnf_available=rhel,
        rpm_available=rhel,
        systemctl_available=True,
        cpu_count=2,
        memory_total_bytes=3 * 1024**3,
        memory_available_bytes=2 * 1024**3,
        disk_total_bytes=32 * 1024**3,
        disk_free_bytes=16 * 1024**3,
    )


def _fixture_environment(mode: str = "ready") -> dict[str, str]:
    environment = os.environ.copy()
    environment["ADOJAPAN_RPM_FIXTURE_MODE"] = mode
    return environment


def _run_tool(
    tool: str,
    *arguments: str,
    mode: str = "ready",
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    assert shell is not None
    return subprocess.run(  # noqa: S603 - fixed local CI fixture and allowlisted argv
        [shell, str(FIXTURE / tool), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=_fixture_environment(mode),
        timeout=10,
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX fixture runs in Linux CI")
def test_rpm_fixture_proves_official_and_conflicting_package_states() -> None:
    adapter = DnfDockerAdapter()
    assert "rpm -q" in adapter.supported_packages_check()
    assert "rpm -q" in adapter.conflicting_runtime_check()
    for package in (
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ):
        assert _run_tool("rpm", "-q", package).returncode == 0
        assert _run_tool("rpm", "-q", package, mode="absent").returncode != 0
    assert _run_tool("rpm", "-q", "podman-docker").returncode != 0
    assert _run_tool("rpm", "-q", "podman-docker", mode="conflict").returncode == 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX fixture runs in Linux CI")
def test_dnf_fixture_accepts_only_the_adapter_package_commands() -> None:
    commands = (
        "dnf -y --setopt=install_weak_deps=False install ca-certificates curl gnupg2",
        "dnf -q --disablerepo='*' --enablerepo=docker-ce-stable makecache",
        "dnf -y --setopt=install_weak_deps=False install docker-ce docker-ce-cli "
        "containerd.io docker-buildx-plugin docker-compose-plugin",
    )
    assert all(_run_tool("dnf", *shlex.split(command)[1:]).returncode == 0 for command in commands)
    for package in (
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ):
        arguments = (
            "-q",
            "--disablerepo=*",
            "--enablerepo=docker-ce-stable",
            "list",
            "--available",
            package,
        )
        assert _run_tool("dnf", *arguments).returncode == 0
        incomplete = _run_tool(
            "dnf",
            *arguments,
            mode="alma_native_repo_incomplete",
        )
        if package in {"docker-ce", "docker-ce-cli"}:
            assert incomplete.returncode != 0
        else:
            assert incomplete.returncode == 0
    assert _run_tool("dnf", "-y", "update").returncode == 64
    assert _run_tool("dnf", "-y", "remove", "podman", "runc").returncode == 64


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX fixture runs in Linux CI")
async def test_alma_native_incomplete_fixture_stops_before_dnf_engine_install() -> None:
    platform = _facts(rhel=True)

    class FixtureSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def run(
            self,
            command: str,
            *,
            stdin: SecretStr | None = None,
            timeout: float,
        ) -> RemoteResult:
            del stdin, timeout
            self.commands.append(command)
            if "repo_kind=$(kind" in command:
                return RemoteResult(
                    0,
                    "repo_kind=missing\nrepo_uid=-1\nrepo_sha256=\n"
                    "key_kind=missing\nkey_uid=-1\nkey_fingerprint=\n"
                    "foreign_repository=0\ntemporary_artifact=0\n",
                )
            if "list --available" in command:
                results = (
                    _run_tool(
                        "dnf",
                        "-q",
                        "--disablerepo=*",
                        "--enablerepo=docker-ce-stable",
                        "list",
                        "--available",
                        package,
                        mode="alma_native_repo_incomplete",
                    )
                    for package in (
                        "docker-ce",
                        "docker-ce-cli",
                        "containerd.io",
                        "docker-buildx-plugin",
                        "docker-compose-plugin",
                    )
                )
                return RemoteResult(1 if any(item.returncode != 0 for item in results) else 0)
            if (
                "install docker-ce docker-ce-cli containerd.io "
                "docker-buildx-plugin docker-compose-plugin" in command
            ):
                raise AssertionError("engine package installation must not run")
            return RemoteResult(0)

    session = FixtureSession()
    with pytest.raises(BootstrapError) as captured:
        await DockerBootstrap().install(
            session,  # type: ignore[arg-type]
            PrivilegeContext(PrivilegeMode.ROOT),
            platform,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "docker_repository_incomplete"
    assert not any(
        "install docker-ce docker-ce-cli containerd.io "
        "docker-buildx-plugin docker-compose-plugin" in command
        for command in session.commands
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX fixture runs in Linux CI")
def test_rpm_systemctl_fixture_has_a_bounded_surface() -> None:
    assert _run_tool("systemctl", "is-active", "--quiet", "docker").returncode == 0
    assert _run_tool("systemctl", "enable", "--now", "docker").returncode == 0
    assert _run_tool("systemctl", "restart", "docker").returncode == 64


@pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="generated remote shell requires a POSIX parser",
)
def test_generated_apt_and_dnf_plans_have_valid_posix_shell_syntax() -> None:
    shell = shutil.which("sh")
    assert shell is not None
    plans = (
        AptDockerAdapter().install_plan(_facts(rhel=False)),
        DnfDockerAdapter().install_plan(_facts(rhel=True)),
    )
    for plan in plans:
        for step in plan:
            result = subprocess.run(  # noqa: S603 - fixed generated commands, parse-only
                [shell, "-n", "-c", step.command],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
