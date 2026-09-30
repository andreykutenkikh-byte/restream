"""Migration 9: opt-in media agents, fenced plans and measured route telemetry."""

import sqlite3

SCHEMA_VERSION = 9
TABLES = frozenset(
    {
        "broadcast_media_nodes",
        "broadcast_source_secrets",
        "broadcast_forwarding",
        "broadcast_media_observations",
    }
)


def migrate(db: sqlite3.Connection) -> None:
    db.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS broadcast_media_nodes (
        node_id TEXT PRIMARY KEY REFERENCES restream_nodes(id),
        public_key TEXT NOT NULL,
        srt_host TEXT NOT NULL,
        srt_port INTEGER NOT NULL CHECK(srt_port BETWEEN 1024 AND 65535),
        capabilities_json TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        generation INTEGER NOT NULL DEFAULT 0,
        plan_fingerprint TEXT,
        last_seen_at TEXT,
        last_sequence INTEGER NOT NULL DEFAULT -1,
        boot_id TEXT,
        safe_error_code TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS broadcast_source_secrets (
        source_id TEXT PRIMARY KEY REFERENCES broadcast_sources(id),
        encrypted TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS broadcast_forwarding (
        route_id TEXT PRIMARY KEY REFERENCES broadcast_routes(id),
        source_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        target_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        generation INTEGER NOT NULL DEFAULT 1,
        encrypted TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL,
        CHECK(source_node_id != target_node_id)
    );
    CREATE TABLE IF NOT EXISTS broadcast_media_observations (
        route_id TEXT PRIMARY KEY REFERENCES broadcast_routes(id),
        node_id TEXT NOT NULL REFERENCES restream_nodes(id),
        plan_generation INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('direct','forwarded','unknown')),
        source_identity TEXT,
        video_pts REAL,
        audio_pts REAL,
        video_frames INTEGER NOT NULL DEFAULT 0,
        audio_packets INTEGER NOT NULL DEFAULT 0,
        bitrate_bps INTEGER,
        publisher_frames INTEGER NOT NULL DEFAULT 0,
        publisher_time_us INTEGER NOT NULL DEFAULT 0,
        publisher_connected INTEGER NOT NULL DEFAULT 0,
        valid_samples INTEGER NOT NULL DEFAULT 0,
        direct_samples INTEGER NOT NULL DEFAULT 0,
        safe_error_code TEXT,
        observed_at TEXT NOT NULL
    );
    INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(9,CURRENT_TIMESTAMP);
    COMMIT;
    """)
