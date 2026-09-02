# ruff: noqa: UP017, UP040 -- this native package targets Ubuntu 22.04 Python 3.10.
"""Strict, secret-aware value objects for relay-agent protocol v1."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, TypeAlias, cast
from uuid import UUID

from relay_agent.errors import RelayAgentError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

Action = Literal[
    "STATUS",
    "START",
    "STOP",
    "CONFIGURE_YOUTUBE",
    "CONFIGURE_YOUTUBE_KEY",
    "CLEAR_YOUTUBE",
    "REVEAL_MOBLIN_URL",
]
CompletionStatus = Literal["ok", "failed", "conflict"]

SUPPORTED_ACTIONS = frozenset(
    {
        "STATUS",
        "START",
        "STOP",
        "CONFIGURE_YOUTUBE",
        "CONFIGURE_YOUTUBE_KEY",
        "CLEAR_YOUTUBE",
        "REVEAL_MOBLIN_URL",
    }
)
SAFE_ERROR_CODES = frozenset(
    {
        "relay_active",
        "youtube_not_configured",
        "relayctl_failed",
        "invalid_configuration",
        "command_expired",
        "unsupported_command",
        "internal_error",
    }
)
_YOUTUBE_STREAM_KEY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise RelayAgentError("invalid_protocol")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelayAgentError("invalid_protocol") from exc
    if parsed.tzinfo is None:
        raise RelayAgentError("invalid_protocol")
    return parsed


def is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _finite_number(value: object, *, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


@dataclass(frozen=True, slots=True)
class RelaySnapshot:
    service_state: Literal["active", "inactive", "failed", "unknown"]
    enabled: bool
    main_process: Literal["running", "stopped", "failed", "unknown"]
    srt_listener: Literal["listening", "closed", "failed", "unknown"]
    source: Literal["SLATE", "LIVE", "NONE", "UNKNOWN"]
    youtube_forward: Literal["active", "inactive", "connecting", "failed", "unknown"]
    overall: Literal["ok", "healthy", "degraded", "failed", "offline", "unknown"]
    youtube_url_configured: bool
    youtube_key_configured: bool
    healthy: bool
    portrait_profile: bool
    error_code: str | None = None
    input_bitrate_bps: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.service_state, str) or self.service_state not in {
            "active",
            "inactive",
            "failed",
            "unknown",
        }:
            raise RelayAgentError("invalid_protocol")
        if not isinstance(self.source, str) or self.source not in {
            "SLATE",
            "LIVE",
            "NONE",
            "UNKNOWN",
        }:
            raise RelayAgentError("invalid_protocol")
        if not isinstance(self.main_process, str) or self.main_process not in {
            "running",
            "stopped",
            "failed",
            "unknown",
        }:
            raise RelayAgentError("invalid_protocol")
        if not isinstance(self.srt_listener, str) or self.srt_listener not in {
            "listening",
            "closed",
            "failed",
            "unknown",
        }:
            raise RelayAgentError("invalid_protocol")
        if not isinstance(self.youtube_forward, str) or self.youtube_forward not in {
            "active",
            "inactive",
            "connecting",
            "failed",
            "unknown",
        }:
            raise RelayAgentError("invalid_protocol")
        if not isinstance(self.overall, str) or self.overall not in {
            "ok",
            "healthy",
            "degraded",
            "failed",
            "offline",
            "unknown",
        }:
            raise RelayAgentError("invalid_protocol")
        if not all(
            isinstance(value, bool)
            for value in (
                self.enabled,
                self.youtube_url_configured,
                self.youtube_key_configured,
                self.healthy,
                self.portrait_profile,
            )
        ):
            raise RelayAgentError("invalid_protocol")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or self.error_code not in SAFE_ERROR_CODES
        ):
            raise RelayAgentError("invalid_protocol")
        if self.input_bitrate_bps is not None and (
            self.source != "LIVE"
            or not isinstance(self.input_bitrate_bps, int)
            or isinstance(self.input_bitrate_bps, bool)
            or not 0 <= self.input_bitrate_bps <= 1_000_000_000
        ):
            raise RelayAgentError("invalid_protocol")

    def with_error(self, code: str) -> RelaySnapshot:
        return replace(self, healthy=False, error_code=code)

    def to_json(self) -> JsonObject:
        result: JsonObject = {
            "service_state": self.service_state,
            "enabled": self.enabled,
            "main_process": self.main_process,
            "srt_listener": self.srt_listener,
            "source": self.source,
            "youtube_forward": self.youtube_forward,
            "overall": self.overall,
            "youtube_url_configured": self.youtube_url_configured,
            "youtube_key_configured": self.youtube_key_configured,
            "healthy": self.healthy,
            "portrait_profile": self.portrait_profile,
            "error_code": self.error_code,
        }
        # Omitting an unavailable sample preserves compatibility during a
        # backend-first rollout and prevents a non-LIVE snapshot from claiming
        # input telemetry.
        if self.input_bitrate_bps is not None:
            result["input_bitrate_bps"] = self.input_bitrate_bps
        return result

    @classmethod
    def parse(cls, value: object) -> RelaySnapshot:
        required = {
            "service_state",
            "enabled",
            "main_process",
            "srt_listener",
            "source",
            "youtube_forward",
            "overall",
            "youtube_url_configured",
            "youtube_key_configured",
            "healthy",
            "portrait_profile",
            "error_code",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise RelayAgentError("invalid_protocol")
        extra = set(value) - required
        if extra not in (set(), {"input_bitrate_bps"}):
            raise RelayAgentError("invalid_protocol")
        return cls(
            service_state=cast(
                Literal["active", "inactive", "failed", "unknown"], value["service_state"]
            ),
            enabled=cast(bool, value["enabled"]),
            main_process=cast(
                Literal["running", "stopped", "failed", "unknown"], value["main_process"]
            ),
            srt_listener=cast(
                Literal["listening", "closed", "failed", "unknown"], value["srt_listener"]
            ),
            source=cast(Literal["SLATE", "LIVE", "NONE", "UNKNOWN"], value["source"]),
            youtube_forward=cast(
                Literal["active", "inactive", "connecting", "failed", "unknown"],
                value["youtube_forward"],
            ),
            overall=cast(
                Literal["ok", "healthy", "degraded", "failed", "offline", "unknown"],
                value["overall"],
            ),
            youtube_url_configured=cast(bool, value["youtube_url_configured"]),
            youtube_key_configured=cast(bool, value["youtube_key_configured"]),
            healthy=cast(bool, value["healthy"]),
            portrait_profile=cast(bool, value["portrait_profile"]),
            error_code=cast(str | None, value["error_code"]),
            input_bitrate_bps=cast(int | None, value.get("input_bitrate_bps")),
        )

    @classmethod
    def unavailable(cls, code: str = "internal_error") -> RelaySnapshot:
        return cls(
            "unknown",
            False,
            "unknown",
            "unknown",
            "UNKNOWN",
            "unknown",
            "unknown",
            False,
            False,
            False,
            False,
            code,
        )


@dataclass(frozen=True, slots=True)
class HostMetrics:
    uptime_seconds: float
    load_1m: float
    cpu_percent: float
    memory_total_bytes: int
    memory_available_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int

    def __post_init__(self) -> None:
        if not _finite_number(self.uptime_seconds, minimum=0, maximum=315_576_000):
            raise RelayAgentError("invalid_metrics")
        if not _finite_number(self.load_1m, minimum=0, maximum=100_000):
            raise RelayAgentError("invalid_metrics")
        if not _finite_number(self.cpu_percent, minimum=0, maximum=100):
            raise RelayAgentError("invalid_metrics")
        for value in (
            self.memory_total_bytes,
            self.memory_available_bytes,
            self.disk_total_bytes,
            self.disk_free_bytes,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
                raise RelayAgentError("invalid_metrics")
        if self.memory_total_bytes < 1 or self.memory_available_bytes > self.memory_total_bytes:
            raise RelayAgentError("invalid_metrics")
        if self.disk_total_bytes < 1 or self.disk_free_bytes > self.disk_total_bytes:
            raise RelayAgentError("invalid_metrics")

    def to_json(self) -> JsonObject:
        return {
            "uptime_seconds": self.uptime_seconds,
            "load_1m": self.load_1m,
            "cpu_percent": self.cpu_percent,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
        }


@dataclass(frozen=True, slots=True, repr=False)
class YouTubeConfiguration:
    url: str
    stream_key: str

    def __repr__(self) -> str:
        return "YouTubeConfiguration(url=[REDACTED], stream_key=[REDACTED])"

    def to_broker_payload(self) -> JsonObject:
        return {"youtube_rtmps_url": self.url, "youtube_stream_key": self.stream_key}


@dataclass(frozen=True, slots=True, repr=False)
class YouTubeKeyConfiguration:
    stream_key: str

    def __repr__(self) -> str:
        return "YouTubeKeyConfiguration(stream_key=[REDACTED])"

    def to_broker_payload(self) -> JsonObject:
        return {"youtube_stream_key": self.stream_key}


@dataclass(frozen=True, slots=True, repr=False)
class RelayCommand:
    command_id: str
    action: Action
    lease_seconds: int
    attempt_count: int
    expires_at: datetime
    youtube: YouTubeConfiguration | None = None
    youtube_key: YouTubeKeyConfiguration | None = None

    def __repr__(self) -> str:
        return (
            f"RelayCommand(command_id={self.command_id!r}, action={self.action!r}, "
            "payload=[REDACTED])"
        )

    @classmethod
    def parse(cls, value: object) -> RelayCommand:
        if not isinstance(value, dict) or set(value) != {
            "id",
            "action",
            "payload",
            "lease_seconds",
            "attempt_count",
            "expires_at",
        }:
            raise RelayAgentError("invalid_protocol")
        command_id = value["id"]
        action = value["action"]
        payload = value["payload"]
        lease_seconds = value["lease_seconds"]
        attempt_count = value["attempt_count"]
        if (
            not is_uuid(command_id)
            or not isinstance(action, str)
            or action not in SUPPORTED_ACTIONS
        ):
            raise RelayAgentError("unsupported_command")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 300
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or not 1 <= attempt_count <= 1000
        ):
            raise RelayAgentError("invalid_protocol")
        youtube: YouTubeConfiguration | None = None
        youtube_key: YouTubeKeyConfiguration | None = None
        if action == "CONFIGURE_YOUTUBE":
            if not isinstance(payload, dict) or set(payload) != {
                "youtube_rtmps_url",
                "youtube_stream_key",
            }:
                raise RelayAgentError("invalid_protocol")
            url = payload["youtube_rtmps_url"]
            stream_key = payload["youtube_stream_key"]
            if (
                not isinstance(url, str)
                or not 1 <= len(url) <= 2048
                or not isinstance(stream_key, str)
                or not 1 <= len(stream_key) <= 256
            ):
                raise RelayAgentError("invalid_protocol")
            youtube = YouTubeConfiguration(url, stream_key)
        elif action == "CONFIGURE_YOUTUBE_KEY":
            if not isinstance(payload, dict) or set(payload) != {"youtube_stream_key"}:
                raise RelayAgentError("invalid_protocol")
            stream_key = payload["youtube_stream_key"]
            if not isinstance(stream_key, str) or _YOUTUBE_STREAM_KEY.fullmatch(stream_key) is None:
                raise RelayAgentError("invalid_protocol")
            youtube_key = YouTubeKeyConfiguration(stream_key)
        elif not isinstance(payload, dict) or payload:
            raise RelayAgentError("invalid_protocol")
        return cls(
            command_id=cast(str, command_id),
            action=cast(Action, action),
            lease_seconds=lease_seconds,
            attempt_count=attempt_count,
            expires_at=parse_timestamp(value["expires_at"]),
            youtube=youtube,
            youtube_key=youtube_key,
        )

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


@dataclass(frozen=True, slots=True, repr=False)
class RelayCompletion:
    status: CompletionStatus
    completed_at: str
    safe_result: RelaySnapshot
    secret_result: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ok", "failed", "conflict"}:
            raise RelayAgentError("invalid_protocol")
        parse_timestamp(self.completed_at)
        if self.secret_result is not None and not 1 <= len(self.secret_result) <= 4096:
            raise RelayAgentError("invalid_protocol")

    def __repr__(self) -> str:
        return (
            f"RelayCompletion(status={self.status!r}, completed_at={self.completed_at!r}, "
            f"safe_result={self.safe_result!r}, secret_result=[REDACTED])"
        )

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "completed_at": self.completed_at,
            "safe_result": self.safe_result.to_json(),
            "secret_result": self.secret_result,
        }
