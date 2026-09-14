"""Exercise the release against immutable main and combined migration sources.

These fixtures are actual git blobs, not a schema reconstructed from this release.
All data and credentials are synthetic. No git, network, worker, or relay is invoked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from test_moblin_hud import seed_existing_schema_v5_data

from app.db import Database
from app.services.moblin_hud import (
    HudSessionAuthenticationError,
    MoblinHudService,
    UsedPairingTokenError,
)
from app.session import NewSession, SessionManager

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hud_release"
HUD_TABLES = {"moblin_hud_devices", "moblin_hud_pairings"}
SESSION_SECRET = "synthetic-main-upgrade-session-secret"
HUD_NOW = datetime(2026, 9, 14, tzinfo=UTC)


def pinned_database(name: str, path: Path) -> Database:
    """Load a digest-checked, test-only source snapshot without invoking Git."""
    provenance = json.loads((FIXTURES / "db_sources.json").read_text(encoding="utf-8"))[name]
    source = (FIXTURES / provenance["filename"]).read_text(encoding="utf-8")
    encoded = source.encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == provenance["sha256"]
    git_object = f"blob {len(encoded)}\0".encode() + encoded
    assert hashlib.sha1(git_object, usedforsecurity=False).hexdigest() == provenance["blob"]
    module = ModuleType(f"hud_release_pinned_{name}")
    exec(compile(source, provenance["filename"], "exec"), module.__dict__)  # noqa: S102 - pinned local fixture
    return cast(Database, module.Database(path))


def schema(database: Database) -> list[tuple[Any, ...]]:
    with database.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]


def columns(database: Database) -> dict[str, list[str]]:
    with database.connect() as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")  # noqa: S608 - fixture schema identifiers
            ]
            for table in tables
        }


def rows(
    database: Database,
    projection: dict[str, list[str]],
    *,
    exclude_v6: bool = False,
) -> dict[str, list[tuple[Any, ...]]]:
    with database.connect() as connection:
        result = {}
        for table, names in projection.items():
            where = " WHERE version != 6" if table == "schema_migrations" and exclude_v6 else ""
            result[table] = [
                tuple(row)
                for row in connection.execute(
                    f"SELECT {', '.join(names)} FROM {table}{where} ORDER BY rowid"  # noqa: S608 - fixture schema identifiers
                )
            ]
        return result


def versions(database: Database) -> list[int]:
    with database.connect() as connection:
        return [
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]


def assert_integrity(database: Database) -> None:
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def backup(database: Database, target: Path) -> None:
    # SQLite backup includes committed WAL data without copying live DB/WAL files.
    with database.connect() as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)


@pytest.fixture()
def main_database(tmp_path: Path) -> tuple[Database, NewSession]:
    database = pinned_database("main", tmp_path / "main.sqlite")
    database.migrate()
    assert database.ready()
    assert versions(database) == [1, 2, 3, 4, 5]
    with database.connect() as connection:
        seed_existing_schema_v5_data(connection)
        # Deleted high IDs catch an accidental sequence reconstruction from MAX(id).
        connection.execute(
            "INSERT INTO destinations(id, name, server_url, stream_key_encrypted, "
            "created_at, updated_at) VALUES (99, 'Deleted fixture', "
            "'rtmps://sink.example/live2', 'synthetic-ciphertext', 'created', 'updated')"
        )
        connection.execute("DELETE FROM destinations WHERE id = 99")
        connection.execute(
            "INSERT INTO node_events(id, node_id, event_type, created_at) "
            "VALUES (97, 'existing-moblin_relay', 'synthetic.deleted', 'created')"
        )
        connection.execute("DELETE FROM node_events WHERE id = 97")
        connection.execute(
            "INSERT INTO audit_events(id, event_type, created_at) "
            "VALUES (108, 'synthetic.deleted', 'created')"
        )
        connection.execute("DELETE FROM audit_events WHERE id = 108")
    session = SessionManager(database, SESSION_SECRET, 3600).create()
    return database, session


def test_pinned_main_upgrade_preserves_all_data_schema_sequences_and_admin_auth(
    main_database: tuple[Database, NewSession],
) -> None:
    main, session = main_database
    original_schema = schema(main)
    main_columns = columns(main)
    original_rows = rows(main, main_columns)
    assert all(original_rows.values()), "Every main table contains synthetic state"
    candidate = Database(main.path)
    assert not candidate.ready()

    for _ in range(2):
        candidate = Database(main.path)  # Fresh instance models process reopen.
        candidate.migrate()
        assert candidate.ready()
        assert versions(candidate) == [1, 2, 3, 4, 5, 7]
        assert [
            entry for entry in schema(candidate) if entry[2] not in HUD_TABLES
        ] == original_schema
        actual = rows(candidate, main_columns)
        actual["schema_migrations"] = [row for row in actual["schema_migrations"] if row[0] <= 5]
        assert actual == original_rows
        assert "node_kind" not in columns(candidate)["restream_nodes"]
        assert "install_profile" not in columns(candidate)["node_install_jobs"]
        sessions = SessionManager(candidate, SESSION_SECRET, 3600)
        assert sessions.get(session.token) is not None
        assert sessions.validate_csrf(session.token, session.csrf_token)
        assert_integrity(candidate)

    destination = candidate.create_destination(
        name="Post-upgrade synthetic output",
        server_url="rtmps://sink.example/live2",
        encrypted_key="synthetic-ciphertext",
        enabled=False,
    )
    assert destination["id"] == 100
    with candidate.connect() as connection:
        assert (
            connection.execute(
                "INSERT INTO node_events(node_id, event_type, created_at) "
                "VALUES ('existing-moblin_relay', 'synthetic.next', 'created')"
            ).lastrowid
            == 98
        )
        assert (
            connection.execute(
                "INSERT INTO audit_events(event_type, created_at) "
                "VALUES ('synthetic.next', 'created')"
            ).lastrowid
            == 109
        )


def test_actual_combined_migration_fills_v6_without_changing_existing_or_hud_state(
    main_database: tuple[Database, NewSession],
) -> None:
    main, admin_session = main_database
    candidate = Database(main.path)
    candidate.migrate()
    hud = MoblinHudService(candidate, clock=lambda: HUD_NOW)
    paired_grant = hud.create_pairing("Paired fixture")
    paired = hud.consume_pairing(paired_grant.pairing_token)
    revoked = hud.consume_pairing(hud.create_pairing("Revoked fixture").pairing_token)
    hud.revoke_device(revoked.device_id)
    pending = hud.create_pairing("Pending fixture")
    original_columns = columns(candidate)
    original_rows = rows(candidate, original_columns)
    original_schema = schema(candidate)

    for _ in range(2):
        combined = pinned_database("combined", main.path)
        combined.migrate()
        assert combined.ready()
        assert versions(combined) == [1, 2, 3, 4, 5, 6, 7]
        assert rows(combined, original_columns, exclude_v6=True) == original_rows
        changed_tables = {"restream_nodes", "node_install_jobs"}
        assert [entry for entry in schema(combined) if entry[2] not in changed_tables] == [
            entry for entry in original_schema if entry[2] not in changed_tables
        ]
        with combined.connect() as connection:
            assert [
                tuple(row)
                for row in connection.execute(
                    "SELECT id, node_kind FROM restream_nodes ORDER BY id"
                )
            ] == [
                ("existing-generic_node", "generic_node"),
                ("existing-moblin_relay", "moblin_relay"),
            ]
            assert [
                tuple(row)
                for row in connection.execute(
                    "SELECT id, install_profile FROM node_install_jobs ORDER BY id"
                )
            ] == [("job-1", "generic_node"), ("job-2", "moblin_relay")]
        assert_integrity(combined)
        sessions = SessionManager(combined, SESSION_SECRET, 3600)
        assert sessions.validate_csrf(admin_session.token, admin_session.csrf_token)
        reopened = MoblinHudService(combined, clock=lambda: HUD_NOW)
        assert reopened.authenticate_session(paired.session_token)["id"] == paired.device_id
        with pytest.raises(HudSessionAuthenticationError):
            reopened.authenticate_session(revoked.session_token)
        with pytest.raises(UsedPairingTokenError):
            reopened.consume_pairing(paired_grant.pairing_token)

    # The still-unused fragment survives the combined migration and remains single use.
    assert reopened.consume_pairing(pending.pairing_token).device_id == pending.device_id
    with pytest.raises(UsedPairingTokenError):
        reopened.consume_pairing(pending.pairing_token)


def test_isolated_backup_rollback_restores_main_without_mutating_candidate(
    main_database: tuple[Database, NewSession],
    tmp_path: Path,
) -> None:
    main, admin_session = main_database
    original_columns = columns(main)
    original_rows = rows(main, original_columns)
    original_schema = schema(main)
    backup_path = tmp_path / "before-hud.sqlite"
    backup(main, backup_path)
    candidate = Database(main.path)
    candidate.migrate()
    hud = MoblinHudService(candidate, clock=lambda: HUD_NOW)
    hud_session = hud.consume_pairing(hud.create_pairing().pairing_token)
    candidate_columns = columns(candidate)
    candidate_rows = rows(candidate, candidate_columns)
    assert not pinned_database("main", candidate.path).ready(), "Image-only rollback is not ready"

    restored_path = tmp_path / "isolated-restored-main.sqlite"
    backup(Database(backup_path), restored_path)
    for _ in range(2):
        restored = pinned_database("main", restored_path)
        restored.migrate()
        assert restored.ready()
        assert versions(restored) == [1, 2, 3, 4, 5]
        assert schema(restored) == original_schema
        assert rows(restored, original_columns) == original_rows
        assert HUD_TABLES.isdisjoint(columns(restored))
        assert SessionManager(restored, SESSION_SECRET, 3600).validate_csrf(
            admin_session.token, admin_session.csrf_token
        )
        assert_integrity(restored)
    assert rows(candidate, candidate_columns) == candidate_rows
    assert candidate.ready()
    assert hud.authenticate_session(hud_session.session_token)["id"] == hud_session.device_id


def test_hud_foreign_keys_cascade_only_hud_data(main_database: tuple[Database, NewSession]) -> None:
    main, _ = main_database
    candidate = Database(main.path)
    candidate.migrate()
    hud = MoblinHudService(candidate, clock=lambda: HUD_NOW)
    pending = hud.create_pairing()
    legacy_columns = {
        table: names for table, names in columns(candidate).items() if table not in HUD_TABLES
    }
    before = rows(candidate, legacy_columns)
    with candidate.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO moblin_hud_pairings("
                "id, device_id, token_digest, expires_at, created_at) "
                "VALUES ('orphan', 'absent-device', ?, '2030-01-01', 'created')",
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO node_events(node_id, event_type, created_at) "
                "VALUES ('absent-node', 'synthetic', 'created')"
            )
        connection.execute("DELETE FROM moblin_hud_devices WHERE id = ?", (pending.device_id,))
        assert connection.execute("SELECT COUNT(*) FROM moblin_hud_pairings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM moblin_hud_devices").fetchone()[0] == 0
    assert rows(candidate, legacy_columns) == before
    assert_integrity(candidate)


def test_failed_hud_ddl_rolls_back_tables_indexes_and_marker_together(
    main_database: tuple[Database, NewSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, _ = main_database
    candidate = Database(main.path)
    original_schema = schema(main)
    original_columns = columns(main)
    original_rows = rows(main, original_columns)
    connect = candidate.connect

    def deny_final_index(action: int, name: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_CREATE_INDEX and name == "idx_moblin_hud_pairings_expiry":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def failing_connect() -> Iterator[sqlite3.Connection]:
        with connect() as connection:
            connection.set_authorizer(deny_final_index)
            yield connection

    monkeypatch.setattr(candidate, "connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        candidate.migrate()
    monkeypatch.setattr(candidate, "connect", connect)
    assert schema(main) == original_schema
    assert rows(main, original_columns) == original_rows
    assert versions(main) == [1, 2, 3, 4, 5]
    assert main.ready()
    candidate.migrate()
    assert candidate.ready()
    assert versions(candidate) == [1, 2, 3, 4, 5, 7]
    assert_integrity(candidate)
