from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.core.security import digest_opaque_token
from app.db import SCHEMA_VERSION, Database
from app.services.moblin_hud import (
    EXPIRED_PAIRING_CLEANUP_LIMIT,
    HUD_SESSION_SCOPE,
    HUD_SESSION_TTL_SECONDS,
    LAST_SEEN_WRITE_INTERVAL_SECONDS,
    MAX_ACTIVE_DEVICES,
    MAX_ACTIVE_PAIRINGS,
    PAIRING_TTL_SECONDS,
    DeviceLimitError,
    ExpiredPairingTokenError,
    HudDeviceNotFoundError,
    HudSessionAuthenticationError,
    InvalidPairingTokenError,
    MoblinHudService,
    PairingLimitError,
    UsedPairingTokenError,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


@pytest.fixture()
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 9, 4, 0, 0, tzinfo=UTC))


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "moblin-hud.sqlite")
    value.migrate()
    return value


@pytest.fixture()
def service(database: Database, clock: MutableClock) -> MoblinHudService:
    return MoblinHudService(database, clock=clock)


def database_dump(database: Database) -> str:
    with database.connect() as connection:
        return "\n".join(connection.iterdump())


def pair_device(service: MoblinHudService) -> tuple[str, str]:
    pairing = service.create_pairing()
    session = service.consume_pairing(pairing.pairing_token)
    assert pairing.device_id == session.device_id
    return session.device_id, session.session_token


