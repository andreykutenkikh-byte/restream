"""Migration 14: bounded, authenticated network and OBS measurements."""

import sqlite3

SCHEMA_VERSION = 14
TABLES = frozenset(
    {
        "broadcast_network_links",
        "broadcast_ingress_metrics",
        "broadcast_network_probe_jobs",
        "broadcast_obs_monitors",
        "broadcast_obs_samples",
    }
)


def migrate(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(broadcast_media_nodes)")}
    alteration = (
        "ALTER TABLE broadcast_media_nodes ADD COLUMN probe_port INTEGER;"
        if "probe_port" not in columns
        else ""
    )
    db.executescript(
        """
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS broadcast_network_links (
        route_id TEXT NOT NULL REFERENCES broadcast_routes(id),
        kind TEXT NOT NULL CHECK(kind IN ('tcp','srt')),
        reporter_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY(route_id,kind)
    );
    CREATE TABLE IF NOT EXISTS broadcast_ingress_metrics (
        source_id TEXT PRIMARY KEY REFERENCES broadcast_sources(id),
        reporter_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS broadcast_network_probe_jobs (
        id TEXT PRIMARY KEY,
        route_id TEXT NOT NULL REFERENCES broadcast_routes(id),
        source_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        target_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        state TEXT NOT NULL CHECK(state IN ('WAITING','READY','RUNNING','COMPLETED','FAILED')),
        encrypted TEXT NOT NULL,
        source_ready INTEGER NOT NULL DEFAULT 0,
        target_ready INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        finished_at TEXT,
        throughput_bps INTEGER,
        bytes_received INTEGER,
        elapsed_ms REAL,
        safe_error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_network_probe_route
        ON broadcast_network_probe_jobs(route_id,created_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_network_probe_active ON broadcast_network_probe_jobs((1))
        WHERE state IN ('WAITING','READY','RUNNING');
    CREATE TABLE IF NOT EXISTS broadcast_obs_monitors (
        source_id TEXT PRIMARY KEY REFERENCES broadcast_sources(id),
        token_digest TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        boot_id TEXT,
        last_sequence INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS broadcast_obs_samples (
        source_id TEXT PRIMARY KEY REFERENCES broadcast_sources(id),
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    """
        + alteration
        + """
    INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(14,CURRENT_TIMESTAMP);
    COMMIT;
    """
    )
