from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_broadcast_control as fixtures
from test_broadcast_media import enable_nodes

from app.broadcast.envelope import open_envelope, public_key
from app.broadcast.media_control import MediaHeartbeat, Observation
from app.broadcast.models import CAPABILITIES, BroadcastError
from app.broadcast.store import BroadcastStore
from app.broadcast.switching import SwitchController

store = fixtures.store


class SwitchLab:
    def __init__(self, store: BroadcastStore) -> None:
        self.store = store
        self.media, self.keys = enable_nodes(store)
        self.controller = SwitchController(store, self.media)
        self.session = fixtures.session(store)
        self.output = store.create_output(
            self.session, fixtures.manual_output(), "create-switch-test"
        )
        self.b = store.add_route(self.output, "relay-b", "add-b-switch-test")
        self.c = store.add_route(self.output, "relay-c", "add-c-switch-test")
        self.a = store.snapshot()["sessions"][0]["outputs"][0]["routes"][0]["id"]
        self.store.intent(self.output, True, "start-switch-test")
        with self.store.database.connect() as db:
            db.execute(
                "UPDATE youtube_bindings SET broadcast_id='synthetic-event',"
                "stream_id='synthetic-stream' WHERE output_id=?",
                (self.output,),
            )
        self.sequence = 1
        self.direct = "relay-a"
        self.failed: str | None = None

    def observe(self) -> None:
        self.sequence += 1
        for node, key in self.keys.items():
            envelope = self.media.desired(node)
            plan = open_envelope(key, envelope, node)
            observations = []
            for route in plan["routes"]:
                lease = route["egress_lease"]
                connected = bool(lease) and node != self.failed
                observations.append(
                    Observation(
                        route_id=route["id"],
                        source_kind="direct" if node == self.direct else "forwarded",
                        source_identity=node + self.direct,
                        video_pts=float(self.sequence),
                        audio_pts=float(self.sequence),
                        video_frames=self.sequence * 30,
                        audio_packets=self.sequence * 48,
                        bitrate_bps=4000000,
                        publisher_connected=connected,
                        publisher_running=connected,
                        publisher_frames=self.sequence * 30 if connected else 0,
                        publisher_bytes=self.sequence * 400000 if connected else 0,
                        runtime_secret_present=bool(lease),
                        egress_generation=route["egress_generation"],
                        egress_lease_id=lease["id"] if lease else None,
                        safe_error_code="publisher_failed"
                        if node == self.failed and lease
                        else None,
                        source_switch_gap_ms=500 if node == self.direct else None,
                    )
                )
            self.media.heartbeat(
                node,
                MediaHeartbeat(
                    boot_id="synthetic-agent-boot",
                    sequence=self.sequence,
                    public_key=public_key(key),
                    capabilities=sorted(CAPABILITIES),
                    plan_generation=envelope["context"]["generation"],
                    observations=observations,
                ),
            )

    def state(self, identifier: str) -> str:
        with self.store.database.connect() as db:
            return str(
                db.execute(
                    "SELECT state FROM broadcast_switches WHERE id=?", (identifier,)
                ).fetchone()[0]
            )

    def until(self, identifier: str, state: str) -> None:
        for _ in range(40):
            if self.state(identifier) == state:
                return
            self.observe()
            self.controller.step(identifier)
        raise AssertionError(f"Expected {state}, got {self.state(identifier)}")


