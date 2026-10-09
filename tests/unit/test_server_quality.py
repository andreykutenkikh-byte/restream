from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import test_broadcast_control as fixtures
from test_broadcast_control import manual_output, session
from test_broadcast_media import enable_nodes

from app.broadcast.read_model import snapshot
from app.broadcast.server_quality import QualityReader, analyse, assessment, context, recommend
from app.broadcast.store import BroadcastStore

store = fixtures.store
START = datetime(2026, 10, 9, 0, tzinfo=UTC).timestamp()


def samples(duration: int = 1860, **changes: Any) -> list[dict[str, Any]]:
    return [
        dict(
            at=START + i,
            assessment_context="scope",
            boot="boot",
            source_epoch="source",
            role="current",
            desired_enabled=True,
            source_kind="forwarded",
            input_epoch=1,
            egress_generation=1,
            publisher_epoch=1,
            publisher_frames=i * 30,
            publisher_running=True,
            publisher_connected=True,
            publisher_progress_age_ms=100,
            publisher_retries=0,
            selector_queue_packets=0,
            video_frames=i * 30,
            video_fps=30,
            ingress_network={"state": "FRESH", "bitrate_bps": 10_000_000},
        )
        | changes
        for i in range(0, duration + 1, 5)
    ]


def analysis(data: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return analyse(
        data,
        scope="scope",
        boot="boot",
        ingress=extra.pop("ingress", []),
        legacy_generations=extra.pop("legacy_generations", set()),
        transitions=extra.pop("transitions", []),
        events=extra.pop("events", []),
    )


def grade(
    data: list[dict[str, Any]], network: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    h = analysis(data, **extra)
    return assessment(h, network or {}, ready=True, required_bps=10_000_000, now=START + 1860)


def test_only_actual_stream_observation_earns_stable_grade() -> None:
    assert grade(samples())["state"] == "STABLE"
    assert grade(samples(300))["state"] == "UNTESTED"
    assert grade(samples(desired_enabled=False))["observed_seconds"] == 0
    assert grade(samples(role="standby"))["state"] == "UNTESTED"


@pytest.mark.parametrize(
    "changes",
    [
        {"selector_queue_packets": None},
        {"publisher_progress_age_ms": None},
        {"ingress_network": {"state": "STALE", "bitrate_bps": 10_000_000}},
        {"ingress_network": {"state": "FRESH", "bitrate_bps": 0}},
        {"assessment_context": "another-source-or-key"},
    ],
)
def test_missing_stale_or_foreign_evidence_never_becomes_healthy(changes: dict[str, Any]) -> None:
    q = grade(samples(**changes))
    assert q["observed_seconds"] == 0 and q["state"] == "UNTESTED"


def test_gaps_boots_epochs_and_plan_updates_do_not_manufacture_outages() -> None:
    data = samples(50)
    for i, r in enumerate(data):
        r["at"] += i * 20
        r["selector_queue_packets"] = 1024
    q = grade(data)
    assert q["incidents"] == 0 and q["observed_seconds"] == 0
    for key in ("boot", "source_epoch", "input_epoch", "egress_generation"):
        data = samples(50)
        for i, r in enumerate(data):
            r[key] = i
        assert grade(data)["incidents"] == 0
    data = samples()
    for i, r in enumerate(data):
        r["plan_generation"] = i
    assert grade(data)["state"] == "STABLE"  # whole-node desired plan is not a media outage


def test_sustained_backlog_and_frame_stall_are_problems_not_packet_loss_claims() -> None:
    q = grade(samples(50, selector_queue_packets=1024))
    assert q["state"] == "PROBLEM" and q["incidents"] == 1
    assert q["backlog_seconds"] == 50 and "sustained_backlog" in q["reasons"]
    assert "loss_percent" not in q
    q = grade(samples(50, publisher_frames=0, publisher_progress_age_ms=31_000))
    assert q["state"] == "PROBLEM" and "sender_stalled" in q["reasons"]
    data = samples(50)
    data[3]["selector_queue_packets"] = 1024
    assert grade(data)["incidents"] == 0  # a spike is risk, not a sustained episode
    assert grade(samples(50, selector_queue_bytes=14 * 1024 * 1024))["state"] == "PROBLEM"


def test_counter_reset_is_unknown_and_one_real_restart_is_risk() -> None:
    data = samples(50)
    data[5]["publisher_frames"] = 0
    q = grade(data)
    assert q["unknown_seconds"] == 5 and q["stalled_seconds"] == 0
    for r in data[5:]:
        r["publisher_epoch"] = 2
    assert grade(data)["state"] == "RISK"


def test_slow_sender_is_distinct_from_slow_source() -> None:
    data = samples(50)
    for i, r in enumerate(data):
        r["publisher_frames"] = i * 10
    assert "sender_slow" in grade(data)["reasons"]
    for i, r in enumerate(data):
        r["video_frames"] = i * 10
    assert "sender_slow" not in grade(data)["reasons"]


def test_intentional_source_stop_and_switch_shutdown_are_excluded() -> None:
    data = samples(50, selector_queue_packets=1024, publisher_frames=0)
    events = [
        {"at": START + 20, "code": "invalid_media"},
        {"at": START + 25, "code": "invalid_media"},
    ]
    q = grade(data, transitions=[(START, START + 55)], events=events)
    assert q["incidents"] == q["media_warnings"] == 0
    assert q["transition_seconds"] == 50
    for r in data:
        r["ingress_network"] = {"state": "FRESH", "bitrate_bps": 0}
    q = grade(data, events=events)
    assert q["incidents"] == q["media_warnings"] == 0


def test_route_scoped_warnings_require_confirmed_input_and_steady_coverage() -> None:
    events = [
        {"at": START + 20, "code": "invalid_media"},
        {"at": START + 25, "code": "invalid_media"},
        {"at": START - 30, "code": "invalid_media"},
        {"at": START + 10, "code": "control_unreachable"},
    ]
    q = grade(samples(50), events=events)
    assert q["state"] == "RISK" and q["media_warnings"] == 2


def test_srt_recovery_is_not_final_loss_and_windows_are_not_double_counted() -> None:
    data = samples()
    link = dict(
        state="FRESH",
        observed_at=datetime.fromtimestamp(START + 20, UTC).isoformat(),
        window_seconds=2,
        retransmitted_packets=1000,
        sender_dropped_packets=0,
        rtt_ms=250,
        media_bitrate_bps=10_000_000,
        wire_bitrate_bps=20_000_000,
    )
    for r in data[4:7]:
        r["network"] = {"srt": link}
    q = grade(data)
    assert q["state"] == "STABLE" and q["sender_drop_windows"] == 0
    assert q["overhead_percent"] == 100
    link["sender_dropped_packets"] = 2
    q = grade(data)
    assert q["state"] == "RISK" and q["sender_drop_windows"] == 1
    assert grade(data, transitions=[(START + 19, START + 21)])["sender_drop_windows"] == 0


def test_old_history_requires_independent_ingress_and_destination_proof() -> None:
    data = samples()
    ingress = samples(
        source_kind="direct", input_bytes=0, input_age_ms=100, publisher_connected=False
    )
    for i, r in enumerate(ingress):
        r["input_bytes"] = i * 1_000_000
    for r in data:
        r.pop("assessment_context")
        r.pop("ingress_network")
    assert grade(data, ingress=ingress)["state"] == "UNTESTED"
    q = grade(data, ingress=ingress, legacy_generations={1})
    assert q["state"] == "HISTORY" and "retest_after_update" in q["reasons"]
    # Broken co-located egress cannot make healthy OBS input disappear.
    assert q["historical_seconds"] >= 1800
    for r in ingress:
        r["input_bytes"] = 0
    assert grade(data, ingress=ingress, legacy_generations={1})["observed_seconds"] == 0


def test_new_control_observation_can_replace_provisional_old_incidents() -> None:
    current = analysis(samples())
    old = {
        **current,
        "requires_retest": True,
        "incidents": 2,
        "backlog_seconds": 70,
        "current": current,
        "truncated": False,
    }
    q = assessment(old, {}, ready=True, required_bps=10_000_000, now=START + 1860)
    assert q["state"] == "STABLE" and q["previous_incidents"] == 2
    assert old["incidents"] == 2  # no mutation of cached history
    bad = analysis(samples(50, selector_queue_packets=1024))
    old["current"] = bad
    assert (
        assessment(old, {}, ready=True, required_bps=10_000_000, now=START + 1860)["state"]
        == "PROBLEM"
    )


def test_precheck_headroom_freshness_failures_and_local_unknown() -> None:
    data = samples(150, desired_enabled=False)
    for r in data:
        r["network"] = {
            "tcp": dict(
                state="FRESH",
                reachable=True,
                rtt_ms=20,
                observed_at=datetime.fromtimestamp(r["at"], UTC).isoformat(),
            )
        }
    network = {
        "tcp": {"state": "FRESH", "reachable": True},
        "probe": dict(
            state="COMPLETED",
            throughput_bps=64_000_000,
            finished_at=datetime.fromtimestamp(START + 150, UTC).isoformat(),
        ),
    }
    assert grade(data, network)["state"] == "PRECHECK"
    network["probe"]["throughput_bps"] = 12_000_000
    assert grade(data, network)["state"] == "RISK"
    network["probe"]["state"] = "FAILED"
    assert grade(data, network)["state"] == "UNTESTED"
    network["probe"].update(
        state="COMPLETED", throughput_bps=64_000_000, finished_at="2000-01-01T00:00:00+00:00"
    )
    assert grade(data, network)["probe_headroom_ratio"] is None
    q = grade(samples(), {"local": True})
    assert q["tcp_p50_ms"] is None and q["probe_headroom_ratio"] is None
    assert "capacity_unknown" not in q["reasons"]
    for r in data[:10]:
        r["network"]["tcp"]["reachable"] = False
    assert "tcp_unstable" in grade(data, network)["reasons"]


def test_recommendation_never_selects_an_unavailable_or_risky_sender() -> None:
    good, bad = grade(samples()), grade(samples(50, selector_queue_packets=1024))
    routes = [
        dict(id="bad", role="current", quality=bad),
        dict(id="good", role="standby", quality=good),
    ]
    before = copy.deepcopy(routes)
    assert recommend(routes) == {"route_ids": ["good"], "basis": "STABLE", "automatic": False}
    assert routes == before
    routes[1]["admission_error"] = "resource_limit"
    assert recommend(routes)["route_ids"] == []


def test_capped_speed_probe_does_not_prove_insufficient_channel_capacity() -> None:
    h = analysis(samples())
    probe = {
        "probe": dict(
            state="COMPLETED",
            throughput_bps=64_000_000,
            finished_at=datetime.fromtimestamp(START + 150, UTC).isoformat(),
        )
    }
    q = assessment(h, probe, ready=True, required_bps=50_000_000, now=START + 1860)
    assert q["state"] == "STABLE" and q["probe_capped"]
    assert "probe_ceiling" in q["reasons"] and "little_capacity_headroom" not in q["reasons"]


def test_reader_is_restart_persistent_scoped_bounded_and_read_only(
    store: BroadcastStore, monkeypatch: Any
) -> None:  # noqa: F811
    enable_nodes(store)
    sid = session(store)
    output = store.create_output(sid, manual_output(), "synthetic-quality-output")
    route = store.snapshot()["sessions"][0]["outputs"][0]["routes"][0]["id"]
    now = datetime.now(UTC)
    with store.transaction() as db:
        scope = context(db, route)
        boot_id = db.execute(
            "SELECT boot_id FROM broadcast_media_nodes WHERE node_id='relay-a'"
        ).fetchone()[0]
        boot = hashlib.sha256(str(boot_id or "").encode()).hexdigest()[:24]
        for r in samples():
            r["assessment_context"], r["boot"] = scope, boot
            stamp = (now - timedelta(seconds=1860 - (r.pop("at") - START))).isoformat()
            db.execute(
                "INSERT INTO broadcast_quality_history(output_id,route_id,node_id,"
                "observed_at,signature,payload_json) VALUES(?,?,?,?,'synthetic',?)",
                (output, route, "relay-a", stamp, json.dumps(r)),
            )
    one = snapshot(store)["sessions"][0]["outputs"][0]["routes"][0]["quality"]
    assert one["state"] == "STABLE"
    store.quality = QualityReader()
    assert snapshot(store)["sessions"][0]["outputs"][0]["routes"][0]["quality"] == one
    with store.database.connect() as db:
        before = list(db.execute("SELECT role,desired_enabled FROM broadcast_routes"))
        rows = db.execute("SELECT COUNT(*) FROM broadcast_quality_history").fetchone()[0]
        db.execute(
            "UPDATE youtube_bindings SET credential_fingerprint='different' WHERE output_id=?",
            (output,),
        )
    changed = snapshot(store)["sessions"][0]["outputs"][0]["routes"][0]["quality"]
    assert changed["state"] == "UNTESTED"
    assert "different" not in json.dumps(changed)
    monkeypatch.setattr("app.broadcast.server_quality.MAX_ROWS", 10)
    store.quality = QualityReader()
    limited = snapshot(store)["sessions"][0]["outputs"][0]["routes"][0]["quality"]
    assert limited["truncated"] and "history_limited" in limited["reasons"]
    with store.database.connect() as db:
        assert list(db.execute("SELECT role,desired_enabled FROM broadcast_routes")) == before
        assert db.execute("SELECT COUNT(*) FROM broadcast_quality_history").fetchone()[0] == rows
