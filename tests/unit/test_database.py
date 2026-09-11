import sqlite3
from pathlib import Path

import pytest

from app.db import SCHEMA_VERSION, Database


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    database.migrate()
    assert database.ready()
    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        relay_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(relay_nodes)")
        }
        node_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(restream_nodes)")
        }
        job_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(node_install_jobs)")
        }
    assert version == SCHEMA_VERSION == 6
    assert "input_bitrate_bps" in relay_columns
    assert "node_kind" in node_columns
    assert "install_profile" in job_columns


def test_relay_input_bitrate_column_enforces_bounds(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO restream_nodes(
                id, display_name, address, resolved_ip, ssh_port, ssh_username,
                status, created_at, updated_at
            ) VALUES ('node', 'node', 'relay.example', '192.0.2.1', 22, 'root',
                      'ready', 'now', 'now')
            """
        )
        connection.execute(
            "INSERT INTO relay_nodes(node_id, input_bitrate_bps, created_at, updated_at) "
            "VALUES ('node', 1000000000, 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE relay_nodes SET input_bitrate_bps = 1000000001 WHERE node_id = 'node'"
            )


def test_v6_migration_classifies_existing_relays_and_their_install_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "schema-v5.sqlite")
    database.migrate()
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO restream_nodes(
                id, display_name, address, resolved_ip, ssh_port, ssh_username,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 22, 'root', 'ready', 'created', 'updated')
            """,
            [
                ("relay", "relay", "relay.example", "192.0.2.10"),
                ("generic", "generic", "generic.example", "192.0.2.11"),
            ],
        )
        connection.execute(
            "INSERT INTO relay_nodes(node_id, created_at, updated_at) "
            "VALUES ('relay', 'created', 'updated')"
        )
        connection.executemany(
            """
            INSERT INTO node_install_jobs(
                id, node_id, state, current_step, progress_percent,
                created_at, updated_at, finished_at
            ) VALUES (?, ?, 'completed', 'completed', 100,
                      'created', 'updated', 'finished')
            """,
            [("relay-job", "relay"), ("generic-job", "generic")],
        )
        connection.execute("ALTER TABLE restream_nodes DROP COLUMN node_kind")
        connection.execute("ALTER TABLE node_install_jobs DROP COLUMN install_profile")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")

    database.migrate()

    with database.connect() as connection:
        nodes = {
            row["id"]: row["node_kind"]
            for row in connection.execute("SELECT id, node_kind FROM restream_nodes")
        }
        jobs = {
            row["id"]: row["install_profile"]
            for row in connection.execute("SELECT id, install_profile FROM node_install_jobs")
        }
    assert nodes == {"generic": "generic_node", "relay": "moblin_relay"}
    assert jobs == {"generic-job": "generic_node", "relay-job": "moblin_relay"}


