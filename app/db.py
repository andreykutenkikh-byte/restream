"""Small SQLite repository with idempotent schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Owns only the SQLite database for this Compose project."""

    def __init__(self, path: Path, *, audit_limit: int = 1000) -> None:
        self.path = path
        self.audit_limit = audit_limit

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    stream_key_encrypted TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
                    ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS destinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    stream_key_encrypted TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    state TEXT NOT NULL DEFAULT 'stopped',
                    last_error TEXT,
                    started_at TEXT,
                    worker_pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_destinations_enabled
                    ON destinations(enabled);
                CREATE INDEX IF NOT EXISTS idx_destinations_state
                    ON destinations(state);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
                    ON audit_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS restream_nodes (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    resolved_ip TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL CHECK (ssh_port BETWEEN 1 AND 65535),
                    ssh_username TEXT NOT NULL,
                    host_key_algorithm TEXT,
                    host_key_fingerprint TEXT,
                    host_key_trust_mode TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'installing', 'connecting', 'ready', 'degraded',
                            'offline', 'revoked', 'failed'
                        )
                    ),
                    public_ip TEXT,
                    hostname TEXT,
                    os_name TEXT,
                    os_version TEXT,
                    architecture TEXT,
                    cpu_count INTEGER,
                    uptime_seconds REAL,
                    load_1m REAL,
                    cpu_percent REAL,
                    memory_total_bytes INTEGER,
                    memory_available_bytes INTEGER,
                    disk_total_bytes INTEGER,
                    disk_free_bytes INTEGER,
                    ffmpeg_version TEXT,
                    ffprobe_version TEXT,
                    agent_version TEXT,
                    protocol_version INTEGER,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    current_command_id TEXT,
                    control_latency_ms REAL CHECK (
                        control_latency_ms IS NULL OR
                        control_latency_ms BETWEEN 0 AND 60000
                    ),
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_restream_nodes_status
                    ON restream_nodes(status);
                CREATE INDEX IF NOT EXISTS idx_restream_nodes_last_seen
                    ON restream_nodes(last_seen_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_restream_nodes_target
                    ON restream_nodes(address, ssh_port);
                CREATE TABLE IF NOT EXISTS node_install_jobs (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL DEFAULT 0
                        CHECK (progress_percent BETWEEN 0 AND 100),
                    safe_error_code TEXT,
                    safe_error_message TEXT,
                    worker_job_id TEXT,
                    docker_install_started INTEGER NOT NULL DEFAULT 0
                        CHECK (docker_install_started IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_node_install_jobs_node
                    ON node_install_jobs(node_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_node_install_jobs_state
                    ON node_install_jobs(state);
                CREATE TABLE IF NOT EXISTS node_enrollment_tokens (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    token_digest TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_node_enrollment_tokens_node
                    ON node_enrollment_tokens(node_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_node_enrollment_tokens_expiry
                    ON node_enrollment_tokens(expires_at);
                CREATE TABLE IF NOT EXISTS node_credentials (
                    node_id TEXT PRIMARY KEY REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    token_digest TEXT NOT NULL UNIQUE,
                    issued_at TEXT NOT NULL,
                    last_rotated_at TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS node_commands (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    command_type TEXT NOT NULL CHECK (command_type IN ('PING', 'SELF_TEST')),
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'queued', 'leased', 'acknowledged', 'completed',
                            'failed', 'cancelled'
                        )
                    ),
                    lease_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    completed_at TEXT,
                    safe_result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_node_commands_delivery
                    ON node_commands(node_id, state, lease_until, created_at);
                CREATE TABLE IF NOT EXISTS node_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    safe_detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_node_events_node_created
                    ON node_events(node_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS relay_nodes (
                    node_id TEXT PRIMARY KEY
                        REFERENCES restream_nodes(id) ON DELETE CASCADE,
                    service_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
                        service_state IN ('active', 'inactive', 'failed', 'unknown')
                    ),
                    service_enabled INTEGER NOT NULL DEFAULT 0 CHECK (
                        service_enabled IN (0, 1)
                    ),
                    main_process TEXT NOT NULL DEFAULT 'unknown' CHECK (
                        main_process IN ('running', 'stopped', 'failed', 'unknown')
                    ),
                    srt_listener TEXT NOT NULL DEFAULT 'unknown' CHECK (
                        srt_listener IN ('listening', 'closed', 'failed', 'unknown')
                    ),
                    source TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
                        source IN ('SLATE', 'LIVE', 'NONE', 'UNKNOWN')
                    ),
                    input_bitrate_bps INTEGER CHECK (
                        input_bitrate_bps IS NULL OR
                        input_bitrate_bps BETWEEN 0 AND 1000000000
                    ),
                    youtube_forward TEXT NOT NULL DEFAULT 'unknown' CHECK (
                        youtube_forward IN ('active', 'inactive', 'connecting', 'failed', 'unknown')
                    ),
                    overall TEXT NOT NULL DEFAULT 'unknown' CHECK (
                        overall IN ('ok', 'healthy', 'degraded', 'failed', 'offline', 'unknown')
                    ),
                    youtube_url_configured INTEGER NOT NULL DEFAULT 0 CHECK (
                        youtube_url_configured IN (0, 1)
                    ),
                    youtube_key_configured INTEGER NOT NULL DEFAULT 0 CHECK (
                        youtube_key_configured IN (0, 1)
                    ),
                    healthy INTEGER NOT NULL DEFAULT 0 CHECK (healthy IN (0, 1)),
                    portrait_profile INTEGER NOT NULL DEFAULT 0 CHECK (
                        portrait_profile IN (0, 1)
                    ),
                    last_error_code TEXT,
                    current_command_id TEXT,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relay_nodes_last_seen
                    ON relay_nodes(last_seen_at);
                CREATE TABLE IF NOT EXISTS relay_commands (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES relay_nodes(node_id) ON DELETE CASCADE,
                    command_type TEXT NOT NULL CHECK (
                        command_type IN (
                            'STATUS', 'START', 'STOP', 'CONFIGURE_YOUTUBE',
                            'CONFIGURE_YOUTUBE_KEY', 'REVEAL_MOBLIN_URL',
                            'CLEAR_YOUTUBE'
                        )
                    ),
                    payload_encrypted TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'queued', 'leased', 'acknowledged', 'completed',
                            'failed', 'cancelled'
                        )
                    ),
                    lease_until TEXT,
                    expires_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    completed_at TEXT,
                    completion_status TEXT CHECK (
                        completion_status IS NULL OR
                        completion_status IN ('ok', 'failed', 'conflict')
                    ),
                    safe_result_json TEXT,
                    secret_result_encrypted TEXT,
                    secret_consumed_at TEXT,
                    UNIQUE(node_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_relay_commands_delivery
                    ON relay_commands(node_id, state, lease_until, expires_at, created_at);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (2, CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (3, CURRENT_TIMESTAMP);
                COMMIT;
                """
            )
            node_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(restream_nodes)").fetchall()
            }
            if "control_latency_ms" not in node_columns:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    ALTER TABLE restream_nodes ADD COLUMN control_latency_ms REAL
                    CHECK (
                        control_latency_ms IS NULL OR
                        control_latency_ms BETWEEN 0 AND 60000
                    )
                    """
                )
                connection.execute("COMMIT")
            relay_command_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(relay_commands)").fetchall()
            }
            if "request_fingerprint" not in relay_command_columns:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    ALTER TABLE relay_commands
                    ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''
                    """
                )
                connection.execute("COMMIT")
            relay_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(relay_nodes)").fetchall()
            }
            connection.execute("BEGIN IMMEDIATE")
            if "input_bitrate_bps" not in relay_columns:
                connection.execute(
                    """
                    ALTER TABLE relay_nodes ADD COLUMN input_bitrate_bps INTEGER
                    CHECK (
                        input_bitrate_bps IS NULL OR
                        input_bitrate_bps BETWEEN 0 AND 1000000000
                    )
                    """
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (4, CURRENT_TIMESTAMP)"
            )
            connection.execute("COMMIT")
            relay_command_schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relay_commands'"
            ).fetchone()
            supports_key_only = bool(
                relay_command_schema and "CONFIGURE_YOUTUBE_KEY" in str(relay_command_schema["sql"])
            )
            if not supports_key_only:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE relay_commands RENAME TO relay_commands_before_v5;
                    DROP INDEX IF EXISTS idx_relay_commands_delivery;
                    CREATE TABLE relay_commands (
                        id TEXT PRIMARY KEY,
                        node_id TEXT NOT NULL REFERENCES relay_nodes(node_id) ON DELETE CASCADE,
                        command_type TEXT NOT NULL CHECK (
                            command_type IN (
                                'STATUS', 'START', 'STOP', 'CONFIGURE_YOUTUBE',
                                'CONFIGURE_YOUTUBE_KEY', 'REVEAL_MOBLIN_URL',
                                'CLEAR_YOUTUBE'
                            )
                        ),
                        payload_encrypted TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'queued', 'leased', 'acknowledged', 'completed',
                                'failed', 'cancelled'
                            )
                        ),
                        lease_until TEXT,
                        expires_at TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                        idempotency_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        acknowledged_at TEXT,
                        completed_at TEXT,
                        completion_status TEXT CHECK (
                            completion_status IS NULL OR
                            completion_status IN ('ok', 'failed', 'conflict')
                        ),
                        safe_result_json TEXT,
                        secret_result_encrypted TEXT,
                        secret_consumed_at TEXT,
                        UNIQUE(node_id, idempotency_key)
                    );
                    INSERT INTO relay_commands(
                        id, node_id, command_type, payload_encrypted, state,
                        lease_until, expires_at, attempt_count, idempotency_key,
                        request_fingerprint, created_at, acknowledged_at, completed_at,
                        completion_status, safe_result_json, secret_result_encrypted,
                        secret_consumed_at
                    )
                    SELECT
                        id, node_id, command_type, payload_encrypted, state,
                        lease_until, expires_at, attempt_count, idempotency_key,
                        request_fingerprint, created_at, acknowledged_at, completed_at,
                        completion_status, safe_result_json, secret_result_encrypted,
                        secret_consumed_at
                    FROM relay_commands_before_v5;
                    DROP TABLE relay_commands_before_v5;
                    CREATE INDEX idx_relay_commands_delivery
                        ON relay_commands(
                            node_id, state, lease_until, expires_at, created_at
                        );
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                        VALUES (5, CURRENT_TIMESTAMP);
                    COMMIT;
                    """
                )
            else:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    "VALUES (5, CURRENT_TIMESTAMP)"
                )
                connection.execute("COMMIT")

    def ready(self) -> bool:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
                return bool(row and row["version"] == SCHEMA_VERSION)
        except sqlite3.Error:
            return False

    def get_ingest_encrypted(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT stream_key_encrypted FROM ingest_config WHERE id = 1"
            ).fetchone()
            return str(row["stream_key_encrypted"]) if row else None

    def set_ingest_encrypted(self, encrypted_key: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ingest_config(id, stream_key_encrypted, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stream_key_encrypted = excluded.stream_key_encrypted,
                    updated_at = excluded.updated_at
                """,
                (encrypted_key, now),
            )
            connection.execute("COMMIT")

    def create_session(self, id_hash: str, csrf_hash: str, expires_at: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                """
                INSERT INTO sessions(id_hash, csrf_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (id_hash, csrf_hash, now, expires_at),
            )
            connection.execute("COMMIT")

    def get_session(self, id_hash: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?",
                (id_hash, now),
            ).fetchone()
            return dict(row) if row else None

    def update_session_csrf(self, id_hash: str, csrf_hash: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET csrf_hash = ? WHERE id_hash = ?",
                (csrf_hash, id_hash),
            )
            return cursor.rowcount > 0

    def delete_session(self, id_hash: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))

    def count_destinations(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM destinations").fetchone()
            return int(row["count"] if row else 0)

    def list_destinations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM destinations ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_destination(self, destination_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM destinations WHERE id = ?", (destination_id,)
            ).fetchone()
            return dict(row) if row else None

    def create_destination(
        self, *, name: str, server_url: str, encrypted_key: str, enabled: bool
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO destinations(
                    name, server_url, stream_key_encrypted, enabled, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'stopped', ?, ?)
                """,
                (name, server_url, encrypted_key, int(enabled), now, now),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite invariant
                raise RuntimeError("SQLite did not return a destination id")
            destination_id = cursor.lastrowid
        destination = self.get_destination(destination_id)
        if destination is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("Destination was not persisted")
        return destination

    def update_destination(self, destination_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "server_url", "stream_key_encrypted", "enabled"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_destination(destination_id)
        if "enabled" in updates:
            updates["enabled"] = int(bool(updates["enabled"]))
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), destination_id]
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE destinations SET {assignments} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_destination(destination_id)

    def set_destination_state(
        self,
        destination_id: int,
        state: str,
        *,
        error: str | None = None,
        worker_pid: int | None = None,
        started_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE destinations
                SET state = ?, last_error = ?, worker_pid = ?, started_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, error, worker_pid, started_at, utc_now(), destination_id),
            )

    def delete_destination(self, destination_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM destinations WHERE id = ?", (destination_id,))
            return cursor.rowcount > 0

    def add_audit_event(self, event_type: str, detail: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO audit_events(event_type, detail, created_at) VALUES (?, ?, ?)",
                (event_type, detail, utc_now()),
            )
            connection.execute(
                """
                DELETE FROM audit_events
                WHERE id NOT IN (
                    SELECT id FROM audit_events ORDER BY id DESC LIMIT ?
                )
                """,
                (self.audit_limit,),
            )
            connection.execute("COMMIT")

    def list_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (safe_limit,)
            ).fetchall()
            return [dict(row) for row in rows]
