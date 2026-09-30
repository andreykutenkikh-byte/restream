"""Migration 11: separate operator capability and explicit egress stop evidence."""

import sqlite3

SCHEMA_VERSION = 11
TABLES = frozenset({"broadcast_operators", "broadcast_operator_pairings"})


def migrate(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = {r[1] for r in db.execute("PRAGMA table_info(broadcast_media_observations)")}
        additions = {
            "publisher_running": "INTEGER NOT NULL DEFAULT 0",
            "runtime_secret_present": "INTEGER NOT NULL DEFAULT 0",
            "egress_generation": "INTEGER NOT NULL DEFAULT 0",
            "egress_lease_id": "TEXT",
            "publisher_bytes": "INTEGER NOT NULL DEFAULT 0",
            "source_switch_gap_ms": "REAL",
        }
        for name, definition in additions.items():
            if name not in existing:
                db.execute(
                    f"ALTER TABLE broadcast_media_observations ADD COLUMN {name} {definition}"
                )
        db.execute(
            "CREATE TABLE IF NOT EXISTS broadcast_operators ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES broadcast_sessions(id),"
            "label TEXT NOT NULL, token_digest TEXT UNIQUE, expires_at TEXT NOT NULL,"
            "revoked_at TEXT, created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS broadcast_operator_pairings ("
            "digest TEXT PRIMARY KEY, operator_id TEXT NOT NULL REFERENCES broadcast_operators(id),"
            "expires_at TEXT NOT NULL, used_at TEXT)"
        )
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(11,CURRENT_TIMESTAMP)")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
