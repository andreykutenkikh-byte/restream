"""Scoped pairing and read-only device sessions for the Moblin streamer HUD."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, TypedDict, cast
from uuid import uuid4

from app.core.security import (
    digest_opaque_token,
    generate_enrollment_token,
    generate_session_id,
    verify_opaque_token_digest,
)
from app.db import Database

PAIRING_TTL_SECONDS: Final = 10 * 60
HUD_SESSION_TTL_SECONDS: Final = 30 * 24 * 60 * 60
LAST_SEEN_WRITE_INTERVAL_SECONDS: Final = 60
MAX_ACTIVE_PAIRINGS: Final = 3
MAX_ACTIVE_DEVICES: Final = 5
EXPIRED_PAIRING_CLEANUP_LIMIT: Final = 100
HUD_SESSION_SCOPE: Final = "stream_monitor"
_DUMMY_DIGEST: Final = "0" * 64
_OPAQUE_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}\Z")

Clock = Callable[[], datetime]
HudDeviceStatus = Literal["active", "expired", "revoked"]


class HudDeviceView(TypedDict):
    """Secret-free device metadata safe for the administrator and HUD caller."""

    id: str
    display_name: str
    scope: str
    created_at: str
    updated_at: str
    last_seen_at: str | None
    expires_at: str
    revoked_at: str | None
    status: HudDeviceStatus
    active: bool


class MoblinHudError(RuntimeError):
    """Base class for safe Moblin HUD domain failures."""


class PairingLimitError(MoblinHudError):
    """Raised when too many unexpired pairing grants already exist."""


class DeviceLimitError(MoblinHudError):
    """Raised when the maximum number of active HUD devices is reached."""


class PairingTokenError(MoblinHudError):
    """Base class for invalid, expired, or replayed pairing grants."""


class InvalidPairingTokenError(PairingTokenError):
    """Raised when a pairing token is missing or unknown."""


class ExpiredPairingTokenError(PairingTokenError):
    """Raised when a pairing grant is past its ten-minute lifetime."""


class UsedPairingTokenError(PairingTokenError):
    """Raised when a one-time pairing grant is replayed."""


class HudSessionAuthenticationError(MoblinHudError):
    """Raised for all missing, invalid, expired, or revoked HUD sessions."""


class HudDeviceNotFoundError(MoblinHudError):
    """Raised when a paired HUD device does not exist."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PairingGrant:
    """One-time pairing material returned only to its creating admin request."""

    device_id: str
    pairing_token: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HudSessionGrant:
    """Opaque read-only session material returned only after successful pairing."""

    device_id: str
    session_token: str = field(repr=False)
    expires_at: str
    scope: str = HUD_SESSION_SCOPE


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _candidate_digest(token: str | None) -> str:
    if not isinstance(token, str) or _OPAQUE_TOKEN_PATTERN.fullmatch(token) is None:
        return _DUMMY_DIGEST
    return digest_opaque_token(token)


def _verification_token(token: str | None) -> str:
    if not isinstance(token, str) or _OPAQUE_TOKEN_PATTERN.fullmatch(token) is None:
        return "invalid"
    return token


