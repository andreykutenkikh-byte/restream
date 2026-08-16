"""In-memory jobs, cooperative cancellation, TTL, and restart semantics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import SecretStr

from bootstrap_worker.errors import (
    BootstrapError,
    CancellationRequested,
    InvalidTransitionError,
    JobConflictError,
    JobNotFoundError,
    WorkerRestartedError,
    safe_failure,
)
from bootstrap_worker.host_keys import HostKeyVerifier
from bootstrap_worker.installer import (
    DockerBootstrap,
    InstallReceipt,
    PrivilegeContext,
    RemoteNodeInstaller,
    detect_privilege,
    probe_system,
    validate_operating_system,
    validate_resources,
    verify_sudo_password,
)
from bootstrap_worker.models import (
    TERMINAL_JOB_STATES,
    BootstrapRequest,
    DockerDisposition,
    HostKeyResult,
    JobAccepted,
    JobState,
    JobView,
    SafeError,
    SystemFacts,
    TargetIdentity,
    TimeoutPolicy,
    utc_now,
)
from bootstrap_worker.ssh import AsyncSSHConnector, RemoteSession, SSHConnector
from bootstrap_worker.state_machine import JobStateMachine
from bootstrap_worker.targets import TargetPolicy

MIN_TERMINAL_RESULT_TTL_SECONDS = 1200.0


def _request_identity(request: BootstrapRequest) -> tuple[object, ...]:
    """Return the secret-free identity used for idempotent create retries."""

    return (
        request.node_id,
        request.address,
        request.port,
        request.username,
        request.expected_host_fingerprint,
        request.pinned_host_fingerprint,
        request.control_url,
        request.node_agent_image,
        request.node_agent_environment,
        request.recover_failed_install,
        request.adopt_empty_managed_root_for_test,
    )


@dataclass(slots=True)
class JobRecord:
    job_id: UUID
    worker_instance_id: UUID
    request: BootstrapRequest | None = field(repr=False)
    target: TargetIdentity
    created_at: datetime
    updated_at: datetime
    created_monotonic: float
    machine: JobStateMachine = field(default_factory=JobStateMachine)
    request_identity: tuple[object, ...] = field(default_factory=tuple, repr=False)
    safe_error: SafeError | None = None
    host_key: HostKeyResult | None = None
    system: SystemFacts | None = None
    docker_install_started: bool = False
    docker_installed: bool = False
    finished_at: datetime | None = None
    finished_monotonic: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    enrollment_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    host_key_persisted_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        repr=False,
    )
    sudo_passwords: asyncio.Queue[SecretStr] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1),
        repr=False,
    )
    enrollment_tokens: asyncio.Queue[SecretStr] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1),
        repr=False,
    )
    enrollment_token_received: bool = False

    def transition(self, target: JobState) -> None:
        if self.machine.state is JobState.CANCELLING and target not in {
            JobState.CANCELLED,
            JobState.FAILED,
        }:
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)
        self.machine.transition(target)
        self.updated_at = utc_now()

    def checkpoint(self) -> None:
        if self.cancel_event.is_set() or self.machine.state is JobState.CANCELLING:
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)

    async def wait_for_secret_or_cancel(self) -> SecretStr:
        secret_task = asyncio.create_task(self.sudo_passwords.get())
        cancel_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {secret_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done and cancel_task.result():
            if secret_task in done:
                secret_task.result()
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)
        return secret_task.result()

    async def wait_for_enrollment_or_cancel(self) -> None:
        enrollment_task = asyncio.create_task(self.enrollment_event.wait())
        cancel_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {enrollment_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done and cancel_task.result():
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)

    async def wait_for_enrollment_token_or_cancel(self) -> SecretStr:
        token_task = asyncio.create_task(self.enrollment_tokens.get())
        cancel_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {token_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done and cancel_task.result():
            if token_task in done:
                token_task.result()
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)
        return token_task.result()

    async def wait_for_host_key_persistence_or_cancel(self) -> None:
        persisted_task = asyncio.create_task(self.host_key_persisted_event.wait())
        cancel_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {persisted_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done and cancel_task.result():
            raise CancellationRequested("cancelled", safe_failure("cancelled").safe_message)

    def require_request(self) -> BootstrapRequest:
        if self.request is None:
            raise safe_failure("bootstrap_worker_restarted")
        return self.request

    def clear_enrollment_token(self) -> None:
        self.enrollment_token_received = False
        while not self.enrollment_tokens.empty():
            try:
                self.enrollment_tokens.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive race guard
                break

    def clear_secrets(self) -> None:
        self.request = None
        while not self.sudo_passwords.empty():
            try:
                self.sudo_passwords.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive race guard
                break
        self.clear_enrollment_token()

    def snapshot(self) -> JobView:
        return JobView(
            job_id=self.job_id,
            worker_instance_id=self.worker_instance_id,
            state=self.machine.state,
            current_step=self.machine.current_step,
            progress_percent=self.machine.progress_percent(),
            steps=self.machine.step_views(),
            safe_error=self.safe_error,
            target=self.target,
            host_key=self.host_key,
            system=self.system,
            docker_install_started=self.docker_install_started,
            docker_installed=self.docker_installed,
            enrollment_token_received=self.enrollment_token_received,
            created_at=self.created_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
        )


class BootstrapExecutor:
    """Execute the only supported bootstrap workflow, one bounded step at a time."""

    def __init__(
        self,
        *,
        target_policy: TargetPolicy,
        connector: SSHConnector | None = None,
        docker: DockerBootstrap | None = None,
        installer: RemoteNodeInstaller | None = None,
        timeouts: TimeoutPolicy | None = None,
    ) -> None:
        self._target_policy = target_policy
        self._connector = connector or AsyncSSHConnector()
        self._docker = docker or DockerBootstrap()
        self._installer = installer or RemoteNodeInstaller()
        self.timeouts = timeouts or TimeoutPolicy()

    async def run(self, record: JobRecord) -> None:
        request = record.require_request()
        session: RemoteSession | None = None
        privilege: PrivilegeContext | None = None
        receipt: InstallReceipt | None = None
        completed = False
        try:
            record.transition(JobState.RESOLVING)
            record.checkpoint()
            target = await self._target_policy.revalidate(record.target)
            record.target = target

            record.transition(JobState.CONNECTING)
            record.checkpoint()
            verifier = HostKeyVerifier(
                expected_fingerprint=request.expected_host_fingerprint,
                pinned_fingerprint=request.pinned_host_fingerprint,
            )
            record.transition(JobState.VERIFYING_HOST_KEY)

            def host_key_verified(result: HostKeyResult) -> None:
                # AsyncSSH validates the server key before user authentication.
                # Publish the verified TOFU/expected result immediately so a
                # failed password attempt cannot discard the pin.
                record.host_key = result
                record.updated_at = utc_now()
                if record.machine.state is JobState.VERIFYING_HOST_KEY:
                    record.transition(JobState.AUTHENTICATING)

            session = await self._connector.connect(
                target=target,
                username=request.username,
                password=request.ssh_password,
                verifier=verifier,
                on_host_key_verified=host_key_verified,
                timeouts=self.timeouts,
            )
            record.checkpoint()
            if record.machine.state is not JobState.AUTHENTICATING or verifier.result is None:
                raise safe_failure("ssh_host_key_unsupported")
            # The callback must have made the verified result observable before
            # AsyncSSH attempted password authentication.
            if record.host_key != verifier.result:
                raise safe_failure("ssh_host_key_unsupported")

            # No remote command may run until the coordinating backend has
            # durably persisted the verified fingerprint and acknowledged it.
            await record.wait_for_host_key_persistence_or_cancel()
            record.checkpoint()

            record.transition(JobState.CHECKING_PRIVILEGES)
            privilege = await detect_privilege(
                session,
                request.ssh_password,
                timeout=self.timeouts.command_seconds,
            )
            if privilege is None and request.sudo_password is not None:
                privilege = await verify_sudo_password(
                    session,
                    request.sudo_password,
                    timeout=self.timeouts.command_seconds,
                )
            if privilege is None:
                record.transition(JobState.NEEDS_SUDO_PASSWORD)
                record.safe_error = SafeError(
                    code="needs_sudo_password",
                    message="Для продолжения требуется пароль sudo.",
                )
            while privilege is None:
                submitted = await record.wait_for_secret_or_cancel()
                record.transition(JobState.CHECKING_PRIVILEGES)
                privilege = await verify_sudo_password(
                    session,
                    submitted,
                    timeout=self.timeouts.command_seconds,
                )
                submitted = SecretStr("")
                if privilege is None:
                    record.transition(JobState.NEEDS_SUDO_PASSWORD)
                    record.safe_error = SafeError(
                        code="sudo_password_invalid",
                        message=safe_failure("sudo_password_invalid").safe_message,
                    )
            record.safe_error = None
            record.checkpoint()

            record.transition(JobState.CHECKING_SYSTEM)
            facts = await probe_system(session, timeout=self.timeouts.command_seconds)
            facts = validate_operating_system(facts)
            record.system = facts
            record.checkpoint()

            record.transition(JobState.CHECKING_RESOURCES)
            facts = validate_resources(facts)
            record.system = facts
            record.checkpoint()

            record.transition(JobState.CHECKING_DOCKER)
            docker_state = await self._docker.inspect(
                session,
                privilege,
                timeout=self.timeouts.command_seconds,
            )
            if docker_state is DockerDisposition.UNSUPPORTED:
                raise safe_failure("unsupported_docker_installation")
            if docker_state is DockerDisposition.ABSENT:
                record.transition(JobState.INSTALLING_DOCKER)
                record.docker_install_started = True
                record.updated_at = utc_now()
                await self._docker.install(
                    session,
                    privilege,
                    facts,
                    timeouts=self.timeouts,
                )
                record.docker_installed = True
            # READY is intentionally observation-only: the install/start plan is
            # unreachable for an existing supported Docker daemon.
            record.checkpoint()

            record.transition(JobState.NEEDS_ENROLLMENT_TOKEN)
            async with asyncio.timeout(self.timeouts.command_seconds):
                enrollment_token = await record.wait_for_enrollment_token_or_cancel()
            record.checkpoint()

            record.transition(JobState.PREPARING_AGENT)
            try:
                receipt = await self._installer.prepare(
                    session,
                    privilege,
                    request,
                    facts,
                    enrollment_token=enrollment_token,
                    job_id=record.job_id,
                    docker_installed=record.docker_installed,
                    timeouts=self.timeouts,
                )
            finally:
                enrollment_token = SecretStr("")
                record.clear_enrollment_token()
            record.checkpoint()

            record.transition(JobState.INSTALLING_AGENT)
            await self._installer.install(
                session,
                privilege,
                receipt,
                timeouts=self.timeouts,
            )
            record.checkpoint()

            record.transition(JobState.WAITING_FOR_ENROLLMENT)
            async with asyncio.timeout(self.timeouts.enrollment_seconds):
                await record.wait_for_enrollment_or_cancel()
            record.checkpoint()
            # The backend only signals this event after it has accepted the
            # one-time enrollment. From this point the managed filesystem is
            # evidence and must survive cancellation/final-check failure.
            receipt.enrollment_completed = True

            record.transition(JobState.RUNNING_SELF_TEST)
            await self._installer.final_check(
                session,
                privilege,
                receipt,
                timeout=self.timeouts.command_seconds,
            )
            record.transition(JobState.COMPLETED)
            receipt.workflow_committed = True
            completed = True
        except TimeoutError as exc:
            raise safe_failure("agent_enrollment_failed") from exc
        finally:
            if session is not None and privilege is not None and receipt is not None:
                try:
                    async with asyncio.timeout(self.timeouts.command_seconds):
                        if not completed:
                            await self._installer.rollback(
                                session,
                                privilege,
                                receipt,
                                timeout=self.timeouts.command_seconds,
                            )
                        await self._installer.cleanup_temp(
                            session,
                            privilege,
                            receipt,
                            timeout=self.timeouts.command_seconds,
                        )
                except TimeoutError:
                    pass
            if privilege is not None:
                privilege.clear()
            if session is not None:
                session.close()
                try:
                    async with asyncio.timeout(5):
                        await session.wait_closed()
                except TimeoutError:
                    pass
            request = None  # type: ignore[assignment]


class JobStore:
    """Single-process job store; no password or token persistence exists."""

    def __init__(
        self,
        *,
        target_policy: TargetPolicy,
        executor: BootstrapExecutor | None = None,
        max_active_jobs: int = 1,
        terminal_ttl_seconds: float = MIN_TERMINAL_RESULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_active_jobs < 1:
            raise ValueError("max_active_jobs must be positive")
        if terminal_ttl_seconds <= 0:
            raise ValueError("terminal_ttl_seconds must be positive")
        self.instance_id = uuid4()
        self.started_at = utc_now()
        self._target_policy = target_policy
        self._executor = executor or BootstrapExecutor(target_policy=target_policy)
        self._max_active_jobs = max_active_jobs
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._clock = clock
        self._jobs: dict[UUID, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: BootstrapRequest) -> JobAccepted:
        async with self._lock:
            self._prune_locked()
            if request.node_agent_environment != self._target_policy.environment:
                raise safe_failure("invalid_target")
            request_identity = _request_identity(request)
            if request.job_id is not None:
                existing = self._jobs.get(request.job_id)
                if existing is not None:
                    if existing.request_identity != request_identity:
                        error = safe_failure("job_conflict")
                        raise JobConflictError(error.code, error.safe_message)
                    return JobAccepted(
                        job_id=existing.job_id,
                        state=existing.machine.state,
                        worker_instance_id=self.instance_id,
                    )
            active = [
                job for job in self._jobs.values() if job.machine.state not in TERMINAL_JOB_STATES
            ]
            if len(active) >= self._max_active_jobs:
                error = safe_failure("job_conflict")
                raise JobConflictError(error.code, error.safe_message)
            target = await self._target_policy.resolve(request.address, request.port)
            if any(
                job.target.address == target.address and job.target.port == target.port
                for job in active
            ):
                error = safe_failure("job_conflict")
                raise JobConflictError(error.code, error.safe_message)
            job_id = request.job_id or uuid4()
            if job_id in self._jobs:
                error = safe_failure("job_conflict")
                raise JobConflictError(error.code, error.safe_message)
            now = utc_now()
            record = JobRecord(
                job_id=job_id,
                worker_instance_id=self.instance_id,
                request=request,
                target=target,
                created_at=now,
                updated_at=now,
                created_monotonic=self._clock(),
                request_identity=request_identity,
            )
            self._jobs[job_id] = record
            record.task = asyncio.create_task(self._run(record), name=f"bootstrap-{job_id}")
            return JobAccepted(
                job_id=job_id,
                state=JobState.QUEUED,
                worker_instance_id=self.instance_id,
            )

    async def discover(self, job_id: UUID) -> JobAccepted:
        """Discover an accepted caller-supplied UUID without an instance header."""

        async with self._lock:
            self._prune_locked()
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            return JobAccepted(
                job_id=record.job_id,
                state=record.machine.state,
                worker_instance_id=self.instance_id,
            )

    async def _run(self, record: JobRecord) -> None:
        try:
            async with asyncio.timeout(self._executor.timeouts.overall_seconds):
                await self._executor.run(record)
        except CancellationRequested:
            if record.machine.state not in TERMINAL_JOB_STATES:
                if record.machine.state is not JobState.CANCELLING:
                    record.transition(JobState.CANCELLING)
                record.transition(JobState.CANCELLED)
                failure = safe_failure("cancelled")
                record.safe_error = SafeError(code=failure.code, message=failure.safe_message)
        except TimeoutError:
            self._fail(record, safe_failure("overall_timeout"))
        except BootstrapError as exc:
            self._fail(record, exc)
        except asyncio.CancelledError:
            if record.machine.state not in TERMINAL_JOB_STATES:
                self._fail(record, safe_failure("bootstrap_worker_restarted"))
            raise
        except Exception:
            self._fail(record, safe_failure("remote_command_failed"))
        finally:
            if record.machine.state in TERMINAL_JOB_STATES:
                now = utc_now()
                record.finished_at = now
                record.updated_at = now
                record.finished_monotonic = self._clock()
                record.clear_secrets()

    def _fail(self, record: JobRecord, error: BootstrapError) -> None:
        if record.machine.state in TERMINAL_JOB_STATES:
            return
        try:
            record.transition(JobState.FAILED)
        except InvalidTransitionError:
            return
        record.safe_error = SafeError(code=error.code, message=error.safe_message)

    def _assert_instance(self, expected_instance_id: UUID) -> None:
        if expected_instance_id != self.instance_id:
            error = safe_failure("bootstrap_worker_restarted")
            raise WorkerRestartedError(error.code, error.safe_message)

    async def get(self, job_id: UUID, *, expected_instance_id: UUID) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            self._prune_locked()
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            return record.snapshot()

    async def submit_sudo_password(
        self,
        job_id: UUID,
        password: SecretStr,
        *,
        expected_instance_id: UUID,
    ) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            if record.machine.state is not JobState.NEEDS_SUDO_PASSWORD:
                raise safe_failure("invalid_job_state")
            if record.sudo_passwords.full():
                raise safe_failure("invalid_job_state")
            record.sudo_passwords.put_nowait(password)
            record.safe_error = None
            record.updated_at = utc_now()
            return record.snapshot()

    async def submit_enrollment_token(
        self,
        job_id: UUID,
        enrollment_token: SecretStr,
        *,
        expected_instance_id: UUID,
    ) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            if record.machine.state is not JobState.NEEDS_ENROLLMENT_TOKEN:
                raise safe_failure("invalid_job_state")
            if record.enrollment_token_received:
                return record.snapshot()
            if record.enrollment_tokens.full():  # pragma: no cover - invariant guard
                raise safe_failure("invalid_job_state")
            record.enrollment_tokens.put_nowait(enrollment_token)
            record.enrollment_token_received = True
            record.updated_at = utc_now()
            return record.snapshot()

    async def mark_host_key_persisted(
        self,
        job_id: UUID,
        *,
        expected_instance_id: UUID,
    ) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            if record.host_key is None:
                raise safe_failure("invalid_job_state")
            if record.host_key_persisted_event.is_set():
                return record.snapshot()
            if record.machine.state is not JobState.AUTHENTICATING:
                raise safe_failure("invalid_job_state")
            record.host_key_persisted_event.set()
            record.updated_at = utc_now()
            return record.snapshot()

    async def cancel(
        self,
        job_id: UUID,
        *,
        expected_instance_id: UUID,
    ) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            if record.machine.state in TERMINAL_JOB_STATES:
                raise safe_failure("invalid_job_state")
            if record.machine.state is not JobState.CANCELLING:
                record.transition(JobState.CANCELLING)
            record.cancel_event.set()
            return record.snapshot()

    async def mark_enrollment_completed(
        self,
        job_id: UUID,
        *,
        expected_instance_id: UUID,
    ) -> JobView:
        self._assert_instance(expected_instance_id)
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                error = safe_failure("job_not_found")
                raise JobNotFoundError(error.code, error.safe_message)
            if record.machine.state is not JobState.WAITING_FOR_ENROLLMENT:
                raise safe_failure("invalid_job_state")
            record.enrollment_event.set()
            record.updated_at = utc_now()
            return record.snapshot()

    async def prune(self) -> None:
        async with self._lock:
            self._prune_locked()

    def _prune_locked(self) -> None:
        now = self._clock()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_monotonic is not None
            and now - job.finished_monotonic >= self._terminal_ttl_seconds
        ]
        for job_id in expired:
            del self._jobs[job_id]

    async def shutdown(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            for record in self._jobs.values():
                if record.machine.state not in TERMINAL_JOB_STATES:
                    self._fail(record, safe_failure("bootstrap_worker_restarted"))
                    if record.task is not None:
                        record.task.cancel()
                        tasks.append(record.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for record in self._jobs.values():
            if record.machine.state in TERMINAL_JOB_STATES:
                record.clear_secrets()


__all__ = [
    "BootstrapExecutor",
    "JobRecord",
    "JobStore",
    "MIN_TERMINAL_RESULT_TTL_SECONDS",
]
