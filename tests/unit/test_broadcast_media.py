from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import test_broadcast_control as control_fixtures
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_broadcast_control import manual_output, session

from app.broadcast.envelope import open_envelope, public_key, seal
from app.broadcast.media_control import MediaControl, MediaHeartbeat, MediaNodeEnable, Observation
from app.broadcast.media_runtime import MediaRuntime
from app.broadcast.models import CAPABILITIES, BroadcastError, MediaProfile, ResourceLimits
from app.broadcast.store import BroadcastStore

store = control_fixtures.store


def enable_nodes(store: BroadcastStore) -> tuple[MediaControl, dict[str, X25519PrivateKey]]:  # noqa: F811
    control = MediaControl(store, test_loopback=True)
    keys = {node: X25519PrivateKey.generate() for node in ("relay-a", "relay-b", "relay-c")}
    for i, (node, key) in enumerate(keys.items()):
        control.enable(
            node,
            MediaNodeEnable(
                public_key=public_key(key),
                srt_host="127.0.0.1",
                srt_port=19000 + i,
                limits=ResourceLimits(),
            ),
        )
        control.heartbeat(node, heartbeat(key))
    return control, keys


def heartbeat(key: X25519PrivateKey, sequence: int = 1, **extra: Any) -> MediaHeartbeat:
    return MediaHeartbeat(
        boot_id="synthetic-agent-boot",
        sequence=sequence,
        public_key=public_key(key),
        capabilities=sorted(CAPABILITIES),
        plan_generation=0,
        **extra,
    )


def test_each_node_only_receives_own_outputs_in_encrypted_envelope(store: BroadcastStore) -> None:  # noqa: F811
    sid = session(store)
    a = store.create_output(sid, manual_output(key="synthetic-a-key"), "synthetic-output-a")
    b = store.create_output(sid, manual_output("relay-b", "synthetic-b-key"), "synthetic-output-b")
    control, keys = enable_nodes(store)
    for output in (a, b):
        store.intent(output, True, "synthetic-intent-start")
    envelopes = {node: control.desired(node) for node in keys}
    for node, envelope in envelopes.items():
        assert "synthetic-a-key" not in json.dumps(envelope)
        assert "synthetic-b-key" not in json.dumps(envelope)
        plan = open_envelope(keys[node], envelope, node)
        text = json.dumps(plan)
        assert ("synthetic-a-key" in text) == (node == "relay-a")
        assert ("synthetic-b-key" in text) == (node == "relay-b")
    forward = open_envelope(keys["relay-b"], envelopes["relay-b"], "relay-b")["routes"][0][
        "forward"
    ]
    assert len(forward["passphrase"]) >= 43
    assert len(forward["token"]) >= 43
    with pytest.raises(ValueError, match="scope"):
        open_envelope(keys["relay-b"], envelopes["relay-a"], "relay-b")
    with pytest.raises(InvalidTag):
        open_envelope(keys["relay-b"], envelopes["relay-a"], "relay-a")
    with store.database.connect() as db:
        dump = "\n".join(db.iterdump())
        assert forward["passphrase"] not in dump and forward["token"] not in dump


def test_v1_capabilities_and_unknown_resource_capacity_fail_closed(store: BroadcastStore) -> None:  # noqa: F811
    control = MediaControl(store)
    with store.database.connect() as db, pytest.raises(BroadcastError, match="not_enabled"):
        control.ready_node(db, "relay-a")
    with pytest.raises(BroadcastError, match="public_media"):
        control.enable(
            "relay-a",
            MediaNodeEnable(
                public_key=public_key(X25519PrivateKey.generate()),
                srt_host="127.0.0.1",
                srt_port=19000,
                limits=ResourceLimits(),
            ),
        )
    control, keys = enable_nodes(store)
    with pytest.raises(BroadcastError, match="stale_heartbeat"):
        control.heartbeat("relay-a", heartbeat(keys["relay-a"]))
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET last_seen_at='2000-01-01T00:00:00+00:00' "
            "WHERE node_id='relay-a'"
        )
    with pytest.raises(BroadcastError, match="stale"):
        control.desired("relay-a")


def test_moving_audio_video_and_publisher_are_separate_from_socket_connect(
    store: BroadcastStore,
) -> None:  # noqa: F811
    sid = session(store)
    output = store.create_output(sid, manual_output(), "synthetic-media-output")
    control, keys = enable_nodes(store)
    store.intent(output, True, "synthetic-media-start")
    envelope = control.desired("relay-a")
    generation = envelope["context"]["generation"]
    lease = open_envelope(keys["relay-a"], envelope, "relay-a")["routes"][0]["egress_lease"]
    route = store.snapshot()["sessions"][0]["outputs"][0]["routes"][0]["id"]
    for seq in range(2, 6):
        data = MediaHeartbeat(
            boot_id="synthetic-agent-boot",
            sequence=seq,
            public_key=public_key(keys["relay-a"]),
            capabilities=sorted(CAPABILITIES),
            plan_generation=generation,
            observations=[
                Observation(
                    route_id=route,
                    source_kind="forwarded",
                    source_identity="connection",
                    video_pts=float(seq),
                    audio_pts=1.0,
                    video_frames=90,
                    audio_packets=90,
                    bitrate_bps=4_000_000,
                    publisher_frames=100,
                    publisher_connected=True,
                    egress_generation=lease["generation"],
                    egress_lease_id=lease["id"],
                )
            ],
        )
        control.heartbeat("relay-a", data)
    with store.database.connect() as db:
        row = db.execute(
            "SELECT valid_samples,direct_samples FROM broadcast_media_observations"
        ).fetchone()
        assert tuple(row) == (0, 0)
    assert store.snapshot()["sessions"][0]["outputs"][0]["state"] != "PUBLISHING"
    data.sequence += 1
    data.plan_generation -= 1
    with pytest.raises(BroadcastError, match="stale_plan_generation"):
        control.heartbeat("relay-a", data)


