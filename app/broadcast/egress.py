"""Only the control plane owns the credential in youtube_bindings.

Leases contain references and HMAC fingerprints, never a plaintext credential.
Intent changes fence the whole output; renewal never revives an expired grant.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.broadcast.store import BroadcastStore

EGRESS_TTL_SECONDS = 300
RENEW_BEFORE_SECONDS = 180


class EgressLeases:
    def __init__(self, store: BroadcastStore) -> None:
        self.store = store

    def sync(self, db: sqlite3.Connection, output_id: str) -> None:
        output = self.store.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
        credential = self.store.row(
            db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,)
        )
        routes = (
            db.execute(
                "SELECT r.* FROM broadcast_routes r JOIN restream_nodes n ON n.id=r.node_id "
                "JOIN broadcast_media_nodes m ON m.node_id=r.node_id WHERE r.output_id=? "
                "AND r.desired_enabled=1 AND r.youtube_slot IS NOT NULL AND m.enabled=1 "
                "AND n.revoked_at IS NULL AND n.status!='revoked' ORDER BY r.id",
                (output_id,),
            ).fetchall()
            if output["desired_enabled"]
            else []
        )
        intent = self.store.fingerprint(
            [
                output["generation"],
                credential["credential_fingerprint"],
                [(r["id"], r["node_id"], r["youtube_slot"], r["generation"]) for r in routes],
            ]
        )
        authority = db.execute(
            "SELECT * FROM broadcast_egress_authority WHERE output_id=?", (output_id,)
        ).fetchone()
        now = datetime.now(UTC)
        stamp = now.isoformat()
        db.execute(
            "UPDATE broadcast_egress_leases SET state='EXPIRED' WHERE output_id=? "
            "AND state='ACTIVE' AND expires_at<=?",
            (output_id, stamp),
        )
        if authority and authority["intent_fingerprint"] == intent:
            # An expired/revoked assignment requires a NEW explicit intent, never a replay.
            return
        generation = authority["generation"] + 1 if authority else 1
        db.execute(
            "INSERT INTO broadcast_egress_authority VALUES (?,?,?) ON CONFLICT(output_id) "
            "DO UPDATE SET generation=excluded.generation,"
            "intent_fingerprint=excluded.intent_fingerprint",
            (output_id, generation, intent),
        )
        db.execute(
            "UPDATE broadcast_egress_leases SET state='REVOKED',revoked_at=? "
            "WHERE output_id=? AND state='ACTIVE'",
            (stamp, output_id),
        )
        if credential["credentials_encrypted"]:
            for route in routes:
                db.execute(
                    "INSERT INTO broadcast_egress_leases VALUES (?,?,?,?,?,?,?,?,NULL,'ACTIVE',?)",
                    (
                        secrets.token_hex(16),
                        output_id,
                        route["id"],
                        route["node_id"],
                        route["youtube_slot"],
                        generation,
                        stamp,
                        (now + timedelta(seconds=EGRESS_TTL_SECONDS)).isoformat(),
                        credential["credential_fingerprint"],
                    ),
                )
        self.store.event(
            db,
            output["session_id"],
            "egress.assignment_changed",
            output_id=output_id,
            detail={"generation": generation, "active_leases": len(routes)},
        )

    def grant(self, db: sqlite3.Connection, route_id: str, node_id: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        # Only the authenticated node's own request can renew its unexpired lease.
        db.execute(
            "UPDATE broadcast_egress_leases SET expires_at=? WHERE route_id=? AND node_id=? "
            "AND state='ACTIVE' AND expires_at>? AND expires_at<?",
            (
                (now + timedelta(seconds=EGRESS_TTL_SECONDS)).isoformat(),
                route_id,
                node_id,
                now.isoformat(),
                (now + timedelta(seconds=RENEW_BEFORE_SECONDS)).isoformat(),
            ),
        )
        row = db.execute(
            "SELECT id,output_id,node_id,youtube_slot,generation,issued_at,expires_at,"
            "credential_fingerprint FROM broadcast_egress_leases WHERE route_id=? "
            "AND node_id=? AND state='ACTIVE' AND expires_at>?",
            (route_id, node_id, datetime.now(UTC).isoformat()),
        ).fetchone()
        return dict(row) if row else None
