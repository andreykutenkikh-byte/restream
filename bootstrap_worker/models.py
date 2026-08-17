"""Public and in-memory models for the isolated bootstrap worker."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_SSH_USERNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_PINNED_IMAGE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_LOCAL_IMAGE = re.compile(
    r"^(?:(?:[a-z0-9]+(?:[.-][a-z0-9]+)*)(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
)


class JobState(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    CONNECTING = "connecting"
    VERIFYING_HOST_KEY = "verifying_host_key"
    AUTHENTICATING = "authenticating"
    CHECKING_PRIVILEGES = "checking_privileges"
    NEEDS_SUDO_PASSWORD = "needs_sudo_password"  # noqa: S105 - state name
    CHECKING_SYSTEM = "checking_system"
    CHECKING_RESOURCES = "checking_resources"
    CHECKING_DOCKER = "checking_docker"
    INSTALLING_DOCKER = "installing_docker"
    NEEDS_ENROLLMENT_TOKEN = "needs_enrollment_token"  # noqa: S105 - state name
    PREPARING_AGENT = "preparing_agent"
    INSTALLING_AGENT = "installing_agent"
    WAITING_FOR_ENROLLMENT = "waiting_for_enrollment"
    RUNNING_SELF_TEST = "running_self_test"
    COMPLETED = "completed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED})


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class BootstrapStep(StrEnum):
    SSH_CONNECT = "ssh_connect"
    SYSTEM_CHECK = "system_check"
    RESOURCES_CHECK = "resources_check"
    DOCKER_CHECK = "docker_check"
    AGENT_INSTALL = "agent_install"
    PANEL_CONNECT = "panel_connect"
    FINAL_CHECK = "final_check"


class HostTrustMode(StrEnum):
    TOFU = "tofu"
    EXPECTED = "expected"
    PINNED = "pinned"


class DockerDisposition(StrEnum):
    READY = "ready"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"


class InstallOwnership(StrEnum):
    ABSENT = "absent"
    MANAGED = "managed"
    CONFLICT = "conflict"


class PrivilegeMode(StrEnum):
    ROOT = "root"
    PASSWORDLESS_SUDO = "passwordless_sudo"
    PASSWORD_SUDO = "password_sudo"  # noqa: S105 - privilege mode name


class TimeoutPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_seconds: float = Field(default=900.0, gt=0, le=3600)
    connect_seconds: float = Field(default=10.0, gt=0, le=60)
    authentication_seconds: float = Field(default=15.0, gt=0, le=60)
    command_seconds: float = Field(default=60.0, gt=0, le=300)
    package_seconds: float = Field(default=300.0, gt=0, le=900)
    enrollment_seconds: float = Field(default=300.0, gt=0, le=900)


class BootstrapRequest(BaseModel):
    """Secrets accepted over UDS and retained only by one in-memory job."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    job_id: UUID | None = None
    node_id: UUID
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65_535)
    username: str = Field(min_length=1, max_length=64)
    ssh_password: SecretStr = Field(alias="password", min_length=1, max_length=4096)
    sudo_password: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    expected_host_fingerprint: str | None = Field(default=None, max_length=128)
    pinned_host_fingerprint: str | None = Field(default=None, max_length=128)
    control_url: str = Field(min_length=1, max_length=2048)
    node_agent_image: str = Field(min_length=1, max_length=512)
    node_agent_environment: Literal["development", "production", "test"] = "production"
    recover_failed_install: bool = Field(default=False, strict=True)
    adopt_empty_managed_root_for_test: bool = Field(default=False, strict=True)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not _SSH_USERNAME.fullmatch(value):
            raise ValueError("username is not a safe POSIX account name")
        return value

    @field_validator("control_url")
    @classmethod
    def validate_control_url(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("control_url contains forbidden characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("control_url is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("control_url must use http or https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("control_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("control_url must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("control_url must be an origin URL")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("control_url port is invalid")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_environment_transport(self) -> BootstrapRequest:
        if self.node_agent_environment == "production" and not self.control_url.startswith(
            "https://"
        ):
            raise ValueError("production control_url must use https")
        if self.adopt_empty_managed_root_for_test and self.node_agent_environment != "test":
            raise ValueError("empty managed-root adoption is test-only")
        if self.node_agent_environment == "production":
            if not _PINNED_IMAGE.fullmatch(self.node_agent_image):
                raise ValueError("production node_agent_image must be pinned by sha256 digest")
        elif not (
            _PINNED_IMAGE.fullmatch(self.node_agent_image)
            or _LOCAL_IMAGE.fullmatch(self.node_agent_image)
        ):
            raise ValueError("node_agent_image is invalid")
        return self

    @field_validator("node_agent_image")
    @classmethod
    def validate_node_agent_image(cls, value: str) -> str:
        if value != value.strip() or any(
            character.isspace() or not character.isprintable() for character in value
        ):
            raise ValueError("node_agent_image contains forbidden characters")
        return value


class SudoPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sudo_password: SecretStr = Field(min_length=1, max_length=4096)


class EnrollmentTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrollment_token: SecretStr = Field(min_length=32, max_length=4096)


class TargetIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str
    port: int
    resolved_ip: str
    resolution_set: tuple[str, ...]
    test_allowlisted: bool = False


class HostKeyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: str
    fingerprint: str
    trust_mode: HostTrustMode


class SystemFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hostname: str = Field(min_length=1, max_length=253)
    os_name: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=32)
    architecture: str = Field(min_length=1, max_length=32)
    cpu_count: int = Field(ge=0, le=4096)
    memory_total_bytes: int = Field(gt=0, le=2**63 - 1)
    memory_available_bytes: int = Field(ge=0, le=2**63 - 1)
    disk_total_bytes: int = Field(gt=0, le=2**63 - 1)
    disk_free_bytes: int = Field(ge=0, le=2**63 - 1)

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        if value != value.strip() or any(
            character.isspace() or not character.isprintable() for character in value
        ):
            raise ValueError("hostname is invalid")
        return value

    @model_validator(mode="after")
    def validate_capacities(self) -> SystemFacts:
        if self.memory_available_bytes > self.memory_total_bytes:
            raise ValueError("memory values are inconsistent")
        if self.disk_free_bytes > self.disk_total_bytes:
            raise ValueError("disk values are inconsistent")
        return self

    @property
    def os_id(self) -> str:
        """Compatibility name used by the Docker repository selector."""

        return self.os_name


class SafeError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str


class StepView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: BootstrapStep
    state: StepState


class JobAccepted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    state: JobState
    worker_instance_id: UUID


class JobView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    worker_instance_id: UUID
    state: JobState
    current_step: BootstrapStep | None
    progress_percent: int = Field(ge=0, le=100)
    steps: tuple[StepView, ...]
    safe_error: SafeError | None
    target: TargetIdentity | None
    host_key: HostKeyResult | None
    system: SystemFacts | None
    docker_install_started: bool
    docker_installed: bool
    enrollment_token_received: bool
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class HealthView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "ok"
    worker_instance_id: UUID
    started_at: datetime
    terminal_ttl_seconds: float = Field(ge=1, le=86400)


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "BootstrapRequest",
    "BootstrapStep",
    "DockerDisposition",
    "EnrollmentTokenRequest",
    "HealthView",
    "HostKeyResult",
    "HostTrustMode",
    "InstallOwnership",
    "JobAccepted",
    "JobState",
    "JobView",
    "PrivilegeMode",
    "SafeError",
    "StepState",
    "StepView",
    "SudoPasswordRequest",
    "SystemFacts",
    "TERMINAL_JOB_STATES",
    "TargetIdentity",
    "TimeoutPolicy",
    "utc_now",
]
