from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_broadcast_control as fixtures
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_broadcast_media import enable_nodes

from app.broadcast.envelope import open_envelope
from app.broadcast.media_runtime import MediaRuntime
from app.broadcast.store import BroadcastStore

store = fixtures.store


def empty_runtime(private: X25519PrivateKey, directory: Path) -> MediaRuntime:
    runtime = object.__new__(MediaRuntime)
    runtime.private_key, runtime.node_id = private, "relay-a"
    runtime.generation, runtime.issued_at, runtime.fingerprint = -1, "", ""
    runtime.restarted = False
    runtime.output_fences, runtime.plan = {}, {"routes": []}
    runtime.egress_lock = threading.RLock()
    runtime.fence_file = directory / "fence.json"
    runtime.paths, runtime.probes, runtime.publishers, runtime.forwarders = {}, {}, {}, {}
    runtime.local_password = "synthetic-local-password"
    runtime.ports = SimpleNamespace(rtsp=10000)
    runtime._path = lambda name, config: runtime.paths.update({name: config})
    runtime.http = Mock()
    return runtime


def test_revoke_erases_runtime_and_replay_cannot_resurrect(
    store: BroadcastStore, tmp_path: Path
) -> None:  # noqa: F811
    control, keys = enable_nodes(store)
    output = store.create_output(
        fixtures.session(store), fixtures.manual_output(), "create-egress-test"
    )
    store.intent(output, True, "start-egress-test")
    before = control.desired("relay-a")
    runtime = empty_runtime(keys["relay-a"], tmp_path)
    runtime.accept(before)
    first = runtime.plan["routes"][0]
    assert first["egress_lease"]["node_id"] == "relay-a"
    assert "synthetic-key-one" not in runtime.fence_file.read_text()
    publisher = Mock(argv=["synthetic-key-one"])
    runtime.publishers[first["id"]] = ("identity", publisher)
    store.intent(output, False, "stop-egress-test")
    runtime.accept(control.desired("relay-a"))
    publisher.close.assert_called_once()
    assert not publisher.argv and not runtime.publishers
    assert "synthetic-key-one" not in json.dumps(runtime.plan)
    with pytest.raises(ValueError, match="Stale"):
        runtime.accept(before)
    with store.database.connect() as db:
        assert db.execute("SELECT state FROM broadcast_egress_leases").fetchone()[0] == "REVOKED"
        assert "synthetic-key-one" not in "\n".join(db.iterdump())
    assert "synthetic-key-one" not in json.dumps(store.snapshot())
    # A cold process retains only the non-secret fence, no credential cache.
    cold = empty_runtime(keys["relay-a"], tmp_path)
    saved = json.loads(runtime.fence_file.read_text())
    cold.generation, cold.issued_at = saved["generation"], saved["issued_at"]
    cold.fingerprint, cold.output_fences = saved["fingerprint"], saved["output_fences"]
    cold.restarted = True
    with pytest.raises(ValueError, match="Stale"):
        cold.accept(before)
    assert cold.plan == {"routes": []}


def test_expiry_stops_publisher_and_never_renews_expired_id(
    store: BroadcastStore, tmp_path: Path
) -> None:  # noqa: F811
    control, keys = enable_nodes(store)
    output = store.create_output(
        fixtures.session(store), fixtures.manual_output(), "create-expiry-test"
    )
    store.intent(output, True, "start-expiry-test")
    runtime = empty_runtime(keys["relay-a"], tmp_path)
    runtime.accept(control.desired("relay-a"))
    route = runtime.plan["routes"][0]
    original_id = route["egress_lease"]["id"]
    worker = Mock(argv=["synthetic-key-one"])
    runtime.publishers[route["id"]] = ("identity", worker)
    # During a control-plane outage, valid media is preserved.
    runtime.expire_egress()
    worker.close.assert_not_called()
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    route["egress_lease"]["expires_at"] = expired
    runtime.expire_egress()
    worker.close.assert_called_once()
    assert not worker.argv and not route["destination"] and not route["egress_lease"]
    with store.transaction() as db:
        db.execute("UPDATE broadcast_egress_leases SET expires_at=?", (expired,))
    plan = open_envelope(keys["relay-a"], control.desired("relay-a"), "relay-a")
    assert not plan["routes"][0]["destination"]
    store.intent(output, True, "start-expiry-test")  # Duplicate cannot mint a new lease.
    plan = open_envelope(keys["relay-a"], control.desired("relay-a"), "relay-a")
    assert not plan["routes"][0]["egress_lease"]
    store.intent(output, True, "explicit-restart-test")
    plan = open_envelope(keys["relay-a"], control.desired("relay-a"), "relay-a")
    assert plan["routes"][0]["egress_lease"]["id"] != original_id


def test_standby_has_no_credential_and_removed_node_is_revoked(store: BroadcastStore) -> None:  # noqa: F811
    control, keys = enable_nodes(store)
    output = store.create_output(
        fixtures.session(store), fixtures.manual_output(), "create-removal-test"
    )
    store.add_route(output, "relay-b", "standby-removal-test")
    store.intent(output, True, "start-removal-test")
    standby = open_envelope(keys["relay-b"], control.desired("relay-b"), "relay-b")
    assert standby["routes"][0]["destination"] is None
    assert standby["routes"][0]["egress_lease"] is None
    with store.transaction() as db:
        db.execute(
            "UPDATE restream_nodes SET revoked_at=? WHERE id='relay-a'",
            (datetime.now(UTC).isoformat(),),
        )
    control.desired("relay-b")
    with store.database.connect() as db:
        assert db.execute("SELECT state FROM broadcast_egress_leases").fetchone()[0] == "REVOKED"
