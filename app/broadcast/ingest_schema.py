"""Migration 12: optional, agent-confirmed public RTMP ingress."""

import sqlite3

SCHEMA_VERSION = 12


def migrate(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(broadcast_media_nodes)")}
        if "rtmp_port" not in columns:
            db.execute(
                "ALTER TABLE broadcast_media_nodes ADD COLUMN rtmp_port INTEGER "
                "CHECK(rtmp_port IS NULL OR rtmp_port BETWEEN 1024 AND 65535)"
            )
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) "
            "VALUES(12,CURRENT_TIMESTAMP)"
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
