"""Strict API request and response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit
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


NodeCapability = Literal[
    "ping",
    "self_test",
    "ffmpeg",
    "ffprobe",
    "docker",
    "moblin_relay",
]


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


RelayCommandType = Literal[
    "STATUS",
    "START",
    "STOP",
    "CONFIGURE_YOUTUBE",
    "CONFIGURE_YOUTUBE_KEY",
    "REVEAL_MOBLIN_URL",
    "CLEAR_YOUTUBE",
]
RelayServiceState = Literal["active", "inactive", "failed", "unknown"]
RelaySource = Literal["SLATE", "LIVE", "NONE", "UNKNOWN"]
RelayMainProcess = Literal["running", "stopped", "failed", "unknown"]
RelaySRTListener = Literal["listening", "closed", "failed", "unknown"]
RelayYouTubeForward = Literal["active", "inactive", "connecting", "failed", "unknown"]
RelayOverall = Literal["ok", "healthy", "degraded", "failed", "offline", "unknown"]
RelayErrorCode = Literal[
    "relay_active",
    "youtube_not_configured",
    "relayctl_failed",
    "invalid_configuration",
    "command_expired",
    "unsupported_command",
    "internal_error",
]


class RelaySafeState(StrictModel):
    """Agent state that is safe to persist and return to an administrator."""

    service_state: RelayServiceState
    enabled: bool
    main_process: RelayMainProcess
    srt_listener: RelaySRTListener
    source: RelaySource
    youtube_forward: RelayYouTubeForward
    overall: RelayOverall
    youtube_url_configured: bool
    youtube_key_configured: bool
    healthy: bool
    portrait_profile: bool
    error_code: RelayErrorCode | None = None
    input_bitrate_bps: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=1_000_000_000,
    )

    @model_validator(mode="after")
    def validate_input_bitrate(self) -> RelaySafeState:
        if self.source != "LIVE" and self.input_bitrate_bps is not None:
            raise ValueError("input bitrate is valid only for a LIVE source")
        return self


class RelayHostMetrics(StrictModel):
    uptime_seconds: float = Field(ge=0, le=315_576_000)
    load_1m: float = Field(ge=0, le=100_000)
    cpu_percent: float = Field(ge=0, le=100)
    memory_total_bytes: int = Field(ge=1, le=2**63 - 1)
    memory_available_bytes: int = Field(ge=0, le=2**63 - 1)
    disk_total_bytes: int = Field(ge=1, le=2**63 - 1)
    disk_free_bytes: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_totals(self) -> RelayHostMetrics:
        if self.memory_available_bytes > self.memory_total_bytes:
            raise ValueError("available memory must not exceed total memory")
        if self.disk_free_bytes > self.disk_total_bytes:
            raise ValueError("free disk must not exceed total disk")
        return self


class RelayHeartbeatRequest(StrictModel):
    agent_version: str = Field(min_length=1, max_length=64)
    protocol_version: Literal[1]
    hostname: str = Field(min_length=1, max_length=253)
    relay: RelaySafeState
    host: RelayHostMetrics
    current_command_id: UUID | None = None


class RelayCommandAckRequest(StrictModel):
    pass


class RelayCommandCompleteRequest(StrictModel):
    status: Literal["ok", "failed", "conflict"]
    completed_at: datetime
    safe_result: RelaySafeState
    # SecretStr prevents FastAPI/Pydantic diagnostics from rendering an SRT URL.
    # The service accepts this field only for a successful REVEAL_MOBLIN_URL.
    secret_result: SecretStr | None = None

    @field_validator("secret_result")
    @classmethod
    def validate_secret_result(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plaintext = value.get_secret_value().strip()
        if not 1 <= len(plaintext) <= 4096:
            raise ValueError("secret_result length is invalid")
        return SecretStr(plaintext)


YOUTUBE_RTMPS_HOSTS = frozenset({"a.rtmps.youtube.com", "b.rtmps.youtube.com"})


def _normalize_youtube_rtmps_url(value: SecretStr) -> SecretStr:
    raw = value.get_secret_value()
    normalized = "".join(raw.split())
    if not normalized or len(normalized) > 1024 or "#" in normalized or "\\" in normalized:
        raise ValueError("invalid YouTube RTMPS URL")
    parsed = urlsplit(normalized)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid YouTube RTMPS URL") from exc
    if (
        parsed.scheme.lower() != "rtmps"
        or parsed.hostname is None
        or parsed.hostname.lower() not in YOUTUBE_RTMPS_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port not in {None, 443}
        or parsed.path != "/live2"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid YouTube RTMPS URL")
    try:
        ip_address(parsed.hostname)
    except ValueError:
        pass
    else:  # pragma: no cover - the hostname allowlist already excludes IP literals
        raise ValueError("invalid YouTube RTMPS URL")
    canonical_host = parsed.hostname.lower()
    port = ":443" if parsed_port == 443 else ""
    return SecretStr(f"rtmps://{canonical_host}{port}/live2")


def _normalize_youtube_stream_key(value: SecretStr) -> SecretStr:
    normalized = "".join(value.get_secret_value().split())
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", normalized) is None:
        raise ValueError("invalid YouTube stream key")
    return SecretStr(normalized)


def _validate_admin_password(value: SecretStr) -> SecretStr:
    plaintext = value.get_secret_value()
    if not 1 <= len(plaintext) <= 1024:
        raise ValueError("invalid administrator password")
    return value


class RelayConfigureYouTubeRequest(StrictModel):
    # Both values use SecretStr so a failed validation can never echo a submitted key.
    url: SecretStr
    stream_key: SecretStr
    # Deprecated compatibility field for older cached frontends. Authentication
    # is provided by the administrator session, CSRF token, and same-origin gate.
    admin_password: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def validate_youtube_rtmps_url(cls, value: SecretStr) -> SecretStr:
        return _normalize_youtube_rtmps_url(value)

    @field_validator("stream_key")
    @classmethod
    def validate_youtube_stream_key(cls, value: SecretStr) -> SecretStr:
        return _normalize_youtube_stream_key(value)

    @field_validator("admin_password")
    @classmethod
    def validate_legacy_admin_password(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else _validate_admin_password(value)


class RelayConfigureYouTubeKeyRequest(StrictModel):
    stream_key: SecretStr
    # Accepted but ignored while cached pre-update pages age out.
    admin_password: SecretStr | None = None

    @field_validator("stream_key")
    @classmethod
    def validate_youtube_stream_key(cls, value: SecretStr) -> SecretStr:
        return _normalize_youtube_stream_key(value)

    @field_validator("admin_password")
    @classmethod
    def validate_legacy_admin_password(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else _validate_admin_password(value)


class RelayRevealMoblinURLRequest(StrictModel):
    # Accepted but ignored while cached pre-update pages age out.
    admin_password: SecretStr | None = None

    @field_validator("admin_password")
    @classmethod
    def validate_legacy_admin_password(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else _validate_admin_password(value)


class RelayStepUpRequest(StrictModel):
    admin_password: SecretStr

    @field_validator("admin_password")
    @classmethod
    def validate_admin_password(cls, value: SecretStr) -> SecretStr:
        return _validate_admin_password(value)
