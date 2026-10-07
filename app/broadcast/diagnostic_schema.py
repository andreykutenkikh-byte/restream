"""Migration 13: bounded, append-only stream diagnostics (no credentials)."""

import sqlite3

SCHEMA_VERSION = 13
TABLES = frozenset({"broadcast_quality_history", "broadcast_diagnostic_events"})


def migrate(db: sqlite3.Connection) -> None:
    db.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS broadcast_quality_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        output_id TEXT NOT NULL REFERENCES broadcast_outputs(id),
        route_id TEXT NOT NULL REFERENCES broadcast_routes(id),
        node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        observed_at TEXT NOT NULL,
        signature TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_quality_output_time
        ON broadcast_quality_history(output_id,observed_at,id);
    CREATE INDEX IF NOT EXISTS idx_quality_route_id ON broadcast_quality_history(route_id,id);
    CREATE INDEX IF NOT EXISTS idx_quality_time ON broadcast_quality_history(observed_at);
    CREATE TABLE IF NOT EXISTS broadcast_diagnostic_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        route_id TEXT REFERENCES broadcast_routes(id),
        boot_hash TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        component TEXT NOT NULL,
        code TEXT NOT NULL,
        value INTEGER,
        UNIQUE(node_id,boot_hash,sequence)
    );
    CREATE INDEX IF NOT EXISTS idx_diagnostics_node_time
        ON broadcast_diagnostic_events(node_id,received_at,id);
    CREATE INDEX IF NOT EXISTS idx_diagnostics_time ON broadcast_diagnostic_events(received_at);
    INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(13,CURRENT_TIMESTAMP);
    COMMIT;
    """)