class MoblinHudService:
    """Owns atomic one-time pairing and scoped HUD session transitions."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = _now,
        expired_pairing_cleanup_limit: int = EXPIRED_PAIRING_CLEANUP_LIMIT,
    ) -> None:
        if not 1 <= expired_pairing_cleanup_limit <= EXPIRED_PAIRING_CLEANUP_LIMIT:
            raise ValueError(
                "expired_pairing_cleanup_limit must be between 1 and "
                f"{EXPIRED_PAIRING_CLEANUP_LIMIT}"
            )
        self.database = database
        self.clock = clock
        self.expired_pairing_cleanup_limit = expired_pairing_cleanup_limit

    def _time(self) -> datetime:
        return _as_utc(self.clock())

    def _add_audit_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        device_id: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(event_type, detail, created_at) VALUES (?, ?, ?)",
            (event_type, device_id, now),
        )
        connection.execute(
            """
            DELETE FROM audit_events
            WHERE id NOT IN (
                SELECT id FROM audit_events ORDER BY id DESC LIMIT ?
            )
            """,
            (self.database.audit_limit,),
        )

    def _prune_expired_pairings(
        self,
        connection: sqlite3.Connection,
        now: str,
    ) -> int:
        rows = connection.execute(
            """
            SELECT id, device_id
            FROM moblin_hud_pairings
            WHERE expires_at <= ?
            ORDER BY expires_at ASC, id ASC
            LIMIT ?
            """,
            (now, self.expired_pairing_cleanup_limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                "DELETE FROM moblin_hud_pairings WHERE id = ?",
                (str(row["id"]),),
            )
            connection.execute(
                """
                DELETE FROM moblin_hud_devices
                WHERE id = ? AND session_digest IS NULL
                """,
                (str(row["device_id"]),),
            )
        return len(rows)

    def prune_expired_pairings(self) -> int:
        """Remove at most the configured number of expired pairing records."""

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _timestamp(self._time())
            removed = self._prune_expired_pairings(connection, now)
            connection.execute("COMMIT")
        return removed

    @staticmethod
    def _normalize_display_name(display_name: str) -> str:
        if not isinstance(display_name, str):
            raise TypeError("display_name must be a string")
        normalized = display_name.strip()
        if not 1 <= len(normalized) <= 80:
            raise ValueError("display_name must contain between 1 and 80 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("display_name must not contain control characters")
        return normalized

    def create_pairing(self, display_name: str = "Moblin iPhone") -> PairingGrant:
        """Create a device-bound, ten-minute pairing grant.

        The raw token exists only in the returned value.  Persistence and audit
        records contain its digest and the preallocated device identifier only.
        """

        safe_name = self._normalize_display_name(display_name)
        device_id = str(uuid4())
        pairing_id = str(uuid4())
        pairing_token = generate_enrollment_token()
        token_digest = digest_opaque_token(pairing_token)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_value = self._time()
            now = _timestamp(now_value)
            expires_at = _timestamp(now_value + timedelta(seconds=PAIRING_TTL_SECONDS))
            self._prune_expired_pairings(connection, now)
            active_devices = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM moblin_hud_devices
                WHERE session_digest IS NOT NULL
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (now,),
            ).fetchone()
            if int(active_devices["count"] if active_devices else 0) >= MAX_ACTIVE_DEVICES:
                raise DeviceLimitError("maximum active HUD devices reached")
            active_pairings = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM moblin_hud_pairings
                WHERE used_at IS NULL AND expires_at > ?
                """,
                (now,),
            ).fetchone()
            if int(active_pairings["count"] if active_pairings else 0) >= MAX_ACTIVE_PAIRINGS:
                raise PairingLimitError("maximum active HUD pairings reached")
            connection.execute(
                """
                INSERT INTO moblin_hud_devices(
                    id, display_name, session_digest, scope, created_at,
                    updated_at, last_seen_at, expires_at, revoked_at
                ) VALUES (?, ?, NULL, ?, ?, ?, NULL, NULL, NULL)
                """,
                (device_id, safe_name, HUD_SESSION_SCOPE, now, now),
            )
            connection.execute(
                """
                INSERT INTO moblin_hud_pairings(
                    id, device_id, token_digest, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (pairing_id, device_id, token_digest, expires_at, now),
            )
            self._add_audit_event(
                connection,
                "moblin_hud.pairing_created",
                device_id,
                now,
            )
            connection.execute("COMMIT")
        return PairingGrant(
            device_id=device_id,
            pairing_token=pairing_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _pairing_row(
        connection: sqlite3.Connection,
        candidate_digest: str,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT pairing.id, pairing.device_id, pairing.token_digest,
                       pairing.expires_at, pairing.used_at,
                       device.session_digest, device.revoked_at
                FROM moblin_hud_pairings AS pairing
                JOIN moblin_hud_devices AS device ON device.id = pairing.device_id
                WHERE pairing.token_digest = ?
                """,
                (candidate_digest,),
            ).fetchone(),
        )

    @staticmethod
    def _validate_pairing_row(
        pairing_token: str | None,
        row: sqlite3.Row | None,
        now: datetime,
    ) -> sqlite3.Row:
        expected_digest = str(row["token_digest"]) if row else _DUMMY_DIGEST
        valid_digest = verify_opaque_token_digest(
            _verification_token(pairing_token), expected_digest
        )
        if row is None or not valid_digest:
            raise InvalidPairingTokenError("pairing token is invalid")
        if row["used_at"] is not None or row["session_digest"] is not None:
            raise UsedPairingTokenError("pairing token has already been used")
        if row["revoked_at"] is not None:
            raise InvalidPairingTokenError("pairing token is invalid")
        if _parse_timestamp(str(row["expires_at"])) <= now:
            raise ExpiredPairingTokenError("pairing token has expired")
        return row

    def consume_pairing(self, pairing_token: str | None) -> HudSessionGrant:
        """Atomically exchange a valid one-time token for a HUD-only session."""

        precheck_now = self._time()
        candidate_digest = _candidate_digest(pairing_token)
        with self.database.connect() as connection:
            row = self._pairing_row(connection, candidate_digest)
        self._validate_pairing_row(pairing_token, row, precheck_now)

        session_token = generate_session_id()
        session_digest = digest_opaque_token(session_token)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_value = self._time()
            now = _timestamp(now_value)
            expires_at = _timestamp(now_value + timedelta(seconds=HUD_SESSION_TTL_SECONDS))
            row = self._pairing_row(connection, candidate_digest)
            row = self._validate_pairing_row(pairing_token, row, now_value)
            active_devices = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM moblin_hud_devices
                WHERE session_digest IS NOT NULL
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (now,),
            ).fetchone()
            if int(active_devices["count"] if active_devices else 0) >= MAX_ACTIVE_DEVICES:
                raise DeviceLimitError("maximum active HUD devices reached")
            device_id = str(row["device_id"])
            consumed = connection.execute(
                """
                UPDATE moblin_hud_pairings
                SET used_at = ?
                WHERE id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (now, str(row["id"]), now),
            )
            if consumed.rowcount != 1:  # pragma: no cover - guarded by write lock
                raise UsedPairingTokenError("pairing token has already been used")
            updated = connection.execute(
                """
                UPDATE moblin_hud_devices
                SET session_digest = ?, updated_at = ?, last_seen_at = ?, expires_at = ?
                WHERE id = ? AND session_digest IS NULL AND revoked_at IS NULL
                """,
                (session_digest, now, now, expires_at, device_id),
            )
            if updated.rowcount != 1:  # pragma: no cover - guarded by validation
                raise UsedPairingTokenError("pairing token has already been used")
            self._add_audit_event(
                connection,
                "moblin_hud.device_paired",
                device_id,
                now,
            )
            connection.execute("COMMIT")
        return HudSessionGrant(
            device_id=device_id,
            session_token=session_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _session_row(
        connection: sqlite3.Connection,
        candidate_digest: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT id, display_name, session_digest, scope, created_at, updated_at,
                   last_seen_at, expires_at, revoked_at
            FROM moblin_hud_devices
            WHERE session_digest = ?
            """,
            (candidate_digest,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _validate_session_row(
        session_token: str | None,
        row: sqlite3.Row | None,
        now: datetime,
    ) -> sqlite3.Row:
        expected_digest = str(row["session_digest"]) if row else _DUMMY_DIGEST
        valid_digest = verify_opaque_token_digest(
            _verification_token(session_token), expected_digest
        )
        if (
            row is None
            or not valid_digest
            or row["scope"] != HUD_SESSION_SCOPE
            or row["revoked_at"] is not None
            or row["expires_at"] is None
            or _parse_timestamp(str(row["expires_at"])) <= now
        ):
            raise HudSessionAuthenticationError("HUD session is invalid")
        return row

    @staticmethod
    def _device_view(row: sqlite3.Row | dict[str, object], now: datetime) -> HudDeviceView:
        expires_at = str(row["expires_at"])
        revoked_at = str(row["revoked_at"]) if row["revoked_at"] is not None else None
        if revoked_at is not None:
            status: HudDeviceStatus = "revoked"
        elif _parse_timestamp(expires_at) <= now:
            status = "expired"
        else:
            status = "active"
        return {
            "id": str(row["id"]),
            "display_name": str(row["display_name"]),
            "scope": str(row["scope"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_seen_at": (str(row["last_seen_at"]) if row["last_seen_at"] is not None else None),
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "status": status,
            "active": status == "active",
        }

    def authenticate_session(self, session_token: str | None) -> HudDeviceView:
        """Authenticate a HUD cookie and rate-limit its durable last-seen write."""

        now_value = self._time()
        now = _timestamp(now_value)
        candidate_digest = _candidate_digest(session_token)
        with self.database.connect() as connection:
            row = self._session_row(connection, candidate_digest)
        row = self._validate_session_row(session_token, row, now_value)

        cutoff = _timestamp(now_value - timedelta(seconds=LAST_SEEN_WRITE_INTERVAL_SECONDS))
        if row["last_seen_at"] is not None and str(row["last_seen_at"]) > cutoff:
            return self._device_view(row, now_value)

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_value = self._time()
            now = _timestamp(now_value)
            cutoff = _timestamp(now_value - timedelta(seconds=LAST_SEEN_WRITE_INTERVAL_SECONDS))
            row = self._session_row(connection, candidate_digest)
            row = self._validate_session_row(session_token, row, now_value)
            touched = connection.execute(
                """
                UPDATE moblin_hud_devices
                SET last_seen_at = ?, updated_at = ?
                WHERE id = ?
                  AND session_digest = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                  AND (last_seen_at IS NULL OR last_seen_at <= ?)
                """,
                (now, now, str(row["id"]), candidate_digest, now, cutoff),
            )
            if touched.rowcount == 1:
                mutable_row = dict(row)
                mutable_row["last_seen_at"] = now
                mutable_row["updated_at"] = now
                view = self._device_view(mutable_row, now_value)
            else:
                view = self._device_view(row, now_value)
            connection.execute("COMMIT")
        return view

    def list_devices(self) -> list[HudDeviceView]:
        """Return only paired device metadata, never session digests or tokens."""

        now = self._time()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, display_name, scope, created_at, updated_at,
                       last_seen_at, expires_at, revoked_at
                FROM moblin_hud_devices
                WHERE session_digest IS NOT NULL
                ORDER BY created_at DESC, id ASC
                """
            ).fetchall()
        return [self._device_view(row, now) for row in rows]

    def revoke_device(self, device_id: str) -> HudDeviceView:
        """Immediately revoke a paired device without deleting safe metadata."""

        if not isinstance(device_id, str) or not device_id:
            raise HudDeviceNotFoundError("HUD device was not found")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now_value = self._time()
            now = _timestamp(now_value)
            row = connection.execute(
                """
                SELECT id, display_name, scope, created_at, updated_at,
                       last_seen_at, expires_at, revoked_at
                FROM moblin_hud_devices
                WHERE id = ? AND session_digest IS NOT NULL
                """,
                (device_id,),
            ).fetchone()
            if row is None:
                raise HudDeviceNotFoundError("HUD device was not found")
            if row["revoked_at"] is None:
                connection.execute(
                    """
                    UPDATE moblin_hud_devices
                    SET revoked_at = ?, updated_at = ?
                    WHERE id = ? AND revoked_at IS NULL
                    """,
                    (now, now, device_id),
                )
                self._add_audit_event(
                    connection,
                    "moblin_hud.device_revoked",
                    device_id,
                    now,
                )
                mutable_row = dict(row)
                mutable_row["revoked_at"] = now
                mutable_row["updated_at"] = now
                view = self._device_view(mutable_row, now_value)
            else:
                view = self._device_view(row, now_value)
            connection.execute("COMMIT")
        return view


__all__ = [
    "EXPIRED_PAIRING_CLEANUP_LIMIT",
    "HUD_SESSION_SCOPE",
    "HUD_SESSION_TTL_SECONDS",
    "LAST_SEEN_WRITE_INTERVAL_SECONDS",
    "MAX_ACTIVE_DEVICES",
    "MAX_ACTIVE_PAIRINGS",
    "PAIRING_TTL_SECONDS",
    "DeviceLimitError",
    "ExpiredPairingTokenError",
    "HudDeviceNotFoundError",
    "HudDeviceView",
    "HudSessionAuthenticationError",
    "HudSessionGrant",
    "InvalidPairingTokenError",
    "MoblinHudError",
    "MoblinHudService",
    "PairingGrant",
    "PairingLimitError",
    "PairingTokenError",
    "UsedPairingTokenError",
]
