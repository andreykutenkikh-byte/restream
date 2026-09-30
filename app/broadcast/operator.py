"""A separate session-scoped capability: switch/cancel only, never an admin session."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.broadcast.models import BroadcastError
from app.broadcast.store import BroadcastStore
from app.db import utc_now


class OperatorService:
    def __init__(self, store: BroadcastStore) -> None:
        self.store = store

    def create(self, session_id: str, label: str, ttl_minutes: int) -> dict[str, str]:
        token, identifier = secrets.token_urlsafe(32), secrets.token_hex(16)
        now = datetime.now(UTC)
        with self.store.transaction() as db:
            self.store.row(db, "SELECT id FROM broadcast_sessions WHERE id=?", (session_id,))
            if (
                db.execute(
                    "SELECT COUNT(*) FROM broadcast_operators WHERE session_id=? "
                    "AND revoked_at IS NULL "
                    "AND expires_at>?",
                    (session_id, now.isoformat()),
                ).fetchone()[0]
                >= 20
            ):
                raise BroadcastError("operator_limit")
            db.execute(
                "INSERT INTO broadcast_operators VALUES (?,?,?,NULL,?,NULL,?)",
                (
                    identifier,
                    session_id,
                    label,
                    (now + timedelta(minutes=ttl_minutes)).isoformat(),
                    now.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO broadcast_operator_pairings VALUES (?,?,?,NULL)",
                (
                    self.store.fingerprint(["pair", token]),
                    identifier,
                    (now + timedelta(minutes=10)).isoformat(),
                ),
            )
            self.store.event(db, session_id, "operator.created", detail={"operator_id": identifier})
        return {"id": identifier, "pairing_token": token}

    def pair(self, token: str) -> str:
        session_token = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT p.*,o.session_id,o.revoked_at,o.expires_at AS operator_expires "
                "FROM broadcast_operator_pairings p JOIN broadcast_operators o "
                "ON o.id=p.operator_id "
                "WHERE p.digest=?",
                (self.store.fingerprint(["pair", token]),),
            ).fetchone()
            if (
                not row
                or row["used_at"]
                or row["revoked_at"]
                or min(row["expires_at"], row["operator_expires"]) <= utc_now()
            ):
                raise BroadcastError("operator_pairing_rejected", 401)
            db.execute(
                "UPDATE broadcast_operator_pairings SET used_at=? WHERE digest=?",
                (utc_now(), row["digest"]),
            )
            db.execute(
                "UPDATE broadcast_operators SET token_digest=? WHERE id=?",
                (self.store.fingerprint(["operator", session_token]), row["operator_id"]),
            )
            self.store.event(
                db, row["session_id"], "operator.paired", detail={"operator_id": row["operator_id"]}
            )
        return session_token

    def authenticate(self, token: str | None) -> dict[str, Any]:
        if not token or len(token) != 43:
            raise BroadcastError("operator_authentication_required", 401)
        with self.store.database.connect() as db:
            row = db.execute(
                "SELECT id,session_id,expires_at FROM broadcast_operators WHERE token_digest=? "
                "AND revoked_at IS NULL AND expires_at>?",
                (self.store.fingerprint(["operator", token]), utc_now()),
            ).fetchone()
        if not row:
            raise BroadcastError("operator_authentication_required", 401)
        return dict(row)

    def revoke(self, identifier: str) -> None:
        with self.store.transaction() as db:
            row = self.store.row(
                db, "SELECT session_id FROM broadcast_operators WHERE id=?", (identifier,)
            )
            db.execute(
                "UPDATE broadcast_operators SET revoked_at=?,token_digest=NULL WHERE id=?",
                (utc_now(), identifier),
            )
            self.store.event(
                db, row["session_id"], "operator.revoked", detail={"operator_id": identifier}
            )
