from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
import test_broadcast_control as fixtures
from test_broadcast_control import manual_output, session
from test_broadcast_media import enable_nodes

from app.broadcast.media_control import MediaHeartbeat
from app.broadcast.models import CAPABILITIES, BroadcastError
from app.broadcast.network_control import measured
from app.broadcast.network_models import (
    NETWORK_CAPABILITY,
    PROBE_CAPABILITY,
    IngressMeasurement,
    LinkMeasurement,
    ObsMeasurement,
    ProbeProgress,
)
from app.broadcast.network_probe import ProbeServer, run_probe
from app.broadcast.network_runtime import TransportSampler
from app.broadcast.store import BroadcastStore

store = fixtures.store


def setup(store: BroadcastStore) -> tuple[object, str, str]:
    media, _ = enable_nodes(store)
    sid = session(store)
    output = store.create_output(sid, manual_output(), "synthetic-network-output")
    route = store.add_route(output, "relay-b", "synthetic-network-route")
    source = store.snapshot()["sessions"][0]["source_id"]
    return media, route, source


def heartbeat(**extra: object) -> MediaHeartbeat:
    return MediaHeartbeat(
        boot_id="synthetic-network-boot",
        sequence=2,
        public_key="x" * 44,
        plan_generation=0,
        network_version=1,
        capabilities=sorted(CAPABILITIES | {NETWORK_CAPABILITY}),
        **extra,
    )


def test_sampler_counts_only_compatible_windows_and_preserves_unknowns() -> None:
    sampler = TransportSampler()
    plan = {"sources": {"source": "private"}, "exports": [{"path": "export", "route_id": "r"}]}
    source = {
        "id": "input",
        "path": "source/source/direct",
        "state": "publish",
        "bytesReceived": 1000,
    }
    link = {
        "id": "output",
        "path": "export",
        "state": "read",
        "msRTT": 200,
        "bytesSentUnique": 1000,
        "bytesSent": 1200,
        "packetsRetrans": 3,
        "packetsSendDrop": 0,
    }
    links, inputs = sampler.sample(plan, [link], [source], 100)
    assert inputs[0].bitrate_bps is None and links[0].sender_dropped_packets is None
    links, inputs = sampler.sample(
        plan,
        [{**link, "bytesSentUnique": 2000, "bytesSent": 2500, "packetsRetrans": 5}],
        [{**source, "bytesReceived": 3000}],
        102,
    )
    assert inputs[0].bitrate_bps == 8000 and inputs[0].missing_packets is None
    assert links[0].media_bitrate_bps == 4000 and links[0].wire_bitrate_bps == 5200
    assert links[0].retransmitted_packets == 2 and links[0].sender_dropped_packets == 0
    _, reset = sampler.sample(plan, [], [{**source, "id": "new-input"}], 104)
    assert reset[0].bitrate_bps is None
    _, old = sampler.sample(plan, [], [{**source, "id": "new-input"}], 200)
    assert old[0].bitrate_bps is None
    assert sampler.sample(plan, [], [{**source, "id": ""}], 202)[1] == []


def test_scope_freshness_out_of_order_and_rtmp_unknown_loss(store: BroadcastStore) -> None:  # noqa: F811
    media, route, source = setup(store)
    now = time.time()
    data = heartbeat(
        link_measurements=[
            LinkMeasurement(route_id=route, sampled_at=now, kind="tcp", reachable=True, rtt_ms=25)
        ],
        ingress_measurements=[
            IngressMeasurement(
                source_id=source,
                sampled_at=now,
                protocol="rtmp",
                connection_epoch="input",
                bitrate_bps=10_000_000,
                dropped_packets=99,
                received_packets=123,
            )
        ],
    )
    with store.transaction() as db:
        media.network.record(db, "relay-a", data)
    with store.transaction() as db, pytest.raises(BroadcastError):
        media.network.record(db, "relay-b", data)
    older = data.model_copy(
        update={
            "link_measurements": [
                data.link_measurements[0].model_copy(update={"sampled_at": now - 3, "rtt_ms": 999})
            ],
            "ingress_measurements": [],
        }
    )
    with store.transaction() as db:
        media.network.record(db, "relay-a", older)
    with store.database.connect() as db:
        view = media.network.route_view(db, route, False)
        assert view["tcp"]["rtt_ms"] == 25
        input_view = media.network.source_view(db, source)["ingress"]
        assert input_view["dropped_packets"] is None and input_view["received_packets"] is None
        assert measured(None, 10) == {"state": "UNKNOWN"}
        assert media.network.route_view(db, route, True)["srt"]["state"] == "LOCAL"
    with store.transaction() as db:
        db.execute("UPDATE broadcast_network_links SET observed_at='2000-01-01T00:00:00+00:00'")
    with store.database.connect() as db:
        assert media.network.route_view(db, route, False)["tcp"]["state"] == "STALE"


