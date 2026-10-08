"""Coexistence with a distro Docker used by another application such as Amnezia."""

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess

import pytest
from test_bootstrap_worker_installer import FakeSession, facts
from test_bootstrap_worker_jobs import (
    FakeConnector,
    FakeDocker,
    FakeInstaller,
    make_request,
    persist_verified_host_key,
    policy,
    provide_jit_enrollment_token,
    wait_for_state,
)
from test_bootstrap_worker_jobs import (
    FakeSession as JobSession,
)

from bootstrap_worker import compose_plugin
from bootstrap_worker.errors import BootstrapError
from bootstrap_worker.installer import AptDockerAdapter, DockerBootstrap, PrivilegeContext
from bootstrap_worker.jobs import BootstrapExecutor, JobStore
from bootstrap_worker.models import DockerDisposition, JobState, PrivilegeMode, TimeoutPolicy
from bootstrap_worker.ssh import RemoteResult


async def test_existing_distro_docker_reuses_daemon_and_only_adds_missing_compose():
    added = False

    def respond(command, _):
        nonlocal added
        assert "apt-get" not in command and "dnf " not in command
        assert "systemctl start" not in command and "systemctl restart" not in command
        assert "systemctl enable" not in command
        assert "for package in docker.io containerd runc podman-docker" not in command
        if "/releases/download/" in command:
            added = True
        if "compose version --short >/dev/null" in command:
            return RemoteResult(0 if added else 1)
        return RemoteResult(0)

    session, docker = FakeSession(respond), DockerBootstrap()
    privilege = PrivilegeContext(PrivilegeMode.ROOT)
    assert (
        await docker.inspect(session, privilege, facts(), timeout=60)
        is DockerDisposition.COMPOSE_MISSING
    )
    await docker.install_compose(session, privilege, facts(), timeouts=TimeoutPolicy())
    assert added
    assert await docker.inspect(session, privilege, facts(), timeout=60) is DockerDisposition.READY
    downloads = sum("/releases/download/" in c for c, _, _ in session.commands)
    await docker.install_compose(session, privilege, facts(), timeouts=TimeoutPolicy())
    assert sum("/releases/download/" in c for c, _, _ in session.commands) == downloads == 1


@pytest.mark.parametrize(
    "failed_check", ["podman-docker", "docker context inspect", "SecurityOptions", "is-active"]
)
async def test_unsafe_or_stopped_existing_runtime_never_downloads_or_installs(failed_check):
    session = FakeSession(lambda command, _: RemoteResult(1 if failed_check in command else 0))
    docker, privilege = DockerBootstrap(), PrivilegeContext(PrivilegeMode.ROOT)
    assert (
        await docker.inspect(session, privilege, facts(), timeout=60)
        is DockerDisposition.UNSUPPORTED
    )
    with pytest.raises(BootstrapError) as failure:
        await docker.install_compose(session, privilege, facts(), timeouts=TimeoutPolicy())
    assert failure.value.code == "unsupported_docker_installation"
    assert not any("/releases/download/" in c or "apt-get" in c for c, _, _ in session.commands)


async def test_compose_preparation_does_not_mark_docker_installed_or_recoverable():
    class PluginDocker(FakeDocker):
        async def inspect(self, *args, **kwargs):
            return DockerDisposition.COMPOSE_MISSING

        async def install_compose(self, *args, **kwargs):
            await asyncio.sleep(0)

    selected = policy()
    installer = FakeInstaller()
    executor = BootstrapExecutor(
        target_policy=selected,
        connector=FakeConnector(JobSession),
        docker=PluginDocker(),
        installer=installer,
        timeouts=TimeoutPolicy(enrollment_seconds=2),
    )
    store = JobStore(target_policy=selected, executor=executor)
    try:
        accepted = await store.create(make_request())
        await persist_verified_host_key(store, accepted.job_id)
        await provide_jit_enrollment_token(store, accepted.job_id)
        await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
        view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
        assert not view.docker_installed and not view.docker_install_started
        await store.mark_enrollment_completed(
            accepted.job_id, expected_instance_id=store.instance_id
        )
        await wait_for_state(store, accepted.job_id, JobState.COMPLETED)
    finally:
        await store.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="Execute POSIX guards in Linux CI")
@pytest.mark.parametrize(
    "packages,accepted",
    [
        ({"docker.io", "containerd", "runc"}, True),
        ({"docker-ce", "docker-ce-cli", "containerd.io"}, True),
        ({"docker.io", "containerd", "runc", "podman-docker"}, False),
        ({"containerd", "runc"}, False),
    ],
)
def test_real_package_probe_accepts_distro_and_ce_but_not_podman(tmp_path, packages, accepted):
    probe = tmp_path / "dpkg-query"
    installed = "|".join(sorted(packages))
    probe.write_text(
        '#!/bin/sh\nfor arg do package="$arg"; done\n'
        f'case "$package" in {installed}) printf "ii \\n" ;; *) exit 1 ;; esac\n'
    )
    probe.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed generated probe with sandbox package shim
        [shutil.which("sh"), "-c", AptDockerAdapter.reusable_packages_check()],
        env={**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"]},
        capture_output=True,
    )
    assert (result.returncode == 0) is accepted


@pytest.mark.skipif(os.name == "nt", reason="Execute POSIX no-clobber/checksum guards in Linux CI")
@pytest.mark.parametrize("failure", [None, "checksum", "existing", "symlink", "download"])
def test_plugin_binary_is_verified_before_execution_and_never_overwrites(
    tmp_path, monkeypatch, failure
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    private_home = tmp_path / "home"
    private_home.mkdir()
    usr = tmp_path / "usr"
    plugin = usr / "local/lib/docker/cli-plugins/docker-compose"
    artifact = tmp_path / "asset"
    executed = tmp_path / "executed"
    artifact.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(executed))}\nprintf "5.6.0\\n"\n')
    monkeypatch.setitem(
        compose_plugin.DIGESTS,
        "x86_64",
        "0" * 64 if failure == "checksum" else hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    scripts = {
        "systemctl": "printf '4321\\n'",
        "docker": "printf '5.6.0\\n'",
        # The runner owns the sandbox; the real installer additionally requires uid 0.
        "stat": "printf '0\\n'",
        "curl": "exit 1"
        if failure == "download"
        else f'for arg do output="$arg"; done\ncp {shlex.quote(str(artifact))} "$output"',
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    if failure in {"existing", "symlink"}:
        plugin.parent.mkdir(parents=True)
        if failure == "existing":
            plugin.write_text("owned by another application")
        else:
            plugin.symlink_to(tmp_path / "foreign")
    # All installation paths stay under the test directory; external commands are sandbox shims.
    command = compose_plugin.install_command("x86_64").replace("/usr", str(usr))
    result = subprocess.run(  # noqa: S603 - fixed generated installer in a private test tree
        [shutil.which("sh"), "-c", command],
        env={
            **os.environ,
            "HOME": str(private_home),
            "DOCKER_CONFIG": "",
            "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        },
        capture_output=True,
    )
    if failure is None:
        assert result.returncode == 0, result.stderr
        assert plugin.read_bytes() == artifact.read_bytes() and executed.exists()
    else:
        assert result.returncode != 0 and not executed.exists()
        if failure == "existing":
            assert plugin.read_text() == "owned by another application"
        elif failure == "symlink":
            assert plugin.is_symlink()
        else:
            assert not plugin.exists()
    assert not list(plugin.parent.glob(".adojapan-compose.*"))