def test_two_switches_same_binding_slots_credentials_and_direct_proof(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    with store.database.connect() as db:
        binding = tuple(db.execute("SELECT * FROM youtube_bindings").fetchone())
    for node, target, slot in [("relay-b", lab.b, "BACKUP"), ("relay-c", lab.c, "PRIMARY")]:
        identifier = lab.controller.request(lab.output, target, "switch-request-" + node)
        lab.until(identifier, "TARGET_MEDIA_READY")
        before = open_envelope(lab.keys[node], lab.media.desired(node), node)
        assert before["routes"][0]["destination"] is None
        assert before["routes"][0]["media_enabled"]
        lab.until(identifier, "TARGET_CREDENTIAL_LEASED")
        with store.database.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM broadcast_egress_leases WHERE state='ACTIVE'"
                ).fetchone()[0]
                == 2
            )
        lab.until(identifier, "AWAITING_DIRECT_SOURCE")
        for _ in range(3):
            lab.observe()
            lab.controller.step(identifier)
            assert lab.state(identifier) == "AWAITING_DIRECT_SOURCE"
        with pytest.raises(BroadcastError, match="cancel_after"):
            lab.controller.cancel(identifier)
        lab.direct = node
        lab.until(identifier, "COMPLETED")
        with store.database.connect() as db:
            current = db.execute(
                "SELECT node_id,youtube_slot FROM broadcast_routes WHERE role='current'"
            ).fetchone()
            assert tuple(current) == (node, slot)
            assert tuple(db.execute("SELECT * FROM youtube_bindings").fetchone()) == binding
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM broadcast_egress_leases WHERE state='ACTIVE'"
                ).fetchone()[0]
                == 1
            )
            assert db.execute("SELECT ingress_node_id FROM broadcast_sources").fetchone()[0] == node
            history = json.loads(
                db.execute(
                    "SELECT durations_json FROM broadcast_switches WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            assert history["source_switch_gap_ms"] == 500
            assert (
                history["TARGET_MEDIA_READY"]
                < history["TARGET_CREDENTIAL_LEASED"]
                < history["OLD_EGRESS_DRAINING"]
            )


@pytest.mark.parametrize("state", ["PREPARING_TARGET", "TARGET_CREDENTIAL_LEASED", "CUTOVER_ARMED"])
def test_cancel_before_cutover_revokes_only_target_and_keeps_old(
    store: BroadcastStore, state: str
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    identifier = lab.controller.request(lab.output, lab.b, "cancel-request-test")
    lab.until(identifier, state)
    lab.controller.cancel(identifier)
    lab.until(identifier, "CANCELLED")
    with store.database.connect() as db:
        old = db.execute(
            "SELECT role,desired_enabled FROM broadcast_routes WHERE id=?", (lab.a,)
        ).fetchone()
        assert tuple(old) == ("current", 1)
        assert not db.execute(
            "SELECT 1 FROM broadcast_egress_leases WHERE node_id='relay-b' AND state='ACTIVE'"
        ).fetchone()


def test_controller_crash_target_error_idempotency_and_egress_only_move(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(
            pool.map(
                lambda _: lab.controller.request(lab.output, lab.b, "concurrent-switch-key"),
                range(2),
            )
        )
    assert ids[0] == ids[1]
    identifier = ids[0]
    lab.until(identifier, "TARGET_CREDENTIAL_LEASED")
    replacement = SwitchController(store, lab.media)
    replacement.step(identifier)
    assert lab.state(identifier) == "TARGET_CREDENTIAL_LEASED"  # Live owner lease fences it.
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_switches SET lease_until='2000-01-01' WHERE id=?", (identifier,)
        )
    lab.controller = replacement
    lab.failed = "relay-b"
    lab.until(identifier, "FAILED")
    with store.database.connect() as db:
        assert (
            db.execute("SELECT role FROM broadcast_routes WHERE id=?", (lab.a,)).fetchone()[0]
            == "current"
        )
    lab.failed = None
    identifier = lab.controller.request(
        lab.output, lab.c, "egress-only-switch-key", handoff_ingress=False
    )
    lab.until(identifier, "EGRESS_SWITCH_COMPLETED")
    with store.database.connect() as db:
        assert (
            db.execute("SELECT ingress_node_id FROM broadcast_sources").fetchone()[0] == "relay-a"
        )
        assert (
            db.execute("SELECT node_id FROM broadcast_routes WHERE role='current'").fetchone()[0]
            == "relay-c"
        )


def test_unmeasured_target_and_stale_stop_cannot_cut_over_or_free_slot(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    identifier = lab.controller.request(lab.output, lab.b, "unmeasured-switch-key")
    lab.controller.step(identifier)
    lab.controller.step(identifier)
    for _ in range(3):
        lab.controller.step(identifier)
    assert lab.state(identifier) == "PREPARING_TARGET"
    lab.until(identifier, "OLD_EGRESS_DRAINING")
    for _ in range(3):
        lab.controller.step(identifier)
    assert lab.state(identifier) == "OLD_EGRESS_DRAINING"
    with store.database.connect() as db:
        assert (
            db.execute("SELECT youtube_slot FROM broadcast_routes WHERE id=?", (lab.a,)).fetchone()[
                0
            ]
            == "PRIMARY"
        )

    with store.transaction() as db:
        # A stale stopped acknowledgment also waits through the partition deadline.
        db.execute(
            "UPDATE broadcast_media_observations SET publisher_running=0,"
            "runtime_secret_present=0,observed_at='2999-01-01',egress_generation=0 "
            "WHERE route_id=?",
            (lab.a,),
        )
    lab.controller.step(identifier)
    assert lab.state(identifier) == "OLD_EGRESS_DRAINING"
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_egress_leases SET expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE route_id=?",
            (lab.a,),
        )
    lab.controller.step(identifier)
    assert lab.state(identifier) == "OLD_CREDENTIAL_REVOKED"


def test_warm_reservations_and_combined_publisher_forward_bandwidth(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    store.admission = lab.media.admit
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET limits_json=json_set(limits_json,"
            "'$.max_publishers_per_node',1) WHERE node_id='relay-b'"
        )
    identifier = lab.controller.request(lab.output, lab.b, "reserve-warm-target")
    lab.until(identifier, "PREPARING_TARGET")
    other = store.create_output(
        lab.session, fixtures.manual_output("relay-b", "synthetic-other-key"), "warm-other-output"
    )
    with pytest.raises(BroadcastError, match="publisher_limit"):
        store.intent(other, True, "warm-over-capacity")
    lab.controller.cancel(identifier)
    lab.until(identifier, "CANCELLED")
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET limits_json=json_set(limits_json,"
            "'$.max_expected_egress_bps',6000000) WHERE node_id='relay-a'"
        )
    # A already sends one 6 Mbps publisher. A forwarded 6 Mbps copy would exceed
    # the same egress budget, even though the target publisher limit is available.
    with pytest.raises(BroadcastError, match="egress_limit"):
        store.intent(other, True, "source-forward-over-capacity")
