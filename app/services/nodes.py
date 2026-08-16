"""Persistence and state transitions for enrolled restream nodes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast
from uuid import UUID, uuid4

from app.core.security import (
    digest_opaque_token,
    generate_enrollment_token,
    generate_node_token,
    verify_opaque_token_digest,
)
from app.db import Database

SUPPORTED_PROTOCOL_VERSION: Final = 1
ENROLLMENT_TTL_SECONDS: Final = 600
HEARTBEAT_INTERVAL_SECONDS: Final = 5
COMMAND_POLL_INTERVAL_SECONDS: Final = 5
HEARTBEAT_MIN_INTERVAL_SECONDS: Final = 1
COMMAND_LEASE_SECONDS: Final = 30
COMMAND_MAX_ATTEMPTS: Final = 3
COMMAND_MAX_AGE_SECONDS: Final = 300

NodeStatus = Literal[
    "installing", "connecting", "ready", "degraded", "offline", "revoked", "failed"
]
CommandType = Literal["PING", "SELF_TEST"]


class NodeDomainError(RuntimeError):
    """Base class for safe node-domain failures."""


class NodeNotFoundError(NodeDomainError):
    """Raised when a node does not exist."""


class NodeAuthenticationError(NodeDomainError):
    """Raised for every permanent token authentication failure."""


class NodeUnavailableError(NodeDomainError):
    """Raised when an authenticated command target is not yet operable."""


class EnrollmentTokenError(NodeDomainError):
    """Raised for invalid, expired, or replayed enrollment tokens."""


class UnsupportedProtocolError(NodeDomainError):
    """Raised when an agent uses an unsupported protocol version."""


class HeartbeatRateLimitError(NodeDomainError):
    """Raised when a node sends heartbeat snapshots too frequently."""


class CommandNotFoundError(NodeDomainError):
    """Raised when a command does not belong to an authenticated node."""


class CommandStateError(NodeDomainError):
    """Raised for an invalid or conflicting command transition."""


@dataclass(frozen=True, slots=True)
class EnrollmentGrant:
    node_id: str
    node_token: str
    heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS
    command_poll_interval_seconds: int = COMMAND_POLL_INTERVAL_SECONDS


Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json(value: Mapping[str, Any] | list[str]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _safe_command_result(command_type: str, result: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {"status", "received_at", "completed_at", "agent_version", "checks"}
    if set(result) != expected_fields:
        raise CommandStateError("command result fields are invalid")
    status_value = result.get("status")
    completed_at = result.get("completed_at")
    agent_version = result.get("agent_version")
    if status_value not in {"ok", "failed"}:
        raise CommandStateError("command result status is invalid")
    if not isinstance(completed_at, str) or not completed_at or len(completed_at) > 64:
        raise CommandStateError("command completion timestamp is invalid")
    if not isinstance(agent_version, str) or not 1 <= len(agent_version) <= 64:
        raise CommandStateError("command agent version is invalid")
    received_at = result.get("received_at")
    checks = result.get("checks")
    if command_type == "PING":
        if not isinstance(received_at, str) or not received_at or checks is not None:
            raise CommandStateError("PING result is invalid")
    elif command_type == "SELF_TEST":
        expected_checks = {
            "control_https",
            "dns",
            "ffmpeg",
            "ffprobe",
            "memory",
            "disk",
            "data_writable",
            "no_inbound_ports",
        }
        if received_at is not None or not isinstance(checks, Mapping):
            raise CommandStateError("SELF_TEST result is invalid")
        if set(checks) != expected_checks or any(
            not isinstance(value, bool) for value in checks.values()
        ):
            raise CommandStateError("SELF_TEST checks are invalid")
        if (status_value == "ok") != all(checks.values()):
            raise CommandStateError("SELF_TEST status does not match its checks")
    else:  # pragma: no cover - command_type is constrained by SQLite
        raise CommandStateError("command type is invalid")
    return dict(result)


class NodeService:
    """Coordinates atomic enrollment, heartbeat, command, and revoke transitions."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = _now,
        event_limit: int = 1000,
    ) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self.database = database
        self.clock = clock
        self.event_limit = event_limit

    def _time(self) -> datetime:
        return _as_utc(self.clock())

    def _add_event(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        event_type: str,
        safe_detail: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO node_events(node_id, event_type, safe_detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (node_id, event_type, safe_detail, _timestamp(self._time())),
        )
        connection.execute(
            """
            DELETE FROM node_events
            WHERE id NOT IN (
                SELECT id FROM node_events ORDER BY id DESC LIMIT ?
            )
            """,
            (self.event_limit,),
        )

    def create_pending_node(
        self,
        *,
        display_name: str,
        address: str,
        resolved_ip: str,
        ssh_port: int,
        ssh_username: str,
        host_key_algorithm: str | None = None,
        host_key_fingerprint: str | None = None,
        host_key_trust_mode: str | None = None,
        status: NodeStatus = "installing",
        node_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = node_id or str(uuid4())
        UUID(identifier)
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO restream_nodes(
                    id, display_name, address, resolved_ip, ssh_port, ssh_username,
                    host_key_algorithm, host_key_fingerprint, host_key_trust_mode,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    display_name,
                    address,
                    resolved_ip,
                    ssh_port,
                    ssh_username,
                    host_key_algorithm,
                    host_key_fingerprint,
                    host_key_trust_mode,
                    status,
                    now,
                    now,
                ),
            )
            self._add_event(connection, identifier, "node.created")
            connection.execute("COMMIT")
        node = self.get_node(identifier)
        if node is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("node was not persisted")
        return node

    def issue_enrollment(self, node_id: str) -> str:
        """Persist only a digest and return the one-time token to the caller."""

        token = generate_enrollment_token()
        digest = digest_opaque_token(token)
        now = self._time()
        now_text = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=ENROLLMENT_TTL_SECONDS))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            node = connection.execute(
                "SELECT status FROM restream_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if node is None:
                connection.execute("ROLLBACK")
                raise NodeNotFoundError("node not found")
            if node["status"] == "revoked":
                connection.execute("ROLLBACK")
                raise NodeAuthenticationError("node access is revoked")
            connection.execute(
                """
                UPDATE node_enrollment_tokens
                SET used_at = ?
                WHERE node_id = ? AND used_at IS NULL
                """,
                (now_text, node_id),
            )
            connection.execute(
                """
                INSERT INTO node_enrollment_tokens(
                    id, node_id, token_digest, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid4()), node_id, digest, expires_at, now_text),
            )
            self._add_event(connection, node_id, "node.enrollment_issued")
            connection.execute("COMMIT")
        return token

    def enroll(
        self,
        enrollment_token: str,
        *,
        public_ip: str,
        profile: Mapping[str, Any],
    ) -> EnrollmentGrant:
        """Consume an enrollment token and issue a permanent credential exactly once."""

        if profile.get("protocol_version") != SUPPORTED_PROTOCOL_VERSION:
            raise UnsupportedProtocolError("unsupported node protocol version")
        try:
            supplied_digest = digest_opaque_token(enrollment_token)
        except (TypeError, ValueError):
            verify_opaque_token_digest("invalid", "0" * 64)
            raise EnrollmentTokenError("enrollment token is invalid or expired") from None

        def lookup(connection: sqlite3.Connection) -> sqlite3.Row | None:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT token.id, token.node_id, token.token_digest, token.expires_at,
                           token.used_at, node.status
                    FROM node_enrollment_tokens AS token
                    JOIN restream_nodes AS node ON node.id = token.node_id
                    WHERE token.token_digest = ?
                    """,
                    (supplied_digest,),
                ).fetchone(),
            )

        def is_eligible(row: sqlite3.Row | None, current_time: str) -> bool:
            expected_digest = str(row["token_digest"]) if row else "0" * 64
            valid_digest = verify_opaque_token_digest(enrollment_token, expected_digest)
            return bool(
                row is not None
                and valid_digest
                and row["used_at"] is None
                and str(row["expires_at"]) > current_time
                and row["status"] != "revoked"
            )

        # Random public input never asks SQLite for a writer lock. The indexed
        # digest candidate is first rejected through a short read-only lookup.
        candidate_time = _timestamp(self._time())
        with self.database.connect() as connection:
            candidate = lookup(connection)
        if not is_eligible(candidate, candidate_time):
            raise EnrollmentTokenError("enrollment token is invalid or expired")

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_text = _timestamp(self._time())
            row = lookup(connection)
            if row is None or not is_eligible(row, now_text):
                connection.execute("ROLLBACK")
                raise EnrollmentTokenError("enrollment token is invalid or expired")

            node_id = str(row["node_id"])
            node_token = generate_node_token(node_id)
            credential_digest = digest_opaque_token(node_token)
            consumed = connection.execute(
                """
                UPDATE node_enrollment_tokens
                SET used_at = ?
                WHERE id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (now_text, row["id"], now_text),
            )
            if consumed.rowcount != 1:
                connection.execute("ROLLBACK")
                raise EnrollmentTokenError("enrollment token is invalid or expired")
            connection.execute(
                """
                INSERT INTO node_credentials(node_id, token_digest, issued_at)
                VALUES (?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    token_digest = excluded.token_digest,
                    issued_at = excluded.issued_at,
                    last_rotated_at = excluded.issued_at,
                    revoked_at = NULL
                """,
                (node_id, credential_digest, now_text),
            )
            connection.execute(
                """
                UPDATE restream_nodes
                SET status = 'connecting', public_ip = ?,
                    hostname = COALESCE(hostname, ?),
                    os_name = COALESCE(os_name, ?),
                    os_version = COALESCE(os_version, ?),
                    architecture = COALESCE(architecture, ?),
                    cpu_count = COALESCE(cpu_count, ?),
                    memory_total_bytes = COALESCE(memory_total_bytes, ?),
                    memory_available_bytes = COALESCE(memory_available_bytes, ?),
                    disk_total_bytes = COALESCE(disk_total_bytes, ?),
                    disk_free_bytes = COALESCE(disk_free_bytes, ?), agent_version = ?,
                    protocol_version = ?, capabilities_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    public_ip,
                    profile["hostname"],
                    profile["os_name"],
                    profile["os_version"],
                    profile["architecture"],
                    profile["cpu_count"],
                    profile["memory_total_bytes"],
                    profile["memory_available_bytes"],
                    profile["disk_total_bytes"],
                    profile["disk_free_bytes"],
                    profile["agent_version"],
                    profile["protocol_version"],
                    _json(list(profile["capabilities"])),
                    now_text,
                    node_id,
                ),
            )
            self._add_event(connection, node_id, "node.enrolled")
            connection.execute("COMMIT")
        return EnrollmentGrant(node_id=node_id, node_token=node_token)

    def authenticate(
        self,
        node_token: str | None,
        *,
        require_supported_protocol: bool = False,
    ) -> dict[str, Any]:
        """Resolve the node id prefix and compare the persisted digest in constant time."""

        node_id = ""
        if isinstance(node_token, str):
            parts = node_token.split("_", 2)
            if len(parts) == 3 and parts[0] == "node" and parts[1] and parts[2]:
                try:
                    node_id = str(UUID(parts[1]))
                except ValueError:
                    node_id = ""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT credential.node_id, credential.token_digest, credential.revoked_at,
                       node.status, node.protocol_version
                FROM node_credentials AS credential
                JOIN restream_nodes AS node ON node.id = credential.node_id
                WHERE credential.node_id = ?
                """,
                (node_id,),
            ).fetchone()
        expected_digest = str(row["token_digest"]) if row else "0" * 64
        if (
            row is None
            or not verify_opaque_token_digest(node_token, expected_digest)
            or row["revoked_at"] is not None
            or row["status"] == "revoked"
        ):
            raise NodeAuthenticationError("node authentication failed")
        if require_supported_protocol and row["protocol_version"] != SUPPORTED_PROTOCOL_VERSION:
            raise UnsupportedProtocolError("unsupported node protocol version")
        return dict(row)

    def record_heartbeat(
        self,
        node_token: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        authenticated = self.authenticate(node_token)
        if snapshot.get("protocol_version") != SUPPORTED_PROTOCOL_VERSION:
            raise UnsupportedProtocolError("unsupported node protocol version")
        node_id = str(authenticated["node_id"])
        now = self._time()
        now_text = _timestamp(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            node = connection.execute(
                "SELECT status, last_seen_at FROM restream_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if node is None:  # pragma: no cover - credential foreign key invariant
                connection.execute("ROLLBACK")
                raise NodeAuthenticationError("node authentication failed")
            if node["status"] == "revoked":
                connection.execute("ROLLBACK")
                raise NodeAuthenticationError("node authentication failed")
            if node["last_seen_at"] is not None:
                previous = _parse_timestamp(str(node["last_seen_at"]))
                if (now - previous).total_seconds() < HEARTBEAT_MIN_INTERVAL_SECONDS:
                    connection.execute("ROLLBACK")
                    raise HeartbeatRateLimitError("heartbeat rate limit exceeded")
            connection.execute(
                """
                UPDATE restream_nodes
                SET status = 'ready', hostname = COALESCE(hostname, ?),
                    agent_version = ?, protocol_version = ?,
                    cpu_count = COALESCE(cpu_count, ?), uptime_seconds = ?, load_1m = ?,
                    cpu_percent = ?,
                    memory_total_bytes = COALESCE(memory_total_bytes, ?),
                    memory_available_bytes = ?,
                    disk_total_bytes = COALESCE(disk_total_bytes, ?),
                    disk_free_bytes = ?, ffmpeg_version = ?,
                    ffprobe_version = ?, capabilities_json = ?, current_command_id = ?,
                    control_latency_ms = ?,
                    last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    snapshot["hostname"],
                    snapshot["agent_version"],
                    snapshot["protocol_version"],
                    snapshot.get("cpu_count"),
                    snapshot["uptime_seconds"],
                    snapshot["load_1m"],
                    snapshot["cpu_percent"],
                    snapshot["memory_total_bytes"],
                    snapshot["memory_available_bytes"],
                    snapshot["disk_total_bytes"],
                    snapshot["disk_free_bytes"],
                    snapshot["ffmpeg_version"],
                    snapshot["ffprobe_version"],
                    _json(list(snapshot["capabilities"])),
                    snapshot["current_command_id"],
                    snapshot["control_latency_ms"],
                    now_text,
                    now_text,
                    node_id,
                ),
            )
            if node["status"] != "ready":
                self._add_event(connection, node_id, "node.ready")
            connection.execute("COMMIT")
        return {"node_id": node_id, "node_status": "ready", "server_time": now_text}

    def _node_view(self, row: sqlite3.Row, *, now: datetime) -> dict[str, Any]:
        view = dict(row)
        status = str(view["status"])
        last_seen = view.get("last_seen_at")
        if status not in {"revoked", "failed", "installing", "connecting"} and last_seen:
            age = (now - _parse_timestamp(str(last_seen))).total_seconds()
            if age > 30:
                status = "offline"
            elif age > 15:
                status = "degraded"
            else:
                status = "ready"
        view["status"] = status
        try:
            capabilities = json.loads(str(view.pop("capabilities_json")))
        except (TypeError, ValueError):  # pragma: no cover - only corrupted storage
            capabilities = []
        view["capabilities"] = capabilities if isinstance(capabilities, list) else []
        return view

    def list_nodes(self) -> list[dict[str, Any]]:
        now = self._time()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM restream_nodes ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._node_view(row, now=now) for row in rows]

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        now = self._time()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM restream_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return self._node_view(row, now=now) if row else None

    def rename_node(self, node_id: str, display_name: str) -> dict[str, Any]:
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE restream_nodes SET display_name = ?, updated_at = ? WHERE id = ?",
                (display_name, now, node_id),
            )
            if cursor.rowcount != 1:
                raise NodeNotFoundError("node not found")
        node = self.get_node(node_id)
        if node is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("node disappeared after rename")
        return node

    def create_command(self, node_id: str, command_type: CommandType) -> dict[str, Any]:
        if command_type not in {"PING", "SELF_TEST"}:
            raise ValueError("unsupported command type")
        command_id = str(uuid4())
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            node = connection.execute(
                """
                SELECT node.status, node.protocol_version,
                       credential.node_id AS credential_node_id,
                       credential.revoked_at AS credential_revoked_at
                FROM restream_nodes AS node
                LEFT JOIN node_credentials AS credential ON credential.node_id = node.id
                WHERE node.id = ?
                """,
                (node_id,),
            ).fetchone()
            if node is None:
                connection.execute("ROLLBACK")
                raise NodeNotFoundError("node not found")
            if node["status"] == "revoked" or node["credential_revoked_at"] is not None:
                connection.execute("ROLLBACK")
                raise NodeAuthenticationError("node access is revoked")
            if node["credential_node_id"] is None or node["status"] in {"installing", "failed"}:
                connection.execute("ROLLBACK")
                raise NodeUnavailableError("node is not ready for commands")
            if node["protocol_version"] != SUPPORTED_PROTOCOL_VERSION:
                connection.execute("ROLLBACK")
                raise UnsupportedProtocolError("unsupported node protocol version")
            connection.execute(
                """
                INSERT INTO node_commands(
                    id, node_id, command_type, payload_json, state, created_at
                ) VALUES (?, ?, ?, '{}', 'queued', ?)
                """,
                (command_id, node_id, command_type, now),
            )
            self._add_event(connection, node_id, "node.command_queued", command_type)
            connection.execute("COMMIT")
        return {
            "id": command_id,
            "node_id": node_id,
            "command_type": command_type,
            "state": "queued",
            "created_at": now,
        }

    def lease_next_command(self, node_token: str) -> dict[str, Any] | None:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        now = self._time()
        now_text = _timestamp(now)
        lease_until = _timestamp(now + timedelta(seconds=COMMAND_LEASE_SECONDS))
        with self.database.connect() as connection:
            candidate = connection.execute(
                """
                SELECT 1 FROM node_commands
                WHERE node_id = ? AND attempt_count <= ?
                  AND (
                    state = 'queued'
                    OR (state IN ('leased', 'acknowledged') AND lease_until <= ?)
                  )
                LIMIT 1
                """,
                (node_id, COMMAND_MAX_ATTEMPTS, now_text),
            ).fetchone()
        if candidate is None:
            return None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exhausted = connection.execute(
                """
                SELECT id FROM node_commands
                WHERE node_id = ? AND state IN ('leased', 'acknowledged')
                  AND lease_until <= ? AND attempt_count >= ?
                """,
                (node_id, now_text, COMMAND_MAX_ATTEMPTS),
            ).fetchall()
            connection.execute(
                """
                UPDATE node_commands
                SET state = 'failed', completed_at = ?, lease_until = NULL,
                    safe_result_json = ?
                WHERE node_id = ? AND state IN ('leased', 'acknowledged')
                  AND lease_until <= ? AND attempt_count >= ?
                """,
                (
                    now_text,
                    _json({"code": "delivery_attempts_exhausted", "status": "failed"}),
                    node_id,
                    now_text,
                    COMMAND_MAX_ATTEMPTS,
                ),
            )
            for _ in exhausted:
                self._add_event(connection, node_id, "node.command_failed", "delivery")
            row = connection.execute(
                """
                SELECT * FROM node_commands
                WHERE node_id = ? AND attempt_count < ?
                  AND (
                    state = 'queued'
                    OR (state IN ('leased', 'acknowledged') AND lease_until <= ?)
                  )
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (node_id, COMMAND_MAX_ATTEMPTS, now_text),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                UPDATE node_commands
                SET state = 'leased', lease_until = ?, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (lease_until, row["id"]),
            )
            connection.execute("COMMIT")
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):  # pragma: no cover - only corrupted storage
            payload = {}
        return {
            "id": str(row["id"]),
            "command_type": str(row["command_type"]),
            "payload": payload if isinstance(payload, dict) else {},
            "lease_seconds": COMMAND_LEASE_SECONDS,
            "attempt_count": int(row["attempt_count"]) + 1,
        }

    def acknowledge_command(self, node_token: str, command_id: str) -> str:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM node_commands WHERE id = ? AND node_id = ?",
                (command_id, node_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise CommandNotFoundError("command not found")
            state = str(row["state"])
            if state in {"acknowledged", "completed"}:
                connection.execute("COMMIT")
                return "acknowledged"
            if state != "leased":
                connection.execute("ROLLBACK")
                raise CommandStateError("command cannot be acknowledged")
            connection.execute(
                """
                UPDATE node_commands
                SET state = 'acknowledged', acknowledged_at = ?
                WHERE id = ?
                """,
                (now, command_id),
            )
            connection.execute("COMMIT")
        return "acknowledged"

    def complete_command(
        self,
        node_token: str,
        command_id: str,
        result: Mapping[str, Any],
    ) -> str:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT command_type, state, safe_result_json FROM node_commands
                WHERE id = ? AND node_id = ?
                """,
                (command_id, node_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise CommandNotFoundError("command not found")
            result_json = _json(_safe_command_result(str(row["command_type"]), result))
            state = str(row["state"])
            if state == "completed":
                connection.execute("COMMIT")
                if row["safe_result_json"] != result_json:
                    raise CommandStateError("command was completed with a different result")
                return "completed"
            if state not in {"leased", "acknowledged"}:
                connection.execute("ROLLBACK")
                raise CommandStateError("command cannot be completed")
            connection.execute(
                """
                UPDATE node_commands
                SET state = 'completed', completed_at = ?, lease_until = NULL,
                    safe_result_json = ?
                WHERE id = ?
                """,
                (now, result_json, command_id),
            )
            self._add_event(
                connection,
                node_id,
                "node.command_completed",
                str(result.get("status", "unknown")),
            )
            connection.execute("COMMIT")
        return "completed"

    def get_command(self, node_id: str, command_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_commands WHERE id = ? AND node_id = ?",
                (command_id, node_id),
            ).fetchone()
        if row is None:
            return None
        view = dict(row)
        for field in ("payload_json", "safe_result_json"):
            raw = view.pop(field)
            key = "payload" if field == "payload_json" else "safe_result"
            view[key] = json.loads(str(raw)) if raw is not None else None
        return view

    def reconcile_command_leases(self, *, node_id: str | None = None) -> dict[str, int]:
        """Expire abandoned commands and requeue bounded, still-deliverable leases."""

        now = self._time()
        now_text = _timestamp(now)
        cutoff = _timestamp(now - timedelta(seconds=COMMAND_MAX_AGE_SECONDS))
        scope = " AND node_id = ?" if node_id is not None else ""
        parameters = (node_id,) if node_id is not None else ()
        exhausted_result = _json({"code": "delivery_attempts_exhausted", "status": "failed"})
        expired_result = _json({"code": "command_expired", "status": "failed"})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exhausted = connection.execute(
                f"""
                SELECT id, node_id FROM node_commands
                WHERE state IN ('leased', 'acknowledged')
                  AND lease_until <= ? AND attempt_count >= ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (now_text, COMMAND_MAX_ATTEMPTS, *parameters),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE node_commands
                SET state = 'failed', completed_at = ?, lease_until = NULL,
                    safe_result_json = ?
                WHERE state IN ('leased', 'acknowledged')
                  AND lease_until <= ? AND attempt_count >= ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (
                    now_text,
                    exhausted_result,
                    now_text,
                    COMMAND_MAX_ATTEMPTS,
                    *parameters,
                ),
            )
            expired = connection.execute(
                f"""
                SELECT id, node_id FROM node_commands
                WHERE state IN ('queued', 'leased', 'acknowledged')
                  AND created_at <= ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (cutoff, *parameters),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE node_commands
                SET state = 'failed', completed_at = ?, lease_until = NULL,
                    safe_result_json = ?
                WHERE state IN ('queued', 'leased', 'acknowledged')
                  AND created_at <= ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (now_text, expired_result, cutoff, *parameters),
            )
            requeued = connection.execute(
                f"""
                UPDATE node_commands
                SET state = 'queued', lease_until = NULL, acknowledged_at = NULL
                WHERE state IN ('leased', 'acknowledged')
                  AND lease_until <= ? AND attempt_count < ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (now_text, COMMAND_MAX_ATTEMPTS, *parameters),
            ).rowcount
            for row in exhausted:
                self._add_event(
                    connection,
                    str(row["node_id"]),
                    "node.command_failed",
                    "delivery",
                )
            for row in expired:
                self._add_event(
                    connection,
                    str(row["node_id"]),
                    "node.command_failed",
                    "expired",
                )
            connection.execute("COMMIT")
        return {
            "failed": len(exhausted) + len(expired),
            "requeued": requeued,
        }

    def revoke_node(self, node_id: str) -> dict[str, Any]:
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            node = connection.execute(
                "SELECT status FROM restream_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if node is None:
                connection.execute("ROLLBACK")
                raise NodeNotFoundError("node not found")
            active_job = connection.execute(
                """
                SELECT 1 FROM node_install_jobs
                WHERE node_id = ? AND state NOT IN ('completed', 'cancelled', 'failed')
                LIMIT 1
                """,
                (node_id,),
            ).fetchone()
            if active_job is not None:
                connection.execute("ROLLBACK")
                raise NodeUnavailableError("node bootstrap must finish before revocation")
            if node["status"] not in {"ready", "degraded", "offline", "revoked"}:
                connection.execute("ROLLBACK")
                raise NodeUnavailableError("node bootstrap must finish before revocation")
            connection.execute(
                """
                UPDATE restream_nodes
                SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, node_id),
            )
            connection.execute(
                """
                UPDATE node_credentials SET revoked_at = COALESCE(revoked_at, ?)
                WHERE node_id = ?
                """,
                (now, node_id),
            )
            connection.execute(
                """
                UPDATE node_enrollment_tokens SET used_at = COALESCE(used_at, ?)
                WHERE node_id = ?
                """,
                (now, node_id),
            )
            connection.execute(
                """
                UPDATE node_commands
                SET state = 'cancelled', completed_at = ?, lease_until = NULL
                WHERE node_id = ? AND state IN ('queued', 'leased', 'acknowledged')
                """,
                (now, node_id),
            )
            if node["status"] != "revoked":
                self._add_event(connection, node_id, "node.revoked")
            connection.execute("COMMIT")
        revoked = self.get_node(node_id)
        if revoked is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("node disappeared after revoke")
        return revoked

    def list_events(self, node_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, node_id, event_type, safe_detail, created_at
                FROM node_events WHERE node_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (node_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_retention(self, *, terminal_command_days: int = 30) -> dict[str, int]:
        """Remove expired enrollment grants and old terminal command results."""

        if terminal_command_days < 1:
            raise ValueError("terminal_command_days must be positive")
        self.reconcile_command_leases()
        now = self._time()
        cutoff = _timestamp(now - timedelta(days=terminal_command_days))
        now_text = _timestamp(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tokens = connection.execute(
                "DELETE FROM node_enrollment_tokens WHERE expires_at <= ?", (now_text,)
            ).rowcount
            commands = connection.execute(
                """
                DELETE FROM node_commands
                WHERE state IN ('completed', 'failed', 'cancelled')
                  AND completed_at IS NOT NULL AND completed_at <= ?
                """,
                (cutoff,),
            ).rowcount
            connection.execute("COMMIT")
        return {"enrollment_tokens": tokens, "commands": commands}


__all__ = [
    "COMMAND_LEASE_SECONDS",
    "COMMAND_MAX_AGE_SECONDS",
    "COMMAND_MAX_ATTEMPTS",
    "COMMAND_POLL_INTERVAL_SECONDS",
    "ENROLLMENT_TTL_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "CommandNotFoundError",
    "CommandStateError",
    "EnrollmentGrant",
    "EnrollmentTokenError",
    "HeartbeatRateLimitError",
    "NodeAuthenticationError",
    "NodeNotFoundError",
    "NodeService",
    "NodeUnavailableError",
    "UnsupportedProtocolError",
]
