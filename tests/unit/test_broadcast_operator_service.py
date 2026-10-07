from __future__ import annotations

import pytest
import test_broadcast_control as fixtures

from app.broadcast.models import BroadcastError
from app.broadcast.operator import OperatorService
from app.broadcast.store import BroadcastStore

store = fixtures.store


def test_pairing_and_device_expiry_are_independent_and_fail_closed(
    store: BroadcastStore,
) -> None:  # noqa: F811
    service = OperatorService(store)
    sid = fixtures.session(store)
    expired_pair = service.create(sid, "Expired link", 60)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_operator_pairings SET expires_at='2000-01-01'")
    with pytest.raises(BroadcastError, match="pairing_rejected"):
        service.pair(expired_pair["pairing_token"])

    active = service.create(sid, "Short session", 5)
    token = service.pair(active["pairing_token"])
    assert service.authenticate(token)["session_id"] == sid
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_operators SET expires_at='2000-01-01' WHERE id=?", (active["id"],)
        )
    with pytest.raises(BroadcastError, match="authentication_required"):
        service.authenticate(token)

    expired_device = service.create(sid, "Expired before pairing", 5)
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_operators SET expires_at='2000-01-01' WHERE id=?",
            (expired_device["id"],),
        )
    with pytest.raises(BroadcastError, match="pairing_rejected"):
        service.pair(expired_device["pairing_token"])
    revoked = service.create(sid, "Revoked before pairing", 60)
    service.revoke(revoked["id"])
    with pytest.raises(BroadcastError, match="pairing_rejected"):
        service.pair(revoked["pairing_token"])

    for _ in range(19):
        service.create(sid, "Bounded device", 60)
    with pytest.raises(BroadcastError, match="operator_limit"):
        service.create(sid, "Over limit", 60)
