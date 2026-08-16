"""Strict API request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class MediaMTXAuthRequest(StrictModel):
    user: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=2048)
    token: str = Field(default="", max_length=2048)
    ip: str = Field(default="", max_length=128)
    action: str = Field(max_length=32)
    path: str = Field(default="", max_length=1024)
    protocol: str = Field(default="", max_length=32)
    # MediaMTX serializes a missing protocol connection ID as JSON null.
    id: str | None = Field(default=None, max_length=256)
    query: str = Field(default="", max_length=2048)
    userAgent: str = Field(default="", max_length=1024)  # noqa: N815


class DestinationCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    server_url: str = Field(min_length=8, max_length=1024)
    stream_key: str = Field(min_length=1, max_length=1024)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("Название содержит недопустимые символы")
        return value


class DestinationUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    server_url: str | None = Field(default=None, min_length=8, max_length=1024)
    stream_key: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("Название содержит недопустимые символы")
        return value


NodeCapability = Literal["ping", "self_test", "ffmpeg", "ffprobe", "docker"]


def _validate_capabilities(value: list[NodeCapability]) -> list[NodeCapability]:
    if len(value) != len(set(value)):
        raise ValueError("capabilities must not contain duplicates")
    return value


class NodeEnrollmentRequest(StrictModel):
    # Length is checked by the enrollment handler after explicit unwrapping. Keeping
    # constraints off the Pydantic field prevents validation errors from echoing the
    # raw one-time credential as their rejected input value.
    enrollment_token: SecretStr
    agent_version: str = Field(min_length=1, max_length=64)
    protocol_version: int = Field(ge=1, le=65_535)
    hostname: str = Field(min_length=1, max_length=253)
    os_name: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=128)
    architecture: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    cpu_count: int = Field(ge=1, le=1024)
    memory_total_bytes: int = Field(ge=1, le=2**63 - 1)
    memory_available_bytes: int = Field(ge=0, le=2**63 - 1)
    disk_total_bytes: int = Field(ge=1, le=2**63 - 1)
    disk_free_bytes: int = Field(ge=0, le=2**63 - 1)
    capabilities: list[NodeCapability] = Field(default_factory=list, max_length=16)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[NodeCapability]) -> list[NodeCapability]:
        return _validate_capabilities(value)

    @model_validator(mode="after")
    def validate_totals(self) -> NodeEnrollmentRequest:
        if self.memory_available_bytes > self.memory_total_bytes:
            raise ValueError("available memory must not exceed total memory")
        if self.disk_free_bytes > self.disk_total_bytes:
            raise ValueError("free disk must not exceed total disk")
        return self


class NodeHeartbeatRequest(StrictModel):
    agent_version: str = Field(min_length=1, max_length=64)
    protocol_version: int = Field(ge=1, le=65_535)
    hostname: str = Field(min_length=1, max_length=253)
    uptime_seconds: float = Field(ge=0, le=315_576_000)
    load_1m: float = Field(ge=0, le=100_000)
    cpu_percent: float = Field(ge=0, le=100)
    memory_total_bytes: int = Field(ge=1, le=2**63 - 1)
    memory_available_bytes: int = Field(ge=0, le=2**63 - 1)
    disk_total_bytes: int = Field(ge=1, le=2**63 - 1)
    disk_free_bytes: int = Field(ge=0, le=2**63 - 1)
    ffmpeg_version: str | None = Field(default=None, max_length=128)
    ffprobe_version: str | None = Field(default=None, max_length=128)
    capabilities: list[NodeCapability] = Field(default_factory=list, max_length=16)
    current_command_id: UUID | None = None
    control_latency_ms: float | None = Field(ge=0, le=60_000)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[NodeCapability]) -> list[NodeCapability]:
        return _validate_capabilities(value)

    @model_validator(mode="after")
    def validate_totals(self) -> NodeHeartbeatRequest:
        if self.memory_available_bytes > self.memory_total_bytes:
            raise ValueError("available memory must not exceed total memory")
        if self.disk_free_bytes > self.disk_total_bytes:
            raise ValueError("free disk must not exceed total disk")
        return self


class NodeRenameRequest(StrictModel):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("display_name contains control characters")
        return value


class NodeSelfTestChecks(StrictModel):
    control_https: bool
    dns: bool
    ffmpeg: bool
    ffprobe: bool
    memory: bool
    disk: bool
    data_writable: bool
    no_inbound_ports: bool


class NodeCommandAckRequest(StrictModel):
    pass


class NodeCommandCompleteRequest(StrictModel):
    status: Literal["ok", "failed"]
    received_at: datetime | None = None
    completed_at: datetime
    agent_version: str = Field(min_length=1, max_length=64)
    checks: NodeSelfTestChecks | None = None

    @model_validator(mode="after")
    def validate_self_test_status(self) -> NodeCommandCompleteRequest:
        if self.checks is None:
            return self
        checks = self.checks.model_dump()
        if (self.status == "ok") != all(checks.values()):
            raise ValueError("SELF_TEST status must match all checks")
        return self
