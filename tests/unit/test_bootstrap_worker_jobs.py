from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Sequence
from uuid import uuid4

import pytest
from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError, JobConflictError, JobNotFoundError, safe_failure
from bootstrap_worker.installer import (
    AgentProcessState,
    InstallReceipt,
    RemoteNodeInstaller,
)
from bootstrap_worker.jobs import BootstrapExecutor, JobRecord, JobStore
from bootstrap_worker.models import (
    BootstrapRequest,
    DockerDisposition,
    InstallOwnership,
    JobState,
    TimeoutPolicy,
)
from bootstrap_worker.ssh import RemoteResult
from bootstrap_worker.targets import TargetPolicy

IMAGE = f"ghcr.io/andreykutenkikh-byte/restream-node@sha256:{'c' * 64}"
FINGERPRINT = "SHA256:" + base64.b64encode(bytes(range(32))).decode().rstrip("=")
ENROLLMENT_TOKEN = SecretStr("enrollment-token-marker-12345678901234567890")


class RepeatingResolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("172.20.0.8",)


class FakeSession:
    def __init__(self, *, root: bool = True) -> None:
        self.root = root
        self.closed = False
        self.commands: list[tuple[str, SecretStr | None]] = []

    async def run(
        self,
        command: str,
        *,
        stdin: SecretStr | None = None,
        timeout: float,
    ) -> RemoteResult:
        del timeout
        self.commands.append((command, stdin))
        if command == "id -u":
            return RemoteResult(0, "0\n" if self.root else "1000\n")
        if command == "sudo -n -p '' true":
            return RemoteResult(1)
        if command == "sudo -S -p '' true":
            accepted = stdin is not None and stdin.get_secret_value() == "correct-sudo"
            return RemoteResult(0 if accepted else 1)
        if "printf 'os_id='" in command:
            return RemoteResult(
                0,
                """hostname=edge-node-01
os_id=ubuntu
os_version=24.04
architecture=x86_64
cpu_count=2
memory_total_bytes=4294967296
memory_available_bytes=2147483648
disk_total_bytes=42949672960
disk_free_bytes=21474836480
""",
            )
        return RemoteResult(0)

    async def put(self, path: str, content: bytes, *, mode: int, timeout: float) -> None:
        del path, content, mode, timeout

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeConnector:
    def __init__(self, session_factory: Callable[[], FakeSession]) -> None:
        self.session_factory = session_factory
        self.sessions: list[FakeSession] = []

    async def connect(self, **kwargs: object) -> FakeSession:
        verifier = kwargs["verifier"]
        callback = kwargs["on_host_key_verified"]
        verifier.verify("ssh-ed25519", FINGERPRINT)  # type: ignore[union-attr]
        callback(verifier.result)  # type: ignore[operator]
        session = self.session_factory()
        self.sessions.append(session)
        return session


class FailingAuthSequenceConnector:
    def __init__(self, fingerprints: Sequence[str]) -> None:
        self.fingerprints = list(fingerprints)

    async def connect(self, **kwargs: object) -> FakeSession:
        verifier = kwargs["verifier"]
        callback = kwargs["on_host_key_verified"]
        result = verifier.verify(  # type: ignore[union-attr]
            "ssh-ed25519",
            self.fingerprints.pop(0),
        )
        callback(result)  # type: ignore[operator]
        raise safe_failure("ssh_authentication_failed")


