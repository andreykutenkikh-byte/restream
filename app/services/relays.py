"""Secure command broker for native Moblin relay nodes.

The native relay connects out to the control plane.  Command payloads are
encrypted before they enter SQLite and are decrypted only while constructing
an authenticated HTTPS lease response.  Secret command results use the same
rule and are destroyed atomically on their first administrative read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast
from uuid import UUID, uuid4

from app.core.security import (
    decrypt_destination_key,
    digest_opaque_token,
    encrypt_destination_key,
    generate_node_token,
    verify_opaque_token_digest,
)
from app.db import Database
from app.schemas import RelaySafeState

RELAY_PROTOCOL_VERSION: Final = 1
RELAY_HEARTBEAT_INTERVAL_SECONDS: Final = 5
RELAY_COMMAND_POLL_INTERVAL_SECONDS: Final = 5
RELAY_HEARTBEAT_MIN_INTERVAL_SECONDS: Final = 1
RELAY_COMMAND_LEASE_SECONDS: Final = 120
RELAY_COMMAND_MAX_ATTEMPTS: Final = 3
RELAY_COMMAND_TTL_SECONDS: Final = 600

RelayCommandType = Literal[
    "STATUS",
    "START",
    "STOP",
    "CONFIGURE_YOUTUBE",
    "REVEAL_MOBLIN_URL",
    "CLEAR_YOUTUBE",
]
RelayCompletionStatus = Literal["ok", "failed", "conflict"]

_COMMAND_TYPES: Final = {
    "STATUS",
    "START",
    "STOP",
    "CONFIGURE_YOUTUBE",
    "REVEAL_MOBLIN_URL",
    "CLEAR_YOUTUBE",
}
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class RelayDomainError(RuntimeError):
    """Base class for failures that API handlers may render safely."""


class RelayNotFoundError(RelayDomainError):
    pass


class RelayAuthenticationError(RelayDomainError):
    pass


class RelayUnavailableError(RelayDomainError):
    pass


class RelayUnsupportedProtocolError(RelayDomainError):
    pass


class RelayHeartbeatRateLimitError(RelayDomainError):
    pass


class RelayCommandNotFoundError(RelayDomainError):
    pass


class RelayCommandStateError(RelayDomainError):
    pass


class RelayCommandPendingError(RelayDomainError):
    pass


class RelayIdempotencyConflictError(RelayDomainError):
    pass


class RelayActiveError(RelayDomainError):
    pass


class RelayNotConfiguredError(RelayDomainError):
    pass


class RelaySecretUnavailableError(RelayDomainError):
    pass


class RelayProvisionConflictError(RelayDomainError):
    pass


@dataclass(frozen=True, slots=True)
class RelayProvisionGrant:
    node_id: str
    node_token: str = field(repr=False)


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


class RelayService:
    """Atomic relay provisioning, presence, command, and secret transitions."""

    def __init__(
        self,
        database: Database,
        master_encryption_key: str,
        *,
        clock: Clock = _now,
    ) -> None:
        self.database = database
        self.master_encryption_key = master_encryption_key
        self.clock = clock

    def _time(self) -> datetime:
        return _as_utc(self.clock())

    def _encrypt_mapping(self, value: Mapping[str, Any]) -> str:
        return encrypt_destination_key(_json(value), self.master_encryption_key)

    def encrypted_empty_payload(self) -> str:
        """Return a fresh encrypted tombstone for an erased relay payload."""

        return self._encrypt_mapping({})

    def _request_fingerprint(
        self,
        command_type: RelayCommandType,
        payload: Mapping[str, Any],
    ) -> str:
        canonical = _json(
            {
                "command_type": command_type,
                "payload": dict(payload),
            }
        ).encode("utf-8")
        return hmac.new(
            self.master_encryption_key.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()

    def _add_audit_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        detail: str,
        created_at: str,
    ) -> None:
        """Insert and bound a safe relay audit event in the caller's transaction."""

        connection.execute(
            "INSERT INTO audit_events(event_type, detail, created_at) VALUES (?, ?, ?)",
            (event_type, detail, created_at),
        )
        connection.execute(
            """
            DELETE FROM audit_events
            WHERE id NOT IN (
                SELECT id FROM audit_events ORDER BY id DESC LIMIT ?
            )
            """,
            (self.database.audit_limit,),
        )

    def _decrypt_mapping(self, ciphertext: str) -> dict[str, Any]:
        value = json.loads(decrypt_destination_key(ciphertext, self.master_encryption_key))
        if not isinstance(value, dict):
            raise RelayCommandStateError("encrypted command payload is invalid")
        return cast(dict[str, Any], value)

    def provision_node(
        self,
        *,
        display_name: str,
        address: str,
        rotate_existing: bool = False,
    ) -> RelayProvisionGrant:
        """Create one UI-visible relay identity and rotate its one raw token."""

        clean_name = display_name.strip()
        clean_address = address.strip()
        if not 1 <= len(clean_name) <= 80 or any(ord(char) < 32 for char in clean_name):
            raise ValueError("display_name is invalid")
        if not 1 <= len(clean_address) <= 253 or any(
            char.isspace() or ord(char) < 32 for char in clean_address
        ):
            raise ValueError("address is invalid")

        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, status, capabilities_json, current_command_id FROM restream_nodes
                WHERE address = ? AND ssh_port = 22
                """,
                (clean_address,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_capabilities = json.loads(str(existing["capabilities_json"]))
                except (TypeError, ValueError):
                    existing_capabilities = []
                if "moblin_relay" not in existing_capabilities:
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "address belongs to a non-relay node; refusing credential rotation"
                    )
                if not rotate_existing:
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "relay already exists; pass --rotate-existing to rotate its credential"
                    )
                relay_state = connection.execute(
                    """
                    SELECT service_state, main_process, current_command_id, last_seen_at
                    FROM relay_nodes WHERE node_id = ?
                    """,
                    (existing["id"],),
                ).fetchone()
                if relay_state is None:
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "relay state is unavailable; refusing credential rotation"
                    )
                if existing["status"] != "revoked" and (
                    relay_state["service_state"] != "inactive"
                    or relay_state["main_process"] != "stopped"
                    or relay_state["last_seen_at"] is None
                    or (
                        self._time() - _parse_timestamp(str(relay_state["last_seen_at"]))
                    ).total_seconds()
                    > 30
                ):
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "existing relay must report a fresh stopped state "
                        "before credential rotation"
                    )
                if (
                    existing["current_command_id"] is not None
                    or relay_state["current_command_id"] is not None
                ):
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "relay command is active; refusing credential rotation"
                    )
                pending_command = connection.execute(
                    """
                    SELECT 1 FROM relay_commands
                    WHERE node_id = ? AND state IN ('queued', 'leased', 'acknowledged')
                    LIMIT 1
                    """,
                    (existing["id"],),
                ).fetchone()
                if pending_command is not None:
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "relay command is pending; refusing credential rotation"
                    )
                active_bootstrap = connection.execute(
                    """
                    SELECT 1 FROM node_install_jobs
                    WHERE node_id = ?
                      AND state NOT IN ('completed', 'succeeded', 'failed', 'cancelled')
                    LIMIT 1
                    """,
                    (existing["id"],),
                ).fetchone()
                if active_bootstrap is not None:
                    connection.execute("ROLLBACK")
                    raise RelayProvisionConflictError(
                        "node bootstrap is active; refusing credential rotation"
                    )
                connection.execute(
                    """
                    UPDATE relay_commands
                    SET secret_result_encrypted = NULL,
                        secret_consumed_at = COALESCE(secret_consumed_at, ?)
                    WHERE node_id = ? AND secret_result_encrypted IS NOT NULL
                    """,
                    (now, existing["id"]),
                )
            node_id = str(existing["id"]) if existing else str(uuid4())
            node_token = generate_node_token(node_id)
            token_digest = digest_opaque_token(node_token)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO restream_nodes(
                        id, display_name, address, resolved_ip, ssh_port, ssh_username,
                        status, protocol_version, capabilities_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 22, 'relay', 'connecting', ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        clean_name,
                        clean_address,
                        clean_address,
                        RELAY_PROTOCOL_VERSION,
                        _json(["moblin_relay"]),
                        now,
                        now,
                    ),
                )
            else:
                safe_capabilities = [
                    item
                    for item in existing_capabilities
                    if isinstance(item, str) and item != "moblin_relay"
                ]
                safe_capabilities.append("moblin_relay")
                connection.execute(
                    """
                    UPDATE restream_nodes
                    SET display_name = ?, status = 'connecting', protocol_version = ?,
                        capabilities_json = ?, current_command_id = NULL,
                        last_seen_at = NULL, updated_at = ?, revoked_at = NULL
                    WHERE id = ?
                    """,
                    (
                        clean_name,
                        RELAY_PROTOCOL_VERSION,
                        _json(safe_capabilities),
                        now,
                        node_id,
                    ),
                )
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
                (node_id, token_digest, now),
            )
            connection.execute(
                """
                INSERT INTO relay_nodes(node_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    service_state = 'unknown', service_enabled = 0,
                    main_process = 'unknown', srt_listener = 'unknown', source = 'UNKNOWN',
                    youtube_forward = 'unknown', overall = 'unknown',
                    youtube_url_configured = 0, youtube_key_configured = 0,
                    healthy = 0, portrait_profile = 0, last_error_code = NULL,
                    current_command_id = NULL, last_seen_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (node_id, now, now),
            )
            self._add_audit_event(
                connection,
                "relay.provisioned",
                f"node_id={node_id}",
                now,
            )
            connection.execute("COMMIT")
        return RelayProvisionGrant(node_id=node_id, node_token=node_token)

    @staticmethod
    def _node_id_from_token(node_token: str | None) -> str:
        node_id = ""
        if isinstance(node_token, str):
            parts = node_token.split("_", 2)
            if len(parts) == 3 and parts[0] == "node" and parts[1] and parts[2]:
                try:
                    node_id = str(UUID(parts[1]))
                except ValueError:
                    return ""
        return node_id

    def _authenticate_connection(
        self,
        connection: sqlite3.Connection,
        node_token: str | None,
        *,
        require_supported_protocol: bool = False,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT credential.node_id, credential.token_digest, credential.revoked_at,
                   node.status, node.protocol_version, node.capabilities_json,
                   relay.last_seen_at AS relay_last_seen_at
            FROM node_credentials AS credential
            JOIN restream_nodes AS node ON node.id = credential.node_id
            JOIN relay_nodes AS relay ON relay.node_id = node.id
            WHERE credential.node_id = ?
            """,
            (self._node_id_from_token(node_token),),
        ).fetchone()
        expected_digest = str(row["token_digest"]) if row else "0" * 64
        if (
            row is None
            or not verify_opaque_token_digest(node_token, expected_digest)
            or row["revoked_at"] is not None
            or row["status"] == "revoked"
        ):
            raise RelayAuthenticationError("relay authentication failed")
        try:
            capabilities = json.loads(str(row["capabilities_json"]))
        except (TypeError, ValueError):
            capabilities = []
        if "moblin_relay" not in capabilities:
            raise RelayAuthenticationError("relay authentication failed")
        if require_supported_protocol and row["protocol_version"] != RELAY_PROTOCOL_VERSION:
            raise RelayUnsupportedProtocolError("unsupported relay protocol version")
        return cast(sqlite3.Row, row)

    def authenticate(
        self,
        node_token: str | None,
        *,
        require_supported_protocol: bool = False,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = self._authenticate_connection(
                connection,
                node_token,
                require_supported_protocol=require_supported_protocol,
            )
        return dict(row)

    def record_heartbeat(self, node_token: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        relay = cast(Mapping[str, Any], snapshot["relay"])
        host = cast(Mapping[str, Any], snapshot["host"])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                authenticated = self._authenticate_connection(connection, node_token)
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            if snapshot.get("protocol_version") != RELAY_PROTOCOL_VERSION:
                connection.execute("ROLLBACK")
                raise RelayUnsupportedProtocolError("unsupported relay protocol version")
            node_id = str(authenticated["node_id"])
            now = self._time()
            now_text = _timestamp(now)
            if authenticated["relay_last_seen_at"] is not None:
                previous = _parse_timestamp(str(authenticated["relay_last_seen_at"]))
                if (now - previous).total_seconds() < RELAY_HEARTBEAT_MIN_INTERVAL_SECONDS:
                    connection.execute("ROLLBACK")
                    raise RelayHeartbeatRateLimitError("relay heartbeat rate limit exceeded")
            connection.execute(
                """
                UPDATE relay_nodes
                SET service_state = ?, service_enabled = ?, main_process = ?,
                    srt_listener = ?, source = ?, youtube_forward = ?, overall = ?,
                    youtube_url_configured = ?, youtube_key_configured = ?,
                    healthy = ?, portrait_profile = ?, last_error_code = ?, current_command_id = ?,
                    last_seen_at = ?, updated_at = ?
                WHERE node_id = ?
                """,
                (
                    relay["service_state"],
                    int(bool(relay["enabled"])),
                    relay["main_process"],
                    relay["srt_listener"],
                    relay["source"],
                    relay["youtube_forward"],
                    relay["overall"],
                    int(bool(relay["youtube_url_configured"])),
                    int(bool(relay["youtube_key_configured"])),
                    int(bool(relay["healthy"])),
                    int(bool(relay["portrait_profile"])),
                    relay.get("error_code"),
                    snapshot.get("current_command_id"),
                    now_text,
                    now_text,
                    node_id,
                ),
            )
            connection.execute(
                """
                UPDATE restream_nodes
                SET status = 'ready', hostname = ?, agent_version = ?, protocol_version = ?,
                    uptime_seconds = ?, load_1m = ?, cpu_percent = ?,
                    memory_total_bytes = ?, memory_available_bytes = ?,
                    disk_total_bytes = ?, disk_free_bytes = ?, capabilities_json = ?,
                    current_command_id = ?, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    snapshot["hostname"],
                    snapshot["agent_version"],
                    snapshot["protocol_version"],
                    host["uptime_seconds"],
                    host["load_1m"],
                    host["cpu_percent"],
                    host["memory_total_bytes"],
                    host["memory_available_bytes"],
                    host["disk_total_bytes"],
                    host["disk_free_bytes"],
                    _json(["moblin_relay"]),
                    snapshot.get("current_command_id"),
                    now_text,
                    now_text,
                    node_id,
                ),
            )
            connection.execute("COMMIT")
        return {
            "node_id": node_id,
            "heartbeat_interval_seconds": RELAY_HEARTBEAT_INTERVAL_SECONDS,
            "command_poll_interval_seconds": RELAY_COMMAND_POLL_INTERVAL_SECONDS,
        }

    def _status_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        last_seen = str(row["last_seen_at"]) if row["last_seen_at"] else None
        generic_status = str(row["node_status"])
        if generic_status != "revoked":
            if last_seen is None:
                generic_status = "offline"
            else:
                age = (self._time() - _parse_timestamp(last_seen)).total_seconds()
                generic_status = "offline" if age > 30 else "degraded" if age > 15 else "ready"
        service = str(row["service_state"])
        overall = "offline" if generic_status == "offline" else str(row["overall"])
        return {
            # Availability is transport presence, not the relay service's
            # active/inactive state. A freshly-heartbeating stopped relay is
            # therefore operable even when its broker reports overall=offline.
            "available": generic_status in {"ready", "degraded"},
            "status": {
                "service": service,
                "enabled": bool(row["service_enabled"]),
                "main_process": str(row["main_process"]),
                "srt_listener": str(row["srt_listener"]),
                "source": str(row["source"]),
                "youtube_forward": str(row["youtube_forward"]),
                "overall": overall,
                "youtube_url_configured": bool(row["youtube_url_configured"]),
                "youtube_key_configured": bool(row["youtube_key_configured"]),
                "portrait_profile": bool(row["portrait_profile"]),
                "error_code": row["last_error_code"],
            },
            "last_seen_at": last_seen,
        }

    def get_status(self, node_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT relay.*, node.status AS node_status
                FROM relay_nodes AS relay
                JOIN restream_nodes AS node ON node.id = relay.node_id
                WHERE relay.node_id = ?
                """,
                (node_id,),
            ).fetchone()
        if row is None:
            raise RelayNotFoundError("relay node not found")
        return self._status_from_row(row)

    def list_nodes(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT relay.*, node.status AS node_status, node.display_name, node.address
                FROM relay_nodes AS relay
                JOIN restream_nodes AS node ON node.id = relay.node_id
                ORDER BY node.created_at, node.id
                """
            ).fetchall()
        return [
            {
                "node_id": str(row["node_id"]),
                "display_name": str(row["display_name"]),
                "address": str(row["address"]),
                **self._status_from_row(row),
            }
            for row in rows
        ]

    def _check_node_for_command(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        *,
        require_fresh: bool = True,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT relay.*, node.status AS node_status, node.protocol_version,
                   credential.revoked_at AS credential_revoked_at
            FROM relay_nodes AS relay
            JOIN restream_nodes AS node ON node.id = relay.node_id
            LEFT JOIN node_credentials AS credential ON credential.node_id = node.id
            WHERE relay.node_id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            raise RelayNotFoundError("relay node not found")
        if row["node_status"] == "revoked" or row["credential_revoked_at"] is not None:
            raise RelayAuthenticationError("relay access is revoked")
        if row["protocol_version"] != RELAY_PROTOCOL_VERSION:
            raise RelayUnsupportedProtocolError("unsupported relay protocol version")
        if not require_fresh:
            return cast(sqlite3.Row, row)
        if row["last_seen_at"] is None:
            raise RelayUnavailableError("relay node is not ready")
        if (self._time() - _parse_timestamp(str(row["last_seen_at"]))).total_seconds() > 30:
            raise RelayUnavailableError("relay node is offline")
        return cast(sqlite3.Row, row)

    def create_command(
        self,
        node_id: str,
        command_type: RelayCommandType,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if command_type not in _COMMAND_TYPES:
            raise ValueError("unsupported relay command type")
        safe_payload = dict(payload or {})
        if command_type == "CONFIGURE_YOUTUBE":
            if set(safe_payload) != {"youtube_rtmps_url", "youtube_stream_key"} or any(
                not isinstance(value, str) or not value for value in safe_payload.values()
            ):
                raise ValueError("configure payload is invalid")
        elif safe_payload:
            raise ValueError("command does not accept a payload")
        if idempotency_key is None:
            idempotency_key = f"server:{uuid4()}"
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency key is invalid")
        request_fingerprint = self._request_fingerprint(command_type, safe_payload)

        now = self._time()
        now_text = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=RELAY_COMMAND_TTL_SECONDS))
        command_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                relay = self._check_node_for_command(
                    connection,
                    node_id,
                    require_fresh=False,
                )
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            existing = connection.execute(
                "SELECT * FROM relay_commands WHERE node_id = ? AND idempotency_key = ?",
                (node_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_fingerprint = str(existing["request_fingerprint"] or "")
                if not hmac.compare_digest(existing_fingerprint, request_fingerprint):
                    connection.execute("ROLLBACK")
                    raise RelayIdempotencyConflictError(
                        "idempotency key belongs to a different relay request"
                    )
                connection.execute("COMMIT")
                return self._command_view(existing)
            try:
                relay = self._check_node_for_command(connection, node_id)
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            if command_type in {"CONFIGURE_YOUTUBE", "CLEAR_YOUTUBE"} and (
                relay["service_state"] != "inactive" or relay["main_process"] != "stopped"
            ):
                connection.execute("ROLLBACK")
                raise RelayActiveError("relay must be stopped before configuration")
            if command_type == "START" and not (
                relay["youtube_url_configured"] and relay["youtube_key_configured"]
            ):
                connection.execute("ROLLBACK")
                raise RelayNotConfiguredError("YouTube is not configured")
            if command_type != "STATUS":
                pending = connection.execute(
                    """
                    SELECT * FROM relay_commands
                    WHERE node_id = ? AND command_type != 'STATUS'
                      AND state IN ('queued', 'leased', 'acknowledged')
                      AND expires_at > ?
                    ORDER BY created_at, id LIMIT 1
                    """,
                    (node_id, now_text),
                ).fetchone()
                if pending is not None:
                    if (
                        command_type == "REVEAL_MOBLIN_URL"
                        and pending["command_type"] == "REVEAL_MOBLIN_URL"
                    ):
                        connection.execute("COMMIT")
                        return self._command_view(pending)
                    connection.execute("ROLLBACK")
                    raise RelayCommandPendingError("a relay mutation command is pending")
            if command_type == "REVEAL_MOBLIN_URL":
                completed = connection.execute(
                    """
                    SELECT * FROM relay_commands
                    WHERE node_id = ? AND command_type = 'REVEAL_MOBLIN_URL'
                      AND state = 'completed' AND completion_status = 'ok'
                      AND secret_result_encrypted IS NOT NULL
                      AND secret_consumed_at IS NULL
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (node_id,),
                ).fetchone()
                if completed is not None:
                    connection.execute("COMMIT")
                    return self._command_view(completed)
            connection.execute(
                """
                INSERT INTO relay_commands(
                    id, node_id, command_type, payload_encrypted, state, expires_at,
                    idempotency_key, request_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    command_id,
                    node_id,
                    command_type,
                    self._encrypt_mapping(safe_payload),
                    expires_at,
                    idempotency_key,
                    request_fingerprint,
                    now_text,
                ),
            )
            created = connection.execute(
                "SELECT * FROM relay_commands WHERE id = ?", (command_id,)
            ).fetchone()
            self._add_audit_event(
                connection,
                "relay.command_queued",
                f"node_id={node_id} action={command_type} id={command_id}",
                now_text,
            )
            connection.execute("COMMIT")
        if created is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("relay command was not persisted")
        return self._command_view(created)

    def lease_next_command(self, node_token: str) -> dict[str, Any] | None:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        self.reconcile_command_leases(node_id=node_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._authenticate_connection(
                    connection,
                    node_token,
                    require_supported_protocol=True,
                )
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            now = self._time()
            now_text = _timestamp(now)
            lease_until = _timestamp(now + timedelta(seconds=RELAY_COMMAND_LEASE_SECONDS))
            expired_safe = _json({"error_code": "command_expired"})
            erased_payload = self._encrypt_mapping({})
            connection.execute(
                """
                UPDATE relay_commands
                SET state = 'failed', lease_until = NULL, completed_at = ?,
                    completion_status = 'failed', safe_result_json = ?, payload_encrypted = ?
                WHERE node_id = ? AND state = 'queued' AND expires_at < ?
                """,
                (now_text, expired_safe, erased_payload, node_id, lease_until),
            )
            row = connection.execute(
                """
                SELECT * FROM relay_commands
                WHERE node_id = ? AND state = 'queued' AND expires_at >= ?
                  AND attempt_count < ?
                ORDER BY created_at, id LIMIT 1
                """,
                (node_id, lease_until, RELAY_COMMAND_MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                UPDATE relay_commands
                SET state = 'leased', lease_until = ?, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (lease_until, row["id"]),
            )
            connection.execute("COMMIT")
        return {
            "id": str(row["id"]),
            "action": str(row["command_type"]),
            "payload": self._decrypt_mapping(str(row["payload_encrypted"])),
            "lease_seconds": RELAY_COMMAND_LEASE_SECONDS,
            "attempt_count": int(row["attempt_count"]) + 1,
            "expires_at": str(row["expires_at"]),
        }

    def acknowledge_command(self, node_token: str, command_id: str) -> str:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._authenticate_connection(
                    connection,
                    node_token,
                    require_supported_protocol=True,
                )
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            now = _timestamp(self._time())
            row = connection.execute(
                "SELECT state, lease_until FROM relay_commands WHERE id = ? AND node_id = ?",
                (command_id, node_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RelayCommandNotFoundError("relay command not found")
            state = str(row["state"])
            if state in {"acknowledged", "completed"}:
                connection.execute("COMMIT")
                return "acknowledged"
            if state != "leased" or str(row["lease_until"]) <= now:
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("relay command cannot be acknowledged")
            connection.execute(
                """
                UPDATE relay_commands SET state = 'acknowledged', acknowledged_at = ?
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
        *,
        status: RelayCompletionStatus,
        completed_at: str,
        safe_result: Mapping[str, Any],
        secret_result: str | None,
    ) -> str:
        authenticated = self.authenticate(node_token, require_supported_protocol=True)
        node_id = str(authenticated["node_id"])
        normalized_safe = RelaySafeState.model_validate(safe_result).model_dump(mode="json")
        safe_result_json = _json(normalized_safe)
        erased_payload = self._encrypt_mapping({})
        # Parse the agent timestamp as protocol input, but use server time for
        # retention so a compromised clock cannot prolong secret storage.
        _parse_timestamp(completed_at)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._authenticate_connection(
                    connection,
                    node_token,
                    require_supported_protocol=True,
                )
            except RelayDomainError:
                connection.execute("ROLLBACK")
                raise
            now_text = _timestamp(self._time())
            row = connection.execute(
                """
                SELECT command_type, state, lease_until, completion_status, safe_result_json
                FROM relay_commands WHERE id = ? AND node_id = ?
                """,
                (command_id, node_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RelayCommandNotFoundError("relay command not found")
            command_type = str(row["command_type"])
            if command_type == "REVEAL_MOBLIN_URL" and status == "ok":
                if secret_result is None or not secret_result.strip():
                    connection.execute("ROLLBACK")
                    raise RelayCommandStateError("successful reveal result requires a secret")
                encrypted_secret = encrypt_destination_key(
                    secret_result.strip(), self.master_encryption_key
                )
            else:
                if secret_result is not None:
                    connection.execute("ROLLBACK")
                    raise RelayCommandStateError("command must not return a secret")
                encrypted_secret = None
            if (
                command_type == "CONFIGURE_YOUTUBE"
                and status == "conflict"
                and normalized_safe.get("error_code") != "relay_active"
            ):
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("configuration conflict result is invalid")
            if (
                command_type == "CONFIGURE_YOUTUBE"
                and status == "ok"
                and not (
                    normalized_safe["youtube_url_configured"]
                    and normalized_safe["youtube_key_configured"]
                )
            ):
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("configuration success result is invalid")
            if (
                command_type == "CLEAR_YOUTUBE"
                and status == "ok"
                and (
                    normalized_safe["youtube_url_configured"]
                    or normalized_safe["youtube_key_configured"]
                )
            ):
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("clear success result is invalid")
            state = str(row["state"])
            if state == "completed":
                connection.execute("COMMIT")
                if (
                    row["completion_status"] != status
                    or row["safe_result_json"] != safe_result_json
                ):
                    raise RelayCommandStateError("relay command has a different result")
                return "completed"
            if state not in {"leased", "acknowledged"}:
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("relay command cannot be completed")
            if row["lease_until"] is None or str(row["lease_until"]) <= now_text:
                connection.execute("ROLLBACK")
                raise RelayCommandStateError("relay command lease has expired")
            connection.execute(
                """
                UPDATE relay_commands
                SET state = 'completed', lease_until = NULL, completed_at = ?,
                    completion_status = ?, safe_result_json = ?, secret_result_encrypted = ?,
                    payload_encrypted = ?
                WHERE id = ?
                """,
                (
                    now_text,
                    status,
                    safe_result_json,
                    encrypted_secret,
                    erased_payload,
                    command_id,
                ),
            )
            connection.execute(
                """
                UPDATE relay_nodes
                SET service_state = ?, service_enabled = ?, main_process = ?,
                    srt_listener = ?, source = ?, youtube_forward = ?, overall = ?,
                    youtube_url_configured = ?, youtube_key_configured = ?, healthy = ?,
                    portrait_profile = ?, last_error_code = ?, current_command_id = NULL,
                    last_seen_at = ?, updated_at = ?
                WHERE node_id = ?
                """,
                (
                    normalized_safe["service_state"],
                    int(normalized_safe["enabled"]),
                    normalized_safe["main_process"],
                    normalized_safe["srt_listener"],
                    normalized_safe["source"],
                    normalized_safe["youtube_forward"],
                    normalized_safe["overall"],
                    int(normalized_safe["youtube_url_configured"]),
                    int(normalized_safe["youtube_key_configured"]),
                    int(normalized_safe["healthy"]),
                    int(normalized_safe["portrait_profile"]),
                    normalized_safe["error_code"],
                    now_text,
                    now_text,
                    node_id,
                ),
            )
            connection.execute(
                """
                UPDATE restream_nodes SET status = 'ready', current_command_id = NULL,
                    last_seen_at = ?, updated_at = ? WHERE id = ?
                """,
                (now_text, now_text, node_id),
            )
            self._add_audit_event(
                connection,
                "relay.command_completed",
                f"node_id={node_id} action={command_type} status={status}",
                now_text,
            )
            connection.execute("COMMIT")
        return "completed"

    def _command_view(self, row: sqlite3.Row) -> dict[str, Any]:
        safe_result = None
        if row["safe_result_json"] is not None:
            try:
                candidate = json.loads(str(row["safe_result_json"]))
            except (TypeError, ValueError):
                candidate = None
            if isinstance(candidate, dict):
                safe_result = candidate
        return {
            "id": str(row["id"]),
            "node_id": str(row["node_id"]),
            "command_type": str(row["command_type"]),
            "state": str(row["state"]),
            "attempt_count": int(row["attempt_count"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "acknowledged_at": row["acknowledged_at"],
            "completed_at": row["completed_at"],
            "completion_status": row["completion_status"],
            "safe_result": safe_result,
            "secret_available": bool(
                row["secret_result_encrypted"] is not None and row["secret_consumed_at"] is None
            ),
        }

    def get_command(self, node_id: str, command_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM relay_commands WHERE id = ? AND node_id = ?",
                (command_id, node_id),
            ).fetchone()
        return self._command_view(row) if row else None

    def consume_secret_result(self, node_id: str, command_id: str) -> str:
        now = _timestamp(self._time())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT command_type, state, completion_status, secret_result_encrypted,
                       secret_consumed_at
                FROM relay_commands WHERE id = ? AND node_id = ?
                """,
                (command_id, node_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RelayCommandNotFoundError("relay command not found")
            ciphertext = row["secret_result_encrypted"]
            if (
                row["command_type"] != "REVEAL_MOBLIN_URL"
                or row["state"] != "completed"
                or row["completion_status"] != "ok"
                or ciphertext is None
                or row["secret_consumed_at"] is not None
            ):
                connection.execute("ROLLBACK")
                raise RelaySecretUnavailableError("relay secret result is unavailable")
            secret = decrypt_destination_key(str(ciphertext), self.master_encryption_key)
            consumed = connection.execute(
                """
                UPDATE relay_commands
                SET secret_result_encrypted = NULL, secret_consumed_at = ?
                WHERE id = ? AND node_id = ? AND secret_consumed_at IS NULL
                """,
                (now, command_id, node_id),
            )
            if consumed.rowcount != 1:  # pragma: no cover - write-lock invariant
                connection.execute("ROLLBACK")
                raise RelaySecretUnavailableError("relay secret result is unavailable")
            self._add_audit_event(
                connection,
                "relay.secret_consumed",
                f"node_id={node_id} action=REVEAL_MOBLIN_URL",
                now,
            )
            connection.execute("COMMIT")
        return secret

    def reconcile_command_leases(self, *, node_id: str | None = None) -> dict[str, int]:
        current_time = self._time()
        now = _timestamp(current_time)
        full_lease_until = _timestamp(current_time + timedelta(seconds=RELAY_COMMAND_LEASE_SECONDS))
        scope = " AND node_id = ?" if node_id is not None else ""
        parameters = (node_id,) if node_id is not None else ()
        expired_safe = _json({"error_code": "command_expired"})
        erased_payload = self._encrypt_mapping({})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                f"""
                UPDATE relay_commands
                SET state = 'failed', lease_until = NULL, completed_at = ?,
                    completion_status = 'failed', safe_result_json = ?, payload_encrypted = ?
                WHERE state IN ('queued', 'leased', 'acknowledged')
                  AND (
                    (
                      expires_at < ?
                      AND (
                        state = 'queued' OR lease_until IS NULL OR lease_until <= ?
                      )
                    )
                    OR (
                      attempt_count >= ?
                      AND (
                        state = 'queued'
                        OR (state IN ('leased', 'acknowledged') AND lease_until <= ?)
                      )
                    )
                  ){scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (
                    now,
                    expired_safe,
                    erased_payload,
                    full_lease_until,
                    now,
                    RELAY_COMMAND_MAX_ATTEMPTS,
                    now,
                    *parameters,
                ),
            ).rowcount
            requeued = connection.execute(
                f"""
                UPDATE relay_commands
                SET state = 'queued', lease_until = NULL, acknowledged_at = NULL
                WHERE state IN ('leased', 'acknowledged') AND lease_until <= ?
                  AND expires_at >= ? AND attempt_count < ?{scope}
                """,  # noqa: S608 - scope is a fixed optional predicate
                (now, full_lease_until, RELAY_COMMAND_MAX_ATTEMPTS, *parameters),
            ).rowcount
            connection.execute("COMMIT")
        return {"failed": expired, "requeued": requeued}

    def prune_retention(self, *, terminal_command_days: int = 30) -> dict[str, int]:
        self.reconcile_command_leases()
        now = self._time()
        terminal_cutoff = _timestamp(now - timedelta(days=terminal_command_days))
        stale_secret_cutoff = _timestamp(now - timedelta(minutes=10))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            secrets_removed = connection.execute(
                """
                UPDATE relay_commands SET secret_result_encrypted = NULL
                WHERE secret_result_encrypted IS NOT NULL AND completed_at <= ?
                """,
                (stale_secret_cutoff,),
            ).rowcount
            commands_removed = connection.execute(
                """
                DELETE FROM relay_commands
                WHERE state IN ('completed', 'failed', 'cancelled') AND completed_at <= ?
                """,
                (terminal_cutoff,),
            ).rowcount
            connection.execute("COMMIT")
        return {"commands": commands_removed, "secrets": secrets_removed}


__all__ = [
    "RELAY_COMMAND_LEASE_SECONDS",
    "RELAY_COMMAND_MAX_ATTEMPTS",
    "RELAY_COMMAND_TTL_SECONDS",
    "RelayActiveError",
    "RelayAuthenticationError",
    "RelayCommandNotFoundError",
    "RelayCommandPendingError",
    "RelayCommandStateError",
    "RelayHeartbeatRateLimitError",
    "RelayIdempotencyConflictError",
    "RelayNotConfiguredError",
    "RelayNotFoundError",
    "RelayProvisionConflictError",
    "RelayProvisionGrant",
    "RelaySecretUnavailableError",
    "RelayService",
    "RelayUnavailableError",
    "RelayUnsupportedProtocolError",
]
