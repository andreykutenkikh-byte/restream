"""Small SQLite repository with idempotent schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


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
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, CURRENT_TIMESTAMP);
                COMMIT;
                """
            )

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