def test_schema_v7_adds_scoped_hud_tables_idempotently(database: Database) -> None:
    database.migrate()
    with database.connect() as connection:
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'moblin_hud_%'
                """
            )
        }
        device_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(moblin_hud_devices)")
        }
        pairing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(moblin_hud_pairings)")
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert version == SCHEMA_VERSION == 7
    assert tables == {"moblin_hud_devices", "moblin_hud_pairings"}
    assert {
        "id",
        "display_name",
        "session_digest",
        "scope",
        "created_at",
        "updated_at",
        "last_seen_at",
        "expires_at",
        "revoked_at",
    } == device_columns
    assert {
        "id",
        "device_id",
        "token_digest",
        "expires_at",
        "used_at",
        "created_at",
    } == pairing_columns
    assert foreign_key_errors == []


def seed_existing_schema_v6_data(connection: sqlite3.Connection) -> None:
    """Representative durable pre-HUD state; every credential here is synthetic."""
    created = "2026-09-03T00:00:00+00:00"
    updated = "2026-09-03T00:01:00+00:00"
    expires = "2030-09-03T00:00:00+00:00"

    def insert(table: str, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608 - fixed fixture identifiers
            tuple(values.values()),
        )

    insert(
        "ingest_config",
        {
            "id": 1,
            "stream_key_encrypted": "synthetic-ingest-ciphertext",
            "updated_at": updated,
        },
    )
    insert(
        "sessions",
        {
            "id_hash": "a" * 64,
            "csrf_hash": "b" * 64,
            "created_at": created,
            "expires_at": expires,
        },
    )
    insert(
        "destinations",
        {
            "id": 41,
            "name": "Existing synthetic YouTube",
            "server_url": "rtmps://sink.example/live2",
            "stream_key_encrypted": "synthetic-destination-ciphertext",
            "enabled": 1,
            "state": "streaming",
            "last_error": None,
            "started_at": created,
            "worker_pid": 4321,
            "created_at": created,
            "updated_at": updated,
        },
    )
    for index, kind in enumerate(("generic_node", "moblin_relay"), start=1):
        node_id = f"existing-{kind}"
        insert(
            "restream_nodes",
            {
                "id": node_id,
                "node_kind": kind,
                "display_name": f"Synthetic node {index}",
                "address": f"node{index}.example",
                "resolved_ip": f"192.0.2.{index}",
                "ssh_port": 2220 + index,
                "ssh_username": "operator",
                "host_key_algorithm": "ssh-ed25519",
                "host_key_fingerprint": f"SHA256:synthetic-{index}",
                "host_key_trust_mode": "tofu",
                "status": "ready",
                "public_ip": f"192.0.2.{index}",
                "hostname": f"relay-{index}",
                "os_name": "Ubuntu",
                "os_version": "24.04",
                "architecture": "x86_64",
                "cpu_count": 2,
                "uptime_seconds": 12345.5,
                "load_1m": 0.25,
                "cpu_percent": 12.5,
                "memory_total_bytes": 4294967296,
                "memory_available_bytes": 3221225472,
                "disk_total_bytes": 42949672960,
                "disk_free_bytes": 32212254720,
                "ffmpeg_version": "6.1.1",
                "ffprobe_version": "6.1.1",
                "agent_version": "1.2.6",
                "protocol_version": 1,
                "capabilities_json": '["moblin_relay"]' if index == 2 else '["ping"]',
                "current_command_id": "relay-queued" if index == 2 else "node-queued",
                "control_latency_ms": 123.5,
                "last_seen_at": updated,
                "created_at": created,
                "updated_at": updated,
                "revoked_at": None,
            },
        )
        insert(
            "node_install_jobs",
            {
                "id": f"job-{index}",
                "node_id": node_id,
                "install_profile": kind,
                "state": "completed" if index == 2 else "failed",
                "current_step": "finalize",
                "progress_percent": 100 if index == 2 else 60,
                "safe_error_code": None if index == 2 else "remote_command_timeout",
                "safe_error_message": None if index == 2 else "Bounded synthetic failure",
                "worker_job_id": f"worker-{index}",
                "docker_install_started": int(index == 1),
                "created_at": created,
                "updated_at": updated,
                "finished_at": updated,
            },
        )
        insert(
            "node_enrollment_tokens",
            {
                "id": f"enrollment-{index}",
                "node_id": node_id,
                "token_digest": str(index) * 64,
                "expires_at": expires,
                "used_at": updated if index == 2 else None,
                "created_at": created,
            },
        )
        insert(
            "node_credentials",
            {
                "node_id": node_id,
                "token_digest": str(index + 2) * 64,
                "issued_at": created,
                "last_rotated_at": updated,
                "revoked_at": None,
            },
        )
        insert(
            "node_events",
            {
                "id": index + 50,
                "node_id": node_id,
                "event_type": "node.heartbeat",
                "safe_detail": '{"status":"ready"}',
                "created_at": updated,
            },
        )
    insert(
        "node_commands",
        {
            "id": "node-queued",
            "node_id": "existing-generic_node",
            "command_type": "PING",
            "payload_json": '{"synthetic":true}',
            "state": "queued",
            "lease_until": None,
            "attempt_count": 0,
            "created_at": created,
            "acknowledged_at": None,
            "completed_at": None,
            "safe_result_json": None,
        },
    )
    insert(
        "relay_nodes",
        {
            "node_id": "existing-moblin_relay",
            "service_state": "active",
            "service_enabled": 0,
            "main_process": "running",
            "srt_listener": "listening",
            "source": "LIVE",
            "input_bitrate_bps": 4000123,
            "youtube_forward": "active",
            "overall": "healthy",
            "youtube_url_configured": 1,
            "youtube_key_configured": 1,
            "healthy": 1,
            "portrait_profile": 1,
            "last_error_code": None,
            "current_command_id": "relay-queued",
            "last_seen_at": updated,
            "created_at": created,
            "updated_at": updated,
        },
    )
    for completed in (False, True):
        identity = "completed" if completed else "queued"
        insert(
            "relay_commands",
            {
                "id": f"relay-{identity}",
                "node_id": "existing-moblin_relay",
                "command_type": "REVEAL_MOBLIN_URL" if completed else "CONFIGURE_YOUTUBE_KEY",
                "payload_encrypted": f"synthetic-{identity}-payload-ciphertext",
                "state": identity,
                "lease_until": None,
                "expires_at": expires,
                "attempt_count": int(completed),
                "idempotency_key": f"idem-{identity}",
                "request_fingerprint": f"fingerprint-{identity}",
                "created_at": created,
                "acknowledged_at": updated if completed else None,
                "completed_at": updated if completed else None,
                "completion_status": "ok" if completed else None,
                "safe_result_json": '{"overall":"healthy"}' if completed else None,
                "secret_result_encrypted": "synthetic-unconsumed-secret-ciphertext"
                if completed
                else None,
                "secret_consumed_at": None,
            },
        )
    insert(
        "audit_events",
        {
            "id": 61,
            "event_type": "relay.command_queued",
            "detail": "relay-queued",
            "created_at": created,
        },
    )
    insert(
        "audit_events",
        {
            "id": 62,
            "event_type": "node.installed",
            "detail": "existing-moblin_relay",
            "created_at": updated,
        },
    )


def test_schema_v6_upgrade_preserves_all_existing_rows_schema_and_foreign_keys(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "schema-v6.sqlite")
    database.migrate()

    def schema(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]

    def existing_rows(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
        # These are all pre-v7 tables, including sequence and migration metadata.
        tables = (
            "ingest_config",
            "sessions",
            "destinations",
            "audit_events",
            "restream_nodes",
            "node_install_jobs",
            "node_enrollment_tokens",
            "node_credentials",
            "node_commands",
            "node_events",
            "relay_nodes",
            "relay_commands",
            "sqlite_sequence",
            "schema_migrations",
        )
        result = {}
        for table in tables:
            suffix = " WHERE version <= 6" if table == "schema_migrations" else ""
            result[table] = [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table}{suffix} ORDER BY rowid"  # noqa: S608 - fixed fixture table names
                )
            ]
        return result

    with database.connect() as connection:
        seed_existing_schema_v6_data(connection)
        expected_schema_v7 = schema(connection)
        # Verified against the PR15 base schema: v7 adds only these two HUD
        # tables (and their indexes), plus marker7. No legacy column differs.
        connection.execute("DROP TABLE moblin_hud_pairings")
        connection.execute("DROP TABLE moblin_hud_devices")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        legacy_schema = schema(connection)
        assert legacy_schema == [
            row
            for row in expected_schema_v7
            if row[2] not in {"moblin_hud_devices", "moblin_hud_pairings"}
        ]
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        before = existing_rows(connection)
        assert all(before.values()), "Every legacy table must contain real synthetic fixture rows"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.ready() is False

    for _ in range(2):
        database.migrate()
        assert database.ready() is True
        with database.connect() as connection:
            assert schema(connection) == expected_schema_v7
            assert existing_rows(connection) == before
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM moblin_hud_devices").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM moblin_hud_pairings").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 7"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                == SCHEMA_VERSION
                == 7
            )


def test_pairing_has_256_bit_entropy_and_persists_only_digest(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
) -> None:
    pairing = service.create_pairing("  Moblin iPhone  ")

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", pairing.pairing_token)
    assert datetime.fromisoformat(pairing.expires_at) == clock.value + timedelta(
        seconds=PAIRING_TTL_SECONDS
    )
    assert pairing.pairing_token not in repr(pairing)
    dump = database_dump(database)
    assert pairing.pairing_token not in dump
    assert digest_opaque_token(pairing.pairing_token) in dump
    with database.connect() as connection:
        device = connection.execute(
            "SELECT * FROM moblin_hud_devices WHERE id = ?", (pairing.device_id,)
        ).fetchone()
        audit = connection.execute(
            "SELECT event_type, detail FROM audit_events ORDER BY id"
        ).fetchall()
    assert device["display_name"] == "Moblin iPhone"
    assert device["session_digest"] is None
    assert device["scope"] == HUD_SESSION_SCOPE
    assert [(row["event_type"], row["detail"]) for row in audit] == [
        ("moblin_hud.pairing_created", pairing.device_id)
    ]


@pytest.mark.parametrize("token", [None, "", "x" * 42, "x" * 44, "!" * 43])
def test_invalid_pairing_is_constant_time_and_read_only(
    service: MoblinHudService,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    comparisons: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return left == right

    monkeypatch.setattr("app.core.security.hmac.compare_digest", compare)
    before = database_dump(database)
    with pytest.raises(InvalidPairingTokenError):
        service.consume_pairing(token)
    assert database_dump(database) == before
    assert comparisons == [(digest_opaque_token("invalid"), "0" * 64)]


def test_pairing_expires_at_exact_ttl_without_being_consumed(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
) -> None:
    pairing = service.create_pairing()
    clock.advance(seconds=PAIRING_TTL_SECONDS)
    with pytest.raises(ExpiredPairingTokenError):
        service.consume_pairing(pairing.pairing_token)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT used_at FROM moblin_hud_pairings WHERE device_id = ?",
            (pairing.device_id,),
        ).fetchone()
    assert row["used_at"] is None


def test_pairing_is_one_time_device_bound_and_session_is_digest_only(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
) -> None:
    pairing = service.create_pairing("Travel iPhone")
    session = service.consume_pairing(pairing.pairing_token)

    assert session.device_id == pairing.device_id
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", session.session_token)
    assert datetime.fromisoformat(session.expires_at) == clock.value + timedelta(
        seconds=HUD_SESSION_TTL_SECONDS
    )
    assert session.scope == HUD_SESSION_SCOPE
    assert session.session_token not in repr(session)
    dump = database_dump(database)
    assert pairing.pairing_token not in dump
    assert session.session_token not in dump
    assert digest_opaque_token(pairing.pairing_token) in dump
    assert digest_opaque_token(session.session_token) in dump
    with pytest.raises(UsedPairingTokenError):
        service.consume_pairing(pairing.pairing_token)
    with database.connect() as connection:
        audit = connection.execute(
            "SELECT event_type, detail FROM audit_events ORDER BY id"
        ).fetchall()
    assert [(row["event_type"], row["detail"]) for row in audit] == [
        ("moblin_hud.pairing_created", pairing.device_id),
        ("moblin_hud.device_paired", pairing.device_id),
    ]


def test_concurrent_pairing_replay_issues_exactly_one_session(
    service: MoblinHudService,
) -> None:
    pairing = service.create_pairing()

    def consume() -> str:
        try:
            return service.consume_pairing(pairing.pairing_token).session_token
        except UsedPairingTokenError:
            return "used"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))
    assert outcomes.count("used") == 1
    assert len([outcome for outcome in outcomes if outcome != "used"]) == 1


def test_active_pairing_limit_and_expired_cleanup(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
) -> None:
    grants = [service.create_pairing() for _ in range(MAX_ACTIVE_PAIRINGS)]
    with pytest.raises(PairingLimitError):
        service.create_pairing()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM moblin_hud_devices").fetchone()[0] == 3

    clock.advance(seconds=PAIRING_TTL_SECONDS + 1)
    replacement = service.create_pairing()
    with database.connect() as connection:
        pairings = connection.execute(
            "SELECT device_id FROM moblin_hud_pairings ORDER BY created_at"
        ).fetchall()
        devices = connection.execute("SELECT id FROM moblin_hud_devices").fetchall()
    assert [row["device_id"] for row in pairings] == [replacement.device_id]
    assert [row["id"] for row in devices] == [replacement.device_id]
    assert all(grant.device_id != replacement.device_id for grant in grants)


def test_active_device_limit_allows_replacement_after_revoke(
    service: MoblinHudService,
) -> None:
    devices = [pair_device(service) for _ in range(MAX_ACTIVE_DEVICES)]
    with pytest.raises(DeviceLimitError):
        service.create_pairing()

    service.revoke_device(devices[0][0])
    new_device_id, new_session = pair_device(service)
    assert new_device_id not in {device_id for device_id, _ in devices}
    assert service.authenticate_session(new_session)["status"] == "active"


def test_device_limit_is_rechecked_atomically_when_pending_pairing_is_consumed(
    service: MoblinHudService,
) -> None:
    existing = [pair_device(service) for _ in range(MAX_ACTIVE_DEVICES - 1)]
    pending = [service.create_pairing() for _ in range(MAX_ACTIVE_PAIRINGS)]
    service.consume_pairing(pending[0].pairing_token)

    with pytest.raises(DeviceLimitError):
        service.consume_pairing(pending[1].pairing_token)
    service.revoke_device(existing[0][0])
    replacement = service.consume_pairing(pending[1].pairing_token)
    assert replacement.device_id == pending[1].device_id


def test_expired_pairing_cleanup_is_bounded_and_removes_only_orphans(
    database: Database,
    clock: MutableClock,
) -> None:
    service = MoblinHudService(database, clock=clock, expired_pairing_cleanup_limit=2)
    expired = (clock.value - timedelta(seconds=1)).isoformat()
    created = (clock.value - timedelta(minutes=11)).isoformat()
    with database.connect() as connection:
        for index in range(5):
            device_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO moblin_hud_devices(
                    id, display_name, scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, f"Expired {index}", HUD_SESSION_SCOPE, created, created),
            )
            connection.execute(
                """
                INSERT INTO moblin_hud_pairings(
                    id, device_id, token_digest, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    device_id,
                    digest_opaque_token(f"expired-{index}"),
                    expired,
                    created,
                ),
            )

    assert service.prune_expired_pairings() == 2
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM moblin_hud_pairings").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM moblin_hud_devices").fetchone()[0] == 3
    assert service.prune_expired_pairings() == 2


