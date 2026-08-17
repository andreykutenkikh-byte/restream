"""Private FastAPI surface served only over the shared Unix domain socket."""

from __future__ import annotations

import asyncio
import hmac
import math
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from bootstrap_worker.errors import (
    BootstrapError,
    JobConflictError,
    JobNotFoundError,
    WorkerRestartedError,
)
from bootstrap_worker.jobs import MIN_TERMINAL_RESULT_TTL_SECONDS, JobStore
from bootstrap_worker.models import (
    BootstrapRequest,
    EnrollmentTokenRequest,
    HealthView,
    JobAccepted,
    JobView,
    SudoPasswordRequest,
)
from bootstrap_worker.targets import TargetPolicy, parse_test_allowlist

BOOTSTRAP_SECRET_HEADER = "X-Bootstrap-Secret"  # noqa: S105 - HTTP header name
WORKER_INSTANCE_HEADER = "X-Bootstrap-Worker-Instance"
DEFAULT_SOCKET_PATH = "/run/adojapan-bootstrap/bootstrap.sock"


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    environment: str = "development"
    socket_path: str = DEFAULT_SOCKET_PATH
    test_target_allowlist: tuple[str, ...] = ()
    bootstrap_secret: SecretStr | None = field(default=None, repr=False)
    max_active_jobs: int = 1
    terminal_ttl_seconds: float = MIN_TERMINAL_RESULT_TTL_SECONDS

    def __post_init__(self) -> None:
        environment = self.environment.strip().lower()
        object.__setattr__(self, "environment", environment)
        if environment not in {"development", "production", "test"}:
            raise ValueError("ENVIRONMENT must be development, production, or test")
        if environment == "production" and self.bootstrap_secret is None:
            raise ValueError("production bootstrap worker requires BOOTSTRAP_SECRET_FILE")
        if self.bootstrap_secret is not None:
            secret_value = self.bootstrap_secret.get_secret_value()
            if len(secret_value) < 32:
                raise ValueError("bootstrap secret is too short")
            if len(secret_value) > 4096:
                raise ValueError("bootstrap secret is too long")
            if any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in secret_value
            ):
                raise ValueError("bootstrap secret contains forbidden characters")
        if environment != "test" and self.test_target_allowlist:
            raise ValueError("TEST_SSH_TARGET_ALLOWLIST is only allowed in ENVIRONMENT=test")
        if self.max_active_jobs < 1:
            raise ValueError("max_active_jobs must be positive")
        if environment == "production" and self.max_active_jobs != 1:
            raise ValueError("production bootstrap worker allows exactly one active job")
        if not math.isfinite(self.terminal_ttl_seconds) or self.terminal_ttl_seconds <= 0:
            raise ValueError("terminal_ttl_seconds must be positive")
        if (
            environment == "production"
            and self.terminal_ttl_seconds < MIN_TERMINAL_RESULT_TTL_SECONDS
        ):
            raise ValueError("production terminal result TTL must be at least 1200 seconds")

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        environment = os.environ.get("ENVIRONMENT", "development")
        socket_path = os.environ.get("BOOTSTRAP_SOCKET_PATH", DEFAULT_SOCKET_PATH)
        allowlist = parse_test_allowlist(os.environ.get("TEST_SSH_TARGET_ALLOWLIST"))
        secret_path = os.environ.get("BOOTSTRAP_SECRET_FILE")
        secret: SecretStr | None = None
        if secret_path:
            path = Path(secret_path)
            if path.is_symlink() or not path.is_file():
                raise ValueError("BOOTSTRAP_SECRET_FILE must be a regular file")
            if path.stat().st_size > 8192:
                raise ValueError("BOOTSTRAP_SECRET_FILE is too large")
            value = path.read_text(encoding="utf-8").strip()
            if len(value) < 32:
                raise ValueError("BOOTSTRAP_SECRET_FILE is too short")
            secret = SecretStr(value)
            value = ""
        try:
            max_active = int(os.environ.get("BOOTSTRAP_MAX_ACTIVE_JOBS", "1"))
            ttl = float(
                os.environ.get(
                    "BOOTSTRAP_JOB_TTL_SECONDS",
                    str(int(MIN_TERMINAL_RESULT_TTL_SECONDS)),
                )
            )
        except ValueError as exc:
            raise ValueError("bootstrap numeric settings are invalid") from exc
        return cls(
            environment=environment,
            socket_path=socket_path,
            test_target_allowlist=allowlist,
            bootstrap_secret=secret,
            max_active_jobs=max_active,
            terminal_ttl_seconds=ttl,
        )


class InternalAuthenticator:
    def __init__(self, secret: SecretStr | None) -> None:
        self._secret = secret

    async def __call__(
        self,
        supplied: str | None = Header(default=None, alias=BOOTSTRAP_SECRET_HEADER),
    ) -> None:
        if self._secret is None:
            return
        expected = self._secret.get_secret_value()
        if supplied is None or not hmac.compare_digest(expected, supplied):
            raise BootstrapError("worker_authentication_failed", "Worker authentication failed.")


