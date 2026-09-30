"""Additive migration 8. Does not execute or mark the separate native migration 6."""

import sqlite3

SCHEMA_VERSION = 8
TABLES = frozenset(
    {
        "broadcast_sources",
        "broadcast_sessions",
        "broadcast_outputs",
        "broadcast_routes",
        "youtube_accounts",
        "youtube_bindings",
        "youtube_operations",
        "youtube_oauth_states",
        "broadcast_requests",
        "broadcast_events",
        "broadcast_switches",
    }
)


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS broadcast_sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind = 'moblin'),
            ingress_node_id TEXT NOT NULL REFERENCES restream_nodes(id),
            profile_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broadcast_sessions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL UNIQUE REFERENCES broadcast_sources(id),
            name TEXT NOT NULL,
            policy TEXT NOT NULL DEFAULT 'manual' CHECK(policy IN ('manual','assisted','auto')),
            auto_enabled INTEGER NOT NULL DEFAULT 0 CHECK(auto_enabled = 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS youtube_accounts (
            channel_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            tokens_encrypted TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('connected','revoked','disconnected')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broadcast_outputs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES broadcast_sessions(id),
            name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'youtube' CHECK(platform = 'youtube'),
            mode TEXT NOT NULL CHECK(mode IN ('manual','youtube_api')),
            visibility TEXT NOT NULL CHECK(visibility IN ('private','unlisted','public')),
            scheduled_start TEXT,
            desired_enabled INTEGER NOT NULL DEFAULT 0 CHECK(desired_enabled IN (0,1)),
            state TEXT NOT NULL DEFAULT 'DRAFT',
            safe_error_code TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_broadcast_outputs_session
            ON broadcast_outputs(session_id);
        CREATE TABLE IF NOT EXISTS broadcast_routes (
            id TEXT PRIMARY KEY,
            output_id TEXT NOT NULL REFERENCES broadcast_outputs(id),
            node_id TEXT NOT NULL REFERENCES restream_nodes(id),
            role TEXT NOT NULL DEFAULT 'standby' CHECK(role IN ('current','warm','standby')),
            youtube_slot TEXT CHECK(youtube_slot IN ('PRIMARY','BACKUP')),
            source_kind TEXT NOT NULL DEFAULT 'unknown'
                CHECK(source_kind IN ('direct','forwarded','unknown')),
            desired_enabled INTEGER NOT NULL DEFAULT 0 CHECK(desired_enabled IN (0,1)),
            generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(output_id,node_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_current_route
            ON broadcast_routes(output_id) WHERE role = 'current';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_slot
            ON broadcast_routes(output_id,youtube_slot) WHERE youtube_slot IS NOT NULL;
        CREATE TABLE IF NOT EXISTS youtube_bindings (
            output_id TEXT PRIMARY KEY REFERENCES broadcast_outputs(id),
            channel_id TEXT,
            broadcast_id TEXT UNIQUE,
            stream_id TEXT UNIQUE,
            ingestion_mode TEXT NOT NULL DEFAULT 'rtmps' CHECK(ingestion_mode = 'rtmps'),
            credentials_encrypted TEXT,
            credential_fingerprint TEXT UNIQUE,
            lifecycle_status TEXT NOT NULL DEFAULT 'unknown',
            stream_status TEXT NOT NULL DEFAULT 'unknown',
            health_status TEXT NOT NULL DEFAULT 'unknown',
            has_backup INTEGER NOT NULL DEFAULT 0 CHECK(has_backup IN (0,1)),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS youtube_operations (
            output_id TEXT PRIMARY KEY REFERENCES broadcast_outputs(id),
            marker TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL DEFAULT 'NEW',
            broadcast_id TEXT,
            stream_id TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            safe_error_code TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS youtube_oauth_states (
            digest TEXT PRIMARY KEY,
            session_digest TEXT NOT NULL,
            verifier_encrypted TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS broadcast_requests (
            scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            result_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(scope,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS broadcast_switches (
            id TEXT PRIMARY KEY,
            output_id TEXT NOT NULL REFERENCES broadcast_outputs(id),
            old_route_id TEXT NOT NULL REFERENCES broadcast_routes(id),
            target_route_id TEXT NOT NULL REFERENCES broadcast_routes(id),
            state TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            cutover_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            safe_error_code TEXT,
            durations_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(old_route_id != target_route_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_active_switch
            ON broadcast_switches(output_id) WHERE active = 1;
        CREATE TABLE IF NOT EXISTS broadcast_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES broadcast_sessions(id),
            output_id TEXT REFERENCES broadcast_outputs(id),
            switch_id TEXT REFERENCES broadcast_switches(id),
            event_type TEXT NOT NULL,
            safe_detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_broadcast_events_session
            ON broadcast_events(session_id,id);
        INSERT OR IGNORE INTO schema_migrations(version,applied_at)
            VALUES (8,CURRENT_TIMESTAMP);
        COMMIT;
        """
    )