class FakeDocker:
    async def inspect(self, *args: object, **kwargs: object) -> DockerDisposition:
        return DockerDisposition.READY

    async def install(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Docker install must not run when inspect returns ready")


class FailingDockerInstall:
    async def inspect(self, *args: object, **kwargs: object) -> DockerDisposition:
        return DockerDisposition.ABSENT

    async def install(self, *args: object, **kwargs: object) -> None:
        raise safe_failure("remote_command_failed")


class FakeInstaller:
    def __init__(self) -> None:
        self.rollback_count = 0
        self.cleanup_count = 0
        self.receipt: InstallReceipt | None = None

    async def prepare(self, *args: object, **kwargs: object) -> InstallReceipt:
        self.receipt = InstallReceipt(
            temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - fake remote path
            ownership=InstallOwnership.ABSENT,
            docker_installed=False,
        )
        return self.receipt

    async def install(self, *args: object, **kwargs: object) -> None:
        receipt = args[2]
        receipt.files_applied = True  # type: ignore[union-attr]

    async def final_check(self, *args: object, **kwargs: object) -> None:
        receipt = args[2]
        receipt.enrollment_completed = True  # type: ignore[union-attr]

    async def rollback(self, *args: object, **kwargs: object) -> None:
        self.rollback_count += 1

    async def cleanup_temp(self, *args: object, **kwargs: object) -> None:
        self.cleanup_count += 1


class FinalCheckCancellationInstaller:
    """Pause after a successful final check to expose the terminal-state race."""

    def __init__(self, receipt: InstallReceipt) -> None:
        self.receipt = receipt
        self.final_check_passed = asyncio.Event()
        self.release_final_check = asyncio.Event()
        self.cleanup_count = 0
        self._rollback = RemoteNodeInstaller()

    async def prepare(self, *args: object, **kwargs: object) -> InstallReceipt:
        return self.receipt

    async def install(self, *args: object, **kwargs: object) -> None:
        receipt = args[2]
        assert receipt is self.receipt
        self.receipt.files_applied = True
        if self.receipt.ownership is InstallOwnership.ABSENT:
            self.receipt.managed_scope_acquired = True

    async def final_check(self, *args: object, **kwargs: object) -> None:
        receipt = args[2]
        assert receipt is self.receipt
        self.receipt.enrollment_completed = True
        self.final_check_passed.set()
        await self.release_final_check.wait()

    async def rollback(self, *args: object, **kwargs: object) -> None:
        await self._rollback.rollback(  # type: ignore[arg-type]
            args[0],
            args[1],
            args[2],
            timeout=kwargs["timeout"],
        )

    async def cleanup_temp(self, *args: object, **kwargs: object) -> None:
        self.cleanup_count += 1


class SlowExecutor:
    def __init__(self, overall_seconds: float) -> None:
        self.timeouts = TimeoutPolicy(overall_seconds=overall_seconds)

    async def run(self, record: JobRecord) -> None:
        record.transition(JobState.RESOLVING)
        await asyncio.Event().wait()


class TokenHoldingExecutor:
    def __init__(self) -> None:
        self.timeouts = TimeoutPolicy(overall_seconds=60)

    async def run(self, record: JobRecord) -> None:
        for state in (
            JobState.RESOLVING,
            JobState.CONNECTING,
            JobState.VERIFYING_HOST_KEY,
            JobState.AUTHENTICATING,
            JobState.CHECKING_PRIVILEGES,
            JobState.CHECKING_SYSTEM,
            JobState.CHECKING_RESOURCES,
            JobState.CHECKING_DOCKER,
            JobState.NEEDS_ENROLLMENT_TOKEN,
        ):
            record.transition(state)
        await asyncio.Event().wait()


def make_request(**updates: object) -> BootstrapRequest:
    values: dict[str, object] = {
        "node_id": uuid4(),
        "address": "ci-ssh-target",
        "port": 22,
        "username": "root",
        "password": "ssh-password-marker",
        "control_url": "http://backend:8000",
        "node_agent_image": IMAGE,
        "node_agent_environment": "test",
    }
    values.update(updates)
    return BootstrapRequest.model_validate(values)


def policy() -> TargetPolicy:
    return TargetPolicy(
        environment="test",
        test_allowlist=("ci-ssh-target:22",),
        resolver=RepeatingResolver(),
    )


async def wait_for_state(store: JobStore, job_id: object, state: JobState) -> None:
    for _ in range(200):
        view = await store.get(job_id, expected_instance_id=store.instance_id)  # type: ignore[arg-type]
        if view.state is state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"job did not reach {state}")


async def persist_verified_host_key(store: JobStore, job_id: object) -> None:
    await wait_for_state(store, job_id, JobState.AUTHENTICATING)
    before = await store.get(job_id, expected_instance_id=store.instance_id)  # type: ignore[arg-type]
    assert before.host_key is not None
    await store.mark_host_key_persisted(
        job_id,  # type: ignore[arg-type]
        expected_instance_id=store.instance_id,
    )


async def provide_jit_enrollment_token(store: JobStore, job_id: object) -> None:
    await wait_for_state(store, job_id, JobState.NEEDS_ENROLLMENT_TOKEN)
    before_token = await store.get(  # type: ignore[arg-type]
        job_id,
        expected_instance_id=store.instance_id,
    )
    assert before_token.enrollment_token_received is False
    await store.submit_enrollment_token(
        job_id,  # type: ignore[arg-type]
        ENROLLMENT_TOKEN,
        expected_instance_id=store.instance_id,
    )


