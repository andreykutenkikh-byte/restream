"""Strict protocol-v1 value objects."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from node_agent.credentials import SensitiveToken
from node_agent.errors import ProtocolError
from node_agent.settings import HEARTBEAT_INTERVAL_SECONDS, PROTOCOL_VERSION

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

AGENT_CAPABILITIES = ("ping", "self_test", "ffmpeg", "ffprobe")
SUPPORTED_COMMANDS = frozenset({"PING", "SELF_TEST"})
SELF_TEST_CHECKS = (
    "control_https",
    "dns",
    "ffmpeg",
    "ffprobe",
    "memory",
    "disk",
    "data_writable",
    "no_inbound_ports",
)
MAX_CONTROL_LATENCY_MS = 60_000.0

_ARCHITECTURE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(character.isprintable() for character in value)
    )


def _bounded_int(value: object, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _bounded_number(value: object, *, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= float(value) <= maximum
    )


def _is_uuid(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value


def _is_timestamp(value: object) -> bool:
    if not _is_text(value, maximum=64):
        return False
    try:
        parsed = datetime.fromisoformat(cast(str, value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    hostname: str
    os_name: str
    os_version: str
    architecture: str
    cpu_count: int
    uptime_seconds: float
    load_1m: float
    cpu_percent: float
    memory_total_bytes: int
    memory_available_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    ffmpeg_version: str | None
    ffprobe_version: str | None

    def __post_init__(self) -> None:
        for identity_name, identity_value, maximum in (
            ("hostname", self.hostname, 253),
            ("OS name", self.os_name, 64),
            ("OS version", self.os_version, 128),
        ):
            if not _is_text(identity_value, maximum=maximum):
                raise ProtocolError("invalid_metrics", f"{identity_name} is invalid")
        if not _ARCHITECTURE_PATTERN.fullmatch(self.architecture):
            raise ProtocolError("invalid_metrics", "architecture is invalid")
        for tool_name, tool_value in (
            ("FFmpeg version", self.ffmpeg_version),
            ("ffprobe version", self.ffprobe_version),
        ):
            if tool_value is not None and not _is_text(tool_value, maximum=128):
                raise ProtocolError("invalid_metrics", f"{tool_name} is invalid")
        if not _bounded_int(self.cpu_count, minimum=1, maximum=1024):
            raise ProtocolError("invalid_metrics", "CPU count is invalid")
        if not _bounded_number(self.uptime_seconds, minimum=0, maximum=315_576_000):
            raise ProtocolError("invalid_metrics", "uptime is invalid")
        if not _bounded_number(self.load_1m, minimum=0, maximum=100_000):
            raise ProtocolError("invalid_metrics", "load is invalid")
        if not _bounded_number(self.cpu_percent, minimum=0, maximum=100):
            raise ProtocolError("invalid_metrics", "CPU percent is invalid")
        for metric_name, metric_value in (
            ("memory total", self.memory_total_bytes),
            ("memory available", self.memory_available_bytes),
            ("disk total", self.disk_total_bytes),
            ("disk free", self.disk_free_bytes),
        ):
            if not _bounded_int(metric_value, minimum=0, maximum=2**63 - 1):
                raise ProtocolError("invalid_metrics", f"{metric_name} is invalid")
        if self.memory_total_bytes == 0 or self.memory_available_bytes > self.memory_total_bytes:
            raise ProtocolError("invalid_metrics", "memory values are inconsistent")
        if self.disk_total_bytes == 0 or self.disk_free_bytes > self.disk_total_bytes:
            raise ProtocolError("invalid_metrics", "disk values are inconsistent")

    def enrollment_payload(
        self, *, enrollment_token: SensitiveToken, agent_version: str
    ) -> JsonObject:
        return {
            "enrollment_token": enrollment_token.reveal(),
            "agent_version": agent_version,
            "protocol_version": PROTOCOL_VERSION,
            "hostname": self.hostname,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "cpu_count": self.cpu_count,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "capabilities": list(AGENT_CAPABILITIES),
        }

    def heartbeat_payload(
        self,
        *,
        agent_version: str,
        current_command_id: str | None,
        control_latency_ms: float | None,
    ) -> JsonObject:
        if current_command_id is not None and not _is_uuid(current_command_id):
            raise ProtocolError("invalid_command", "current command id is invalid")
        if control_latency_ms is not None and not _bounded_number(
            control_latency_ms,
            minimum=0,
            maximum=MAX_CONTROL_LATENCY_MS,
        ):
            raise ProtocolError("invalid_metrics", "control latency is invalid")
        return {
            "agent_version": agent_version,
            "protocol_version": PROTOCOL_VERSION,
            "hostname": self.hostname,
            "uptime_seconds": self.uptime_seconds,
            "load_1m": self.load_1m,
            "cpu_percent": self.cpu_percent,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_version": self.ffprobe_version,
            "capabilities": list(AGENT_CAPABILITIES),
            "current_command_id": current_command_id,
            "control_latency_ms": control_latency_ms,
        }


@dataclass(frozen=True, slots=True, repr=False)
class EnrollmentResponse:
    node_id: str
    node_token: SensitiveToken
    heartbeat_interval_seconds: int
    command_poll_interval_seconds: int

    def __repr__(self) -> str:
        return (
            "EnrollmentResponse(node_id="
            f"{self.node_id!r}, node_token=[REDACTED], "
            f"heartbeat_interval_seconds={self.heartbeat_interval_seconds!r}, "
            f"command_poll_interval_seconds={self.command_poll_interval_seconds!r})"
        )

    @classmethod
    def parse(cls, payload: object) -> EnrollmentResponse:
        if not isinstance(payload, dict):
            raise ProtocolError("invalid_enrollment_response", "enrollment response is invalid")
        node_id = payload.get("node_id")
        raw_token = payload.get("node_token")
        heartbeat_interval = payload.get("heartbeat_interval_seconds")
        command_poll_interval = payload.get("command_poll_interval_seconds")
        if not isinstance(node_id, str) or not _is_uuid(node_id):
            raise ProtocolError("invalid_enrollment_response", "node id is invalid")
        if not isinstance(raw_token, str):
            raise ProtocolError("invalid_enrollment_response", "node credential is invalid")
        if heartbeat_interval != HEARTBEAT_INTERVAL_SECONDS:
            raise ProtocolError(
                "invalid_enrollment_response", "heartbeat interval is incompatible with protocol v1"
            )
        if not _bounded_int(command_poll_interval, minimum=1, maximum=20):
            raise ProtocolError("invalid_enrollment_response", "command poll interval is invalid")
        return cls(
            node_id=node_id,
            node_token=SensitiveToken.parse(raw_token),
            heartbeat_interval_seconds=cast(int, heartbeat_interval),
            command_poll_interval_seconds=cast(int, command_poll_interval),
        )


@dataclass(frozen=True, slots=True)
class NodeCommand:
    command_id: str
    command_type: Literal["PING", "SELF_TEST"]
    lease_seconds: int
    attempt_count: int
    received_at: str

    @classmethod
    def parse(cls, payload: object) -> NodeCommand:
        if not isinstance(payload, dict):
            raise ProtocolError("invalid_command", "command response is invalid")
        command_id = payload.get("id")
        command_type = payload.get("command_type")
        command_payload = payload.get("payload")
        lease_seconds = payload.get("lease_seconds")
        attempt_count = payload.get("attempt_count")
        if not isinstance(command_id, str) or not _is_uuid(command_id):
            raise ProtocolError("invalid_command", "command id is invalid")
        if command_type not in SUPPORTED_COMMANDS:
            raise ProtocolError("unsupported_command", "command type is not supported")
        if not isinstance(command_payload, dict) or command_payload:
            raise ProtocolError("invalid_command", "command payload must be empty")
        if not _bounded_int(lease_seconds, minimum=1, maximum=300):
            raise ProtocolError("invalid_command", "command lease is invalid")
        if not _bounded_int(attempt_count, minimum=1, maximum=1000):
            raise ProtocolError("invalid_command", "command attempt is invalid")
        return cls(
            command_id=command_id,
            command_type=cast(Literal["PING", "SELF_TEST"], command_type),
            lease_seconds=cast(int, lease_seconds),
            attempt_count=cast(int, attempt_count),
            received_at=utc_timestamp(),
        )


@dataclass(frozen=True, slots=True)
class CommandCompletion:
    status: Literal["ok", "failed"]
    received_at: str | None
    completed_at: str
    agent_version: str
    checks: dict[str, bool] | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ok", "failed"}:
            raise ProtocolError("invalid_command_result", "command status is invalid")
        if not _is_timestamp(self.completed_at):
            raise ProtocolError("invalid_command_result", "completion timestamp is invalid")
        if self.received_at is not None and not _is_timestamp(self.received_at):
            raise ProtocolError("invalid_command_result", "received timestamp is invalid")
        if not _is_text(self.agent_version, maximum=64) or not self.agent_version.isascii():
            raise ProtocolError("invalid_command_result", "agent version is invalid")
        if self.checks is None:
            if self.received_at is None:
                raise ProtocolError("invalid_command_result", "PING result is invalid")
            return
        if (
            self.received_at is not None
            or tuple(self.checks) != SELF_TEST_CHECKS
            or not all(isinstance(value, bool) for value in self.checks.values())
        ):
            raise ProtocolError("invalid_command_result", "self-test result is invalid")
        if (self.status == "ok") != all(self.checks.values()):
            raise ProtocolError(
                "invalid_command_result", "self-test status does not match its checks"
            )

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "status": self.status,
            "received_at": self.received_at,
            "completed_at": self.completed_at,
            "agent_version": self.agent_version,
            "checks": None,
        }
        if self.checks is not None:
            payload["checks"] = cast(JsonValue, self.checks.copy())
        return payload

    @classmethod
    def parse_stored(cls, payload: object) -> CommandCompletion:
        if not isinstance(payload, dict):
            raise ProtocolError("invalid_command_result", "stored command result is invalid")
        status = payload.get("status")
        received_at = payload.get("received_at")
        completed_at = payload.get("completed_at")
        agent_version = payload.get("agent_version")
        raw_checks = payload.get("checks")
        if status not in {"ok", "failed"}:
            raise ProtocolError("invalid_command_result", "stored command status is invalid")
        if not isinstance(completed_at, str) or not isinstance(agent_version, str):
            raise ProtocolError("invalid_command_result", "stored command result is invalid")
        if received_at is not None and not isinstance(received_at, str):
            raise ProtocolError("invalid_command_result", "stored command result is invalid")
        checks: dict[str, bool] | None = None
        if raw_checks is not None:
            if not isinstance(raw_checks, dict):
                raise ProtocolError("invalid_command_result", "stored self-test checks are invalid")
            checks = {}
            for key in SELF_TEST_CHECKS:
                value = raw_checks.get(key)
                if not isinstance(value, bool):
                    raise ProtocolError(
                        "invalid_command_result", "stored self-test checks are invalid"
                    )
                checks[key] = value
            if set(raw_checks) != set(SELF_TEST_CHECKS):
                raise ProtocolError("invalid_command_result", "stored self-test checks are invalid")
        return cls(
            status=cast(Literal["ok", "failed"], status),
            received_at=received_at,
            completed_at=completed_at,
            agent_version=agent_version,
            checks=checks,
        )
