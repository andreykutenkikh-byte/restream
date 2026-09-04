from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def test_schema_v6_upgrade_preserves_existing_data_and_adds_hud_tables(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "schema-v6.sqlite")
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(event_type, detail, created_at)
            VALUES ('existing.event', 'preserve-me', '2026-09-03T00:00:00+00:00')
            """
        )
        connection.execute("DROP TABLE moblin_hud_pairings")
        connection.execute("DROP TABLE moblin_hud_devices")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
    assert database.ready() is False

    database.migrate()
    database.migrate()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        preserved = connection.execute(
            "SELECT detail FROM audit_events WHERE event_type = 'existing.event'"
        ).fetchone()["detail"]
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'moblin_hud_%'
                """
            )
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert version == SCHEMA_VERSION == 7
    assert preserved == "preserve-me"
    assert tables == {"moblin_hud_devices", "moblin_hud_pairings"}
    assert foreign_key_errors == []


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