async def build_store(*, root: bool = True) -> tuple[JobStore, FakeInstaller]:
    selected_policy = policy()
    installer = FakeInstaller()
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=FakeConnector(lambda: FakeSession(root=root)),
        docker=FakeDocker(),  # type: ignore[arg-type]
        installer=installer,  # type: ignore[arg-type]
        timeouts=TimeoutPolicy(enrollment_seconds=2),
    )
    return JobStore(target_policy=selected_policy, executor=executor), installer


async def cancel_after_successful_final_check(
    receipt: InstallReceipt,
) -> tuple[FinalCheckCancellationInstaller, FakeSession]:
    selected_policy = policy()
    connector = FakeConnector(FakeSession)
    installer = FinalCheckCancellationInstaller(receipt)
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=connector,
        docker=FakeDocker(),  # type: ignore[arg-type]
        installer=installer,  # type: ignore[arg-type]
        timeouts=TimeoutPolicy(enrollment_seconds=2),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)
    accepted = await store.create(make_request())
    await persist_verified_host_key(store, accepted.job_id)
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    await store.mark_enrollment_completed(
        accepted.job_id,
        expected_instance_id=store.instance_id,
    )
    await asyncio.wait_for(installer.final_check_passed.wait(), timeout=1)
    await wait_for_state(store, accepted.job_id, JobState.RUNNING_SELF_TEST)
    await store.cancel(accepted.job_id, expected_instance_id=store.instance_id)
    installer.release_final_check.set()
    await wait_for_state(store, accepted.job_id, JobState.CANCELLED)
    await store.shutdown()
    return installer, connector.sessions[0]


async def test_full_fake_workflow_completes_and_drops_all_secret_models() -> None:
    store, installer = await build_store()
    accepted = await store.create(make_request())
    await persist_verified_host_key(store, accepted.job_id)
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    waiting = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    serialized = waiting.model_dump_json()
    assert "ssh-password-marker" not in serialized
    assert "enrollment-token-marker" not in serialized
    assert waiting.host_key is not None
    assert waiting.target is not None and waiting.target.resolved_ip == "172.20.0.8"

    await store.mark_enrollment_completed(
        accepted.job_id,
        expected_instance_id=store.instance_id,
    )
    await wait_for_state(store, accepted.job_id, JobState.COMPLETED)
    assert store._jobs[accepted.job_id].request is None
    assert installer.rollback_count == 0
    assert installer.cleanup_count == 1
    assert installer.receipt is not None and installer.receipt.workflow_committed is True
    await store.shutdown()


async def test_cancel_after_final_check_stops_fresh_agent_but_retains_enrollment_evidence() -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - fake remote path
        ownership=InstallOwnership.ABSENT,
        docker_installed=False,
    )
    installer, session = await cancel_after_successful_final_check(receipt)

    assert receipt.enrollment_completed is True
    assert receipt.workflow_committed is False
    assert receipt.rollback_succeeded is True
    assert installer.cleanup_count == 1
    stop = next(command for command, _ in session.commands if " down" in command)
    assert "/opt/adojapan-restream-node/.node-id" in stop
    assert f"{receipt.temp_root}/node-id" in stop
    assert all(
        "rm -rf -- /opt/adojapan-restream-node" not in command for command, _ in session.commands
    )


async def test_cancel_after_final_check_restores_managed_retry_state() -> None:
    receipt = InstallReceipt(
        temp_root=f"/tmp/adojapan-bootstrap-{uuid4()}",  # noqa: S108 - fake remote path
        ownership=InstallOwnership.MANAGED,
        docker_installed=False,
        backup_path="/opt/adojapan-restream-node/.compose.rollback-race",
        backup_permanent_path="/opt/adojapan-restream-node/.node-token.rollback-race",
        existing_enrolled=True,
        rotate_existing_credential=True,
        previous_agent_state=AgentProcessState.RUNNING,
        enrollment_token_applied=True,
    )
    installer, session = await cancel_after_successful_final_check(receipt)

    assert receipt.enrollment_completed is True
    assert receipt.workflow_committed is False
    assert receipt.rollback_succeeded is True
    assert installer.cleanup_count == 1
    rollback = next(
        command
        for command, _ in session.commands
        if ".node-token.rollback-race" in command and "stop -t 45 agent" in command
    )
    assert ".compose.rollback-race" in rollback
    assert "install -o 10001 -g 10001 -m 0600" in rollback
    assert "up -d --force-recreate agent" in rollback
    assert " down" not in rollback
    assert "rm -rf -- /opt/adojapan-restream-node" not in rollback