def test_authentication_returns_only_safe_scope_and_rejects_malformed_tokens(
    service: MoblinHudService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_id, session_token = pair_device(service)
    view = service.authenticate_session(session_token)
    assert view["id"] == device_id
    assert view["scope"] == HUD_SESSION_SCOPE
    assert view["active"] is True
    assert set(view) == {
        "id",
        "display_name",
        "scope",
        "created_at",
        "updated_at",
        "last_seen_at",
        "expires_at",
        "revoked_at",
        "status",
        "active",
    }

    comparisons: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return left == right

    monkeypatch.setattr("app.core.security.hmac.compare_digest", compare)
    with pytest.raises(HudSessionAuthenticationError):
        service.authenticate_session("oversized-" + "x" * 10_000)
    assert comparisons == [(digest_opaque_token("invalid"), "0" * 64)]


def test_last_seen_is_written_no_more_than_once_per_minute(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_id, session_token = pair_device(service)
    with database.connect() as connection:
        original = connection.execute(
            "SELECT last_seen_at, updated_at FROM moblin_hud_devices WHERE id = ?",
            (device_id,),
        ).fetchone()

    statements: list[str] = []
    original_connect = database.connect

    @contextmanager
    def traced_connect() -> Iterator[sqlite3.Connection]:
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(database, "connect", traced_connect)
    clock.advance(seconds=LAST_SEEN_WRITE_INTERVAL_SECONDS - 1)
    service.authenticate_session(session_token)
    assert not any(statement.startswith("BEGIN IMMEDIATE") for statement in statements)
    with original_connect() as connection:
        unchanged = connection.execute(
            "SELECT last_seen_at, updated_at FROM moblin_hud_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    assert dict(unchanged) == dict(original)

    statements.clear()
    clock.advance(seconds=1)
    updated_view = service.authenticate_session(session_token)
    assert any(statement.startswith("BEGIN IMMEDIATE") for statement in statements)
    assert updated_view["last_seen_at"] == clock.value.isoformat()


def test_expired_and_revoked_sessions_are_rejected_and_listed_safely(
    service: MoblinHudService,
    database: Database,
    clock: MutableClock,
) -> None:
    expired_device, expired_session = pair_device(service)
    clock.advance(seconds=HUD_SESSION_TTL_SECONDS)
    with pytest.raises(HudSessionAuthenticationError):
        service.authenticate_session(expired_session)
    assert service.list_devices()[0]["status"] == "expired"

    active_pairing = service.create_pairing("Replacement")
    active_session = service.consume_pairing(active_pairing.pairing_token)
    revoked = service.revoke_device(active_session.device_id)
    assert revoked["status"] == "revoked"
    assert revoked["active"] is False
    with pytest.raises(HudSessionAuthenticationError):
        service.authenticate_session(active_session.session_token)
    again = service.revoke_device(active_session.device_id)
    assert again == revoked
    with database.connect() as connection:
        revocation_events = connection.execute(
            """
            SELECT detail FROM audit_events
            WHERE event_type = 'moblin_hud.device_revoked'
            """
        ).fetchall()
    assert [row["detail"] for row in revocation_events] == [active_session.device_id]
    assert expired_device != active_session.device_id


def test_tampered_scope_is_rejected(service: MoblinHudService, database: Database) -> None:
    device_id, session_token = pair_device(service)
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE moblin_hud_devices SET scope = 'admin' WHERE id = ?",
            (device_id,),
        )
    with pytest.raises(HudSessionAuthenticationError):
        service.authenticate_session(session_token)


def test_revoke_unknown_device_and_invalid_display_names_are_safe(
    service: MoblinHudService,
) -> None:
    for name in ("", " ", "x" * 81, "phone\nname"):
        with pytest.raises(ValueError):
            service.create_pairing(name)
    with pytest.raises(TypeError):
        service.create_pairing(None)  # type: ignore[arg-type]
    with pytest.raises(HudDeviceNotFoundError):
        service.revoke_device(str(uuid4()))


def test_naive_clock_is_rejected(database: Database) -> None:
    service = MoblinHudService(database, clock=lambda: datetime(2026, 9, 4))
    with pytest.raises(ValueError, match="timezone-aware"):
        service.create_pairing()


@pytest.mark.parametrize("limit", [0, EXPIRED_PAIRING_CLEANUP_LIMIT + 1])
def test_cleanup_limit_cannot_be_unbounded(database: Database, limit: int) -> None:
    with pytest.raises(ValueError, match="between"):
        MoblinHudService(database, expired_pairing_cleanup_limit=limit)