def test_schema_v3_database_is_upgraded_with_nullable_bitrate(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO restream_nodes(
                id, display_name, address, resolved_ip, ssh_port, ssh_username,
                status, created_at, updated_at
            ) VALUES ('relay-node', 'HK relay', 'relay.example', '192.0.2.10', 22,
                      'root', 'ready', 'created', 'updated')
            """
        )
        connection.execute(
            "INSERT INTO relay_nodes(node_id, created_at, updated_at) "
            "VALUES ('relay-node', 'created', 'updated')"
        )
        connection.execute("ALTER TABLE relay_nodes DROP COLUMN input_bitrate_bps")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 4")
        connection.executescript(
            """
            ALTER TABLE relay_commands RENAME TO relay_commands_current;
            DROP INDEX IF EXISTS idx_relay_commands_delivery;
            CREATE TABLE relay_commands (
                id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL REFERENCES relay_nodes(node_id) ON DELETE CASCADE,
                command_type TEXT NOT NULL CHECK (
                    command_type IN (
                        'STATUS', 'START', 'STOP', 'CONFIGURE_YOUTUBE',
                        'REVEAL_MOBLIN_URL', 'CLEAR_YOUTUBE'
                    )
                ),
                payload_encrypted TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_until TEXT,
                expires_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                acknowledged_at TEXT,
                completed_at TEXT,
                completion_status TEXT,
                safe_result_json TEXT,
                secret_result_encrypted TEXT,
                secret_consumed_at TEXT,
                UNIQUE(node_id, idempotency_key)
            );
            INSERT INTO relay_commands SELECT * FROM relay_commands_current;
            DROP TABLE relay_commands_current;
            CREATE INDEX idx_relay_commands_delivery
                ON relay_commands(node_id, state, lease_until, expires_at, created_at);
            """
        )
        connection.executemany(
            """
            INSERT INTO relay_commands(
                id, node_id, command_type, payload_encrypted, state, lease_until,
                expires_at, attempt_count, idempotency_key, request_fingerprint,
                created_at, acknowledged_at, completed_at, completion_status,
                safe_result_json, secret_result_encrypted, secret_consumed_at
            ) VALUES (?, 'relay-node', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "queued-command",
                    "CONFIGURE_YOUTUBE",
                    "ciphertext-queued",
                    "queued",
                    None,
                    "2030-01-01T00:00:00+00:00",
                    0,
                    "idem-queued",
                    "fingerprint-queued",
                    "2026-09-02T00:00:00+00:00",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    "completed-command",
                    "STATUS",
                    "ciphertext-tombstone",
                    "completed",
                    None,
                    "2030-01-01T00:00:00+00:00",
                    1,
                    "idem-completed",
                    "fingerprint-completed",
                    "2026-09-02T00:01:00+00:00",
                    "2026-09-02T00:01:01+00:00",
                    "2026-09-02T00:01:02+00:00",
                    "ok",
                    '{"overall":"offline"}',
                    None,
                    None,
                ),
                (
                    "secret-command",
                    "REVEAL_MOBLIN_URL",
                    "ciphertext-empty",
                    "completed",
                    None,
                    "2030-01-01T00:00:00+00:00",
                    1,
                    "idem-secret",
                    "fingerprint-secret",
                    "2026-09-02T00:02:00+00:00",
                    "2026-09-02T00:02:01+00:00",
                    "2026-09-02T00:02:02+00:00",
                    "ok",
                    '{"overall":"offline"}',
                    "encrypted-secret-result",
                    None,
                ),
            ],
        )
        before = [
            dict(row)
            for row in connection.execute("SELECT * FROM relay_commands ORDER BY id").fetchall()
        ]
    assert database.ready() is False

    database.migrate()
    database.migrate()

    assert database.ready() is True
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(relay_nodes)")}
        node = connection.execute(
            "SELECT node_kind FROM restream_nodes WHERE id = 'relay-node'"
        ).fetchone()
        value = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        command_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relay_commands'"
        ).fetchone()[0]
        after = [
            dict(row)
            for row in connection.execute("SELECT * FROM relay_commands ORDER BY id").fetchall()
        ]
        delivery_index = {
            row["name"] for row in connection.execute("PRAGMA index_list(relay_commands)")
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                """
                INSERT INTO relay_commands(
                    id, node_id, command_type, payload_encrypted, state,
                    expires_at, idempotency_key, created_at
                ) VALUES ('duplicate-idempotency', 'relay-node', 'STATUS',
                          'ciphertext', 'queued', '2030-01-01T00:00:00+00:00',
                          'idem-queued', '2026-09-02T00:03:00+00:00')
                """
            )
    assert "input_bitrate_bps" in columns
    assert value == 6
    assert node["node_kind"] == "moblin_relay"
    assert "CONFIGURE_YOUTUBE_KEY" in command_schema
    assert after == before
    assert "idx_relay_commands_delivery" in delivery_index
    assert foreign_key_errors == []


def test_destination_lifecycle(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    destination = database.create_destination(
        name="YouTube",
        server_url="rtmps://example.test/live2",
        encrypted_key="encrypted",
        enabled=False,
    )
    assert database.count_destinations() == 1
    assert destination["state"] == "stopped"

    updated = database.update_destination(destination["id"], enabled=True)
    assert updated is not None
    assert updated["enabled"] == 1

    database.set_destination_state(destination["id"], "waiting_for_input")
    assert database.get_destination(destination["id"])["state"] == "waiting_for_input"
    assert database.delete_destination(destination["id"])


def test_audit_log_is_bounded(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite", audit_limit=3)
    database.migrate()
    for index in range(5):
        database.add_audit_event("test", str(index))
    events = database.list_audit_events(limit=10)
    assert [event["detail"] for event in events] == ["4", "3", "2"]