def test_capacity_idle_admission_and_terminal_token_erasure(store: BroadcastStore) -> None:  # noqa: F811
    media, route, _ = setup(store)
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET probe_port=24005,capabilities_json=?",
            (json.dumps(sorted(CAPABILITIES | {NETWORK_CAPABILITY, PROBE_CAPABILITY})),),
        )
    job = media.network.start_probe(route, "synthetic-probe-idempotent")
    assert job == media.network.start_probe(route, "synthetic-probe-idempotent")
    with pytest.raises(BroadcastError, match="network_probe_busy"):
        media.network.start_probe(route, "synthetic-probe-another")
    with store.transaction() as db:
        plan = media.network.plan(db, "relay-a")
        token = plan["network_probes"][0]["token"]
        assert token not in "\n".join(db.iterdump())
        media.network.record(
            db,
            "relay-b",
            heartbeat(probe_progress=[ProbeProgress(job_id=job, role="receiver", state="READY")]),
        )
        media.network.record(
            db,
            "relay-a",
            heartbeat(
                probe_progress=[
                    ProbeProgress(
                        job_id=job,
                        role="sender",
                        state="COMPLETED",
                        bytes_received=1000,
                        elapsed_ms=1000,
                        throughput_bps=8000,
                    )
                ]
            ),
        )
    with store.database.connect() as db:
        row = db.execute("SELECT * FROM broadcast_network_probe_jobs WHERE id=?", (job,)).fetchone()
        assert row["state"] == "COMPLETED" and row["encrypted"] == ""
        assert token not in json.dumps(media.network.route_view(db, route, False))
    with store.transaction() as db:
        db.execute("UPDATE broadcast_outputs SET desired_enabled=1")
    with pytest.raises(BroadcastError, match="network_probe_media_active"):
        media.network.start_probe(route, "synthetic-probe-active")
    with store.transaction() as db:
        db.execute("UPDATE broadcast_outputs SET desired_enabled=0")
        db.execute("UPDATE broadcast_media_nodes SET last_seen_at='2000-01-01T00:00:00+00:00'")
        assert not media.network.idle(db, "relay-a", "relay-b")


def test_obs_pairing_sequence_reset_and_frame_loss_are_not_packet_loss(
    store: BroadcastStore,
) -> None:  # noqa: F811
    media, _, source = setup(store)
    token = media.network.pair_obs(source)
    data = ObsMeasurement(
        sequence=1,
        boot_id="obs-synthetic-boot",
        active=True,
        reconnecting=False,
        duration_ms=1000,
        total_frames=60,
        dropped_frames=0,
        bytes_sent=1000,
    )
    media.network.record_obs(token, data)
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_obs_samples SET observed_at=?",
            ((datetime.now(UTC) - timedelta(seconds=2)).isoformat(),),
        )
    media.network.record_obs(
        token,
        data.model_copy(
            update={
                "sequence": 2,
                "duration_ms": 3000,
                "total_frames": 180,
                "dropped_frames": 3,
                "bytes_sent": 3000,
            }
        ),
    )
    with store.database.connect() as db:
        view = media.network.source_view(db, source)["obs"]
        assert view["dropped_frames_delta"] == 3 and view["dropped_frames_percent"] == 2.5
        assert "loss_percent" not in view and "boot_id" not in view and "sequence" not in view
        assert token not in "\n".join(db.iterdump())
    with pytest.raises(BroadcastError, match="stale_sample"):
        media.network.record_obs(token, data)
    media.network.revoke_obs(source)
    with pytest.raises(BroadcastError, match="authentication_failed"):
        media.network.record_obs(token, data)


def test_authenticated_bounded_probe_and_starting_media_aborts() -> None:
    server = ProbeServer(0, lambda: True, host="127.0.0.1")
    job = {
        "id": "synthetic-probe",
        "role": "receiver",
        "host": "127.0.0.1",
        "port": server.port,
        "token": "synthetic-probe-token",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    try:
        server.accept([job])
        wrong = run_probe({**job, "token": "wrong"}, lambda: True, test_loopback=True)
        assert wrong.state == "FAILED" and wrong.safe_error == "authentication_failed"
        result = run_probe(job, lambda: True, test_loopback=True)
        assert result.state == "COMPLETED" and 0 < result.bytes_received <= 24_000_000
        assert 2500 < result.elapsed_ms < 5000
        assert run_probe(job, lambda: False, test_loopback=True).safe_error == "media_active"
        started = time.monotonic()
        abort = run_probe(job, lambda: time.monotonic() - started < 0.5, test_loopback=True)
        assert abort.state == "FAILED" and abort.safe_error == "media_active"
    finally:
        server.close()


def test_network_plan_is_stable_across_a_missed_candidate_heartbeat(store: BroadcastStore) -> None:  # noqa: F811
    media, _, _ = setup(store)
    with store.transaction() as db:
        before = media.network.plan(db, "relay-a")["monitor_targets"]
        db.execute(
            "UPDATE broadcast_media_nodes SET last_seen_at=? WHERE node_id='relay-b'",
            ("2000-01-01T00:00:00+00:00",),
        )
        assert before == media.network.plan(db, "relay-a")["monitor_targets"]