def test_agent_rejects_expired_fenced_changed_or_overcommitted_plans(tmp_path: Path) -> None:
    private = X25519PrivateKey.generate()
    runtime = object.__new__(MediaRuntime)
    runtime.private_key, runtime.node_id = private, "relay-a"
    runtime.egress_lock = threading.RLock()
    runtime.restarted = False
    runtime.generation, runtime.issued_at, runtime.fingerprint = 3, "", "different"
    now = datetime.now(UTC)
    context = {
        "purpose": "broadcast-desired-v2",
        "node_id": "relay-a",
        "generation": 2,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
    }
    payload = {"routes": [], "exports": [], "sources": {}, "limits": ResourceLimits().model_dump()}
    with pytest.raises(ValueError, match="Stale"):
        runtime.accept(seal(public_key(private), payload, context))
    context["generation"] = 3
    with pytest.raises(ValueError, match="Generation"):
        runtime.accept(seal(public_key(private), payload, context))
    context["generation"] = 4
    context["expires_at"] = (now + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="lifetime"):
        runtime.accept(seal(public_key(private), payload, context))


def test_profile_capacity_and_transport_rotation(store: BroadcastStore) -> None:  # noqa: F811
    sid = session(store)
    output = store.create_output(sid, manual_output("relay-b"), "synthetic-media-output")
    control, keys = enable_nodes(store)
    store.intent(output, True, "synthetic-media-start")
    first = open_envelope(keys["relay-b"], control.desired("relay-b"), "relay-b")
    old = first["routes"][0]["forward"]
    store.intent(output, False, "synthetic-media-stop")
    control.desired("relay-b")
    store.intent(output, True, "synthetic-media-restart")
    new = open_envelope(keys["relay-b"], control.desired("relay-b"), "relay-b")["routes"][0][
        "forward"
    ]
    assert new["path"] != old["path"] and new["passphrase"] != old["passphrase"]
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET profile_json=? WHERE node_id='relay-b'",
            (MediaProfile(width=1920, height=1080).model_dump_json(),),
        )
    with pytest.raises(BroadcastError, match="incompatible"):
        control.desired("relay-b")


def test_media_events_record_transitions_without_heartbeat_flood_or_secrets(
    store: BroadcastStore,
) -> None:  # noqa: F811
    sid = session(store)
    output = store.create_output(sid, manual_output("relay-b"), "event-output-fixture")
    control, keys = enable_nodes(store)
    store.intent(output, True, "event-start-fixture")
    envelope = control.desired("relay-b")
    plan = open_envelope(keys["relay-b"], envelope, "relay-b")
    route, lease = plan["routes"][0]["id"], plan["routes"][0]["egress_lease"]
    for sequence in range(2, 9):
        lost = sequence >= 7
        control.heartbeat(
            "relay-b",
            MediaHeartbeat(
                boot_id="synthetic-agent-boot",
                sequence=sequence,
                public_key=public_key(keys["relay-b"]),
                capabilities=sorted(CAPABILITIES),
                plan_generation=envelope["context"]["generation"],
                observations=[
                    Observation(
                        route_id=route,
                        source_kind="unknown" if lost else "forwarded",
                        source_identity="synthetic-event-source",
                        video_pts=float(sequence),
                        audio_pts=float(sequence),
                        video_frames=sequence * 30,
                        audio_packets=sequence * 48,
                        bitrate_bps=4000000,
                        publisher_connected=not lost,
                        publisher_running=not lost,
                        runtime_secret_present=True,
                        publisher_frames=sequence * 30,
                        publisher_bytes=sequence * 100000,
                        egress_generation=lease["generation"],
                        egress_lease_id=lease["id"],
                        safe_error_code="source_lost" if lost else None,
                    )
                ],
            ),
        )
    with store.database.connect() as db:
        events = db.execute("SELECT event_type,safe_detail_json FROM broadcast_events").fetchall()
    names = [e["event_type"] for e in events]
    for expected in (
        "output.live",
        "output.failed",
        "interrelay.connected",
        "interrelay.disconnected",
    ):
        assert names.count(expected) == 1
    assert "synthetic-key-one" not in str([tuple(e) for e in events])