async def test_no_remote_command_runs_until_verified_host_key_is_durably_acknowledged() -> None:
    selected_policy = policy()
    connector = FakeConnector(FakeSession)
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=connector,
        docker=FakeDocker(),  # type: ignore[arg-type]
        installer=FakeInstaller(),  # type: ignore[arg-type]
        timeouts=TimeoutPolicy(enrollment_seconds=2),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.AUTHENTICATING)
    assert connector.sessions[0].commands == []
    first = await store.mark_host_key_persisted(
        accepted.job_id,
        expected_instance_id=store.instance_id,
    )
    second = await store.mark_host_key_persisted(
        accepted.job_id,
        expected_instance_id=store.instance_id,
    )
    assert first.host_key == second.host_key
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    await store.cancel(accepted.job_id, expected_instance_id=store.instance_id)
    await wait_for_state(store, accepted.job_id, JobState.CANCELLED)
    await store.shutdown()


async def test_host_key_persistence_wait_is_cancellable_without_remote_commands() -> None:
    selected_policy = policy()
    connector = FakeConnector(FakeSession)
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=connector,
        timeouts=TimeoutPolicy(),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.AUTHENTICATING)
    await store.cancel(accepted.job_id, expected_instance_id=store.instance_id)
    await wait_for_state(store, accepted.job_id, JobState.CANCELLED)
    assert connector.sessions[0].commands == []
    assert connector.sessions[0].closed is True
    await store.shutdown()


async def test_host_key_persistence_wait_is_bounded_by_overall_timeout() -> None:
    selected_policy = policy()
    connector = FakeConnector(FakeSession)
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=connector,
        timeouts=TimeoutPolicy(overall_seconds=0.02),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.FAILED)
    view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    assert view.safe_error is not None
    assert view.safe_error.code == "overall_timeout"
    assert view.host_key is not None
    assert connector.sessions[0].commands == []
    assert connector.sessions[0].closed is True
    await store.shutdown()


async def test_failed_auth_exposes_tofu_pin_and_changed_key_retry_fails_closed() -> None:
    changed = "SHA256:" + base64.b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
    selected_policy = policy()
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=FailingAuthSequenceConnector((FINGERPRINT, changed)),
        timeouts=TimeoutPolicy(),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)

    first = await store.create(make_request())
    await wait_for_state(store, first.job_id, JobState.FAILED)
    first_view = await store.get(first.job_id, expected_instance_id=store.instance_id)
    assert first_view.safe_error is not None
    assert first_view.safe_error.code == "ssh_authentication_failed"
    assert first_view.host_key is not None
    assert first_view.host_key.fingerprint == FINGERPRINT

    retry = await store.create(
        make_request(
            node_id=uuid4(),
            pinned_host_fingerprint=first_view.host_key.fingerprint,
        )
    )
    await wait_for_state(store, retry.job_id, JobState.FAILED)
    retry_view = await store.get(retry.job_id, expected_instance_id=store.instance_id)
    assert retry_view.safe_error is not None
    assert retry_view.safe_error.code == "ssh_host_key_changed"
    assert retry_view.host_key is None
    await store.shutdown()


async def test_docker_install_started_is_visible_even_when_installation_fails() -> None:
    selected_policy = policy()
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=FakeConnector(FakeSession),
        docker=FailingDockerInstall(),  # type: ignore[arg-type]
        installer=FakeInstaller(),  # type: ignore[arg-type]
        timeouts=TimeoutPolicy(),
    )
    store = JobStore(target_policy=selected_policy, executor=executor)
    accepted = await store.create(make_request())
    await persist_verified_host_key(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.FAILED)
    view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    assert view.docker_install_started is True
    assert view.docker_installed is False
    await store.shutdown()


async def test_sudo_flow_accepts_only_in_needs_password_state() -> None:
    store, _ = await build_store(root=False)
    accepted = await store.create(make_request(username="operator"))
    await persist_verified_host_key(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.NEEDS_SUDO_PASSWORD)

    await store.submit_sudo_password(
        accepted.job_id,
        SecretStr("wrong-sudo"),
        expected_instance_id=store.instance_id,
    )
    for _ in range(200):
        view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
        if (
            view.state is JobState.NEEDS_SUDO_PASSWORD
            and view.safe_error is not None
            and view.safe_error.code == "sudo_password_invalid"
        ):
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("wrong sudo password was not processed")
    await store.submit_sudo_password(
        accepted.job_id,
        SecretStr("correct-sudo"),
        expected_instance_id=store.instance_id,
    )
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    await store.cancel(accepted.job_id, expected_instance_id=store.instance_id)
    await wait_for_state(store, accepted.job_id, JobState.CANCELLED)
    await store.shutdown()