def _error_status(error: BootstrapError) -> int:
    if isinstance(error, JobNotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(error, (JobConflictError, WorkerRestartedError)):
        return status.HTTP_409_CONFLICT
    if error.code in {"invalid_job_state"}:
        return status.HTTP_409_CONFLICT
    if error.code in {
        "invalid_target",
        "ssh_host_key_changed",
        "ssh_host_key_unsupported",
    }:
        return status.HTTP_400_BAD_REQUEST
    if error.code == "worker_authentication_failed":
        return status.HTTP_401_UNAUTHORIZED
    return status.HTTP_502_BAD_GATEWAY


def create_app(
    *,
    settings: WorkerSettings | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    selected_settings = settings or WorkerSettings.from_environment()
    if store is None:
        policy = TargetPolicy(
            environment=selected_settings.environment,
            test_allowlist=selected_settings.test_target_allowlist,
        )
        store = JobStore(
            target_policy=policy,
            max_active_jobs=selected_settings.max_active_jobs,
            terminal_ttl_seconds=selected_settings.terminal_ttl_seconds,
        )
    job_store = store
    authenticate = InternalAuthenticator(selected_settings.bootstrap_secret)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        stop = asyncio.Event()

        async def cleanup_loop() -> None:
            while not stop.is_set():
                try:
                    async with asyncio.timeout(30):
                        await stop.wait()
                except TimeoutError:
                    await job_store.prune()

        cleanup_task = asyncio.create_task(cleanup_loop(), name="bootstrap-job-ttl")
        try:
            yield
        finally:
            stop.set()
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            await job_store.shutdown()

    app = FastAPI(
        title="AdoJapan Bootstrap Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.job_store = job_store
    app.state.settings = selected_settings

    @app.exception_handler(BootstrapError)
    async def bootstrap_error_handler(request: Request, error: BootstrapError) -> JSONResponse:
        del request
        payload: dict[str, object] = {
            "safe_error": {"code": error.code, "message": error.safe_message}
        }
        if isinstance(error, WorkerRestartedError):
            payload["worker_instance_id"] = str(job_store.instance_id)
        return JSONResponse(status_code=_error_status(error), content=payload)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default detail can echo invalid request values, including a
        # rejected password. Deliberately discard both the body and error repr.
        del request, error
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "safe_error": {
                    "code": "invalid_request",
                    "message": "Bootstrap request is invalid.",
                }
            },
        )

    def health_view() -> HealthView:
        return HealthView(
            worker_instance_id=job_store.instance_id,
            started_at=job_store.started_at,
            terminal_ttl_seconds=selected_settings.terminal_ttl_seconds,
        )

    @app.get("/health/live", response_model=HealthView)
    async def health_live() -> HealthView:
        return health_view()

    @app.get(
        "/health/ready",
        response_model=HealthView,
        dependencies=[Depends(authenticate)],
    )
    async def health_ready() -> HealthView:
        return health_view()

    @app.post(
        "/v1/jobs",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def create_job(payload: BootstrapRequest) -> JobAccepted:
        return await job_store.create(payload)

    @app.get(
        "/v1/jobs/{job_id}/accepted",
        response_model=JobAccepted,
        dependencies=[Depends(authenticate)],
    )
    async def discover_job(job_id: UUID) -> JobAccepted:
        return await job_store.discover(job_id)

    async def expected_instance(
        value: UUID = Header(alias=WORKER_INSTANCE_HEADER),
    ) -> UUID:
        return value

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobView,
        dependencies=[Depends(authenticate)],
    )
    async def get_job(job_id: UUID, instance_id: UUID = Depends(expected_instance)) -> JobView:
        return await job_store.get(job_id, expected_instance_id=instance_id)

    @app.post(
        "/v1/jobs/{job_id}/sudo-password",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def submit_sudo_password(
        job_id: UUID,
        payload: SudoPasswordRequest,
        instance_id: UUID = Depends(expected_instance),
    ) -> JobView:
        return await job_store.submit_sudo_password(
            job_id,
            payload.sudo_password,
            expected_instance_id=instance_id,
        )

    @app.post(
        "/v1/jobs/{job_id}/enrollment-token",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def submit_enrollment_token(
        job_id: UUID,
        payload: EnrollmentTokenRequest,
        instance_id: UUID = Depends(expected_instance),
    ) -> JobView:
        try:
            return await job_store.submit_enrollment_token(
                job_id,
                payload.enrollment_token,
                expected_instance_id=instance_id,
            )
        finally:
            payload.enrollment_token = SecretStr("")

    @app.post(
        "/v1/jobs/{job_id}/host-key-persisted",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def host_key_persisted(
        job_id: UUID,
        instance_id: UUID = Depends(expected_instance),
    ) -> JobView:
        return await job_store.mark_host_key_persisted(
            job_id,
            expected_instance_id=instance_id,
        )

    @app.post(
        "/v1/jobs/{job_id}/cancel",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def cancel_job(job_id: UUID, instance_id: UUID = Depends(expected_instance)) -> JobView:
        return await job_store.cancel(job_id, expected_instance_id=instance_id)

    @app.post(
        "/v1/jobs/{job_id}/enrollment-completed",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    async def enrollment_completed(
        job_id: UUID,
        instance_id: UUID = Depends(expected_instance),
    ) -> JobView:
        return await job_store.mark_enrollment_completed(
            job_id,
            expected_instance_id=instance_id,
        )

    return app


__all__: Sequence[str] = (
    "BOOTSTRAP_SECRET_HEADER",
    "DEFAULT_SOCKET_PATH",
    "WORKER_INSTANCE_HEADER",
    "InternalAuthenticator",
    "WorkerSettings",
    "create_app",
)
