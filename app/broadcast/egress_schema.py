"""Additive migration 10: output-owned credential authority and revocable runtime leases."""

import sqlite3

SCHEMA_VERSION = 10
TABLES = frozenset({"broadcast_egress_authority", "broadcast_egress_leases"})


def migrate(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {r[1] for r in db.execute("PRAGMA table_info(broadcast_routes)")}
        if "media_warm" not in columns:
            db.execute(
                "ALTER TABLE broadcast_routes ADD COLUMN media_warm INTEGER NOT NULL "
                "DEFAULT 0 CHECK(media_warm IN (0,1))"
            )
        db.execute(
            "CREATE TABLE IF NOT EXISTS broadcast_egress_authority ("
            "output_id TEXT PRIMARY KEY REFERENCES broadcast_outputs(id),"
            "generation INTEGER NOT NULL DEFAULT 0, intent_fingerprint TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS broadcast_egress_leases ("
            "id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES broadcast_outputs(id),"
            "route_id TEXT NOT NULL REFERENCES broadcast_routes(id),"
            "node_id TEXT NOT NULL REFERENCES restream_nodes(id),"
            "youtube_slot TEXT NOT NULL CHECK(youtube_slot IN ('PRIMARY','BACKUP')),"
            "generation INTEGER NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,"
            "revoked_at TEXT, state TEXT NOT NULL CHECK(state IN ('ACTIVE','REVOKED','EXPIRED')),"
            "credential_fingerprint TEXT NOT NULL)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_egress_slot ON "
            "broadcast_egress_leases(output_id,youtube_slot) WHERE state='ACTIVE'"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_egress_route ON "
            "broadcast_egress_leases(route_id) WHERE state='ACTIVE'"
        )
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(10,CURRENT_TIMESTAMP)")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