async def test_cancel_rolls_back_only_prepared_install() -> None:
    store, installer = await build_store()
    accepted = await store.create(make_request())
    await persist_verified_host_key(store, accepted.job_id)
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    await store.cancel(accepted.job_id, expected_instance_id=store.instance_id)
    await wait_for_state(store, accepted.job_id, JobState.CANCELLED)
    assert installer.rollback_count == 1
    assert installer.cleanup_count == 1
    assert store._jobs[accepted.job_id].request is None
    await store.shutdown()


async def test_only_one_active_job_is_allowed() -> None:
    store, _ = await build_store()
    first = await store.create(make_request())
    with pytest.raises(JobConflictError):
        await store.create(make_request(node_id=uuid4()))
    await store.cancel(first.job_id, expected_instance_id=store.instance_id)
    await wait_for_state(store, first.job_id, JobState.CANCELLED)
    await store.shutdown()


async def test_request_environment_must_match_worker_environment() -> None:
    selected_policy = policy()
    store = JobStore(target_policy=selected_policy)
    with pytest.raises(BootstrapError) as captured:
        await store.create(
            make_request(
                node_agent_environment="development",
                node_agent_image="adojapan-restream-node:dev",
            )
        )
    assert captured.value.code == "invalid_target"
    await store.shutdown()


async def test_overall_timeout_is_terminal_and_clears_secrets() -> None:
    selected_policy = policy()
    store = JobStore(
        target_policy=selected_policy,
        executor=SlowExecutor(0.02),  # type: ignore[arg-type]
    )
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.FAILED)
    view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    assert view.safe_error is not None and view.safe_error.code == "overall_timeout"
    assert store._jobs[accepted.job_id].request is None
    await store.shutdown()


async def test_terminal_jobs_are_removed_after_ttl() -> None:
    now = [0.0]
    selected_policy = policy()
    installer = FakeInstaller()
    executor = BootstrapExecutor(
        target_policy=selected_policy,
        connector=FakeConnector(FakeSession),
        docker=FakeDocker(),  # type: ignore[arg-type]
        installer=installer,  # type: ignore[arg-type]
        timeouts=TimeoutPolicy(enrollment_seconds=2),
    )
    store = JobStore(
        target_policy=selected_policy,
        executor=executor,
        terminal_ttl_seconds=10,
        clock=lambda: now[0],
    )
    accepted = await store.create(make_request())
    await persist_verified_host_key(store, accepted.job_id)
    await provide_jit_enrollment_token(store, accepted.job_id)
    await wait_for_state(store, accepted.job_id, JobState.WAITING_FOR_ENROLLMENT)
    await store.mark_enrollment_completed(
        accepted.job_id,
        expected_instance_id=store.instance_id,
    )
    await wait_for_state(store, accepted.job_id, JobState.COMPLETED)
    now[0] = 11
    await store.prune()
    with pytest.raises(JobNotFoundError):
        await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    await store.shutdown()


async def test_shutdown_marks_active_job_as_worker_restarted() -> None:
    selected_policy = policy()
    store = JobStore(
        target_policy=selected_policy,
        executor=SlowExecutor(60),  # type: ignore[arg-type]
    )
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.RESOLVING)
    await store.shutdown()
    view = await store.get(accepted.job_id, expected_instance_id=store.instance_id)
    assert view.state is JobState.FAILED
    assert view.safe_error is not None
    assert view.safe_error.code == "bootstrap_worker_restarted"
    assert store._jobs[accepted.job_id].request is None


async def test_worker_restart_clears_unconsumed_jit_enrollment_secret() -> None:
    selected_policy = policy()
    store = JobStore(
        target_policy=selected_policy,
        executor=TokenHoldingExecutor(),  # type: ignore[arg-type]
    )
    accepted = await store.create(make_request())
    await wait_for_state(store, accepted.job_id, JobState.NEEDS_ENROLLMENT_TOKEN)
    await store.submit_enrollment_token(
        accepted.job_id,
        ENROLLMENT_TOKEN,
        expected_instance_id=store.instance_id,
    )
    record = store._jobs[accepted.job_id]
    assert record.enrollment_tokens.qsize() == 1

    await store.shutdown()

    assert record.machine.state is JobState.FAILED
    assert record.safe_error is not None
    assert record.safe_error.code == "bootstrap_worker_restarted"
    assert record.request is None
    assert record.enrollment_tokens.empty()
