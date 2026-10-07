from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_broadcast_control as fixtures
from pydantic import ValidationError
from test_broadcast_control import manual_output, session
from test_broadcast_media import enable_nodes, heartbeat

from app.broadcast import diagnostics
from app.broadcast.media_control import MediaHeartbeat, Observation
from app.broadcast.media_diagnostics import DiagnosticCollector, DiagnosticEvent, classify
from app.broadcast.models import BroadcastError
from app.broadcast.store import BroadcastStore

store = fixtures.store


def test_raw_log_credentials_never_reach_disk_or_heartbeat(tmp_path: Path) -> None:
    collector = DiagnosticCollector(tmp_path)
    line = (
        "rtmp://user:SECRET_CANARY@host/live/STREAM_CANARY?pass=PASSWORD_CANARY: Input/output error"
    )
    assert classify(line) == ("io_error", None)
    assert classify("arbitrary SECRET_CANARY body") is None
    collector.callback("publisher", "route-safe")(*classify(line))
    assert classify("137 RTP packets lost [SECRET_CANARY]") == ("rtp_packets_lost", 137)
    pending = collector.snapshot()
    collector.acknowledge(pending[-1].sequence)
    assert collector.snapshot() == []
    collector.close()
    text = (tmp_path / "diagnostics.jsonl").read_text() + json.dumps(
        [e.model_dump() for e in pending]
    )
    assert "io_error" in text
    for secret in ("SECRET_CANARY", "STREAM_CANARY", "PASSWORD_CANARY", "rtmp://"):
        assert secret not in text
    with pytest.raises(ValidationError):
        DiagnosticEvent(sequence=1, at=time.time(), component="publisher", code="SECRET_CANARY")


def test_slow_disk_and_offline_controller_do_not_block_media(tmp_path: Path) -> None:
    collector = DiagnosticCollector(tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = collector.handler.handle

    def blocked(record):
        entered.set()
        gate.wait(3)
        return original(record)

    collector.handler.handle = blocked
    start = time.monotonic()
    for index in range(600):
        collector.emit("publisher", f"route-{index}", "io_error")
    assert time.monotonic() - start < 1
    assert entered.wait(1)
    assert len(collector.pending) <= 256 and collector.dropped > 0
    assert collector.disk_queue.qsize() <= 256
    assert len(collector.snapshot()) <= 64
    gate.set()
    collector.close()


def test_history_sampling_state_edges_and_counter_resets(
    store: BroadcastStore, monkeypatch
) -> None:  # noqa: F811
    output = store.create_output(session(store), manual_output(), "diagnostic-output")
    control, keys = enable_nodes(store)
    route = store.snapshot()["sessions"][0]["outputs"][0]["routes"][0]["id"]
    generation = control.desired("relay-a")["context"]["generation"]
    start = datetime.now(UTC)

    def send(seq, second, **values):
        monkeypatch.setattr(
            "app.broadcast.media_control.utc_now",
            lambda: (start + timedelta(seconds=second)).isoformat(),
        )
        data = heartbeat(keys["relay-a"], sequence=seq)
        data.plan_generation = generation
        data.diagnostics_version = 1
        data.observations = [
            Observation(
                route_id=route,
                source_kind="direct",
                source_identity="SOURCE_SECRET_CANARY",
                input_bytes=values.pop("input_bytes", second * 1000),
                video_frames=values.pop("video_frames", second * 30),
                **values,
            )
        ]
        return control.heartbeat("relay-a", data)

    send(2, 0)
    send(3, 2)
    send(4, 6)
    send(5, 7, safe_error_code="publisher_failed")
    with store.database.connect() as db:
        rows = db.execute(
            "SELECT payload_json FROM broadcast_quality_history ORDER BY id"
        ).fetchall()
        assert len(rows) == 3
        data = json.loads(rows[1][0])
        assert data["input_bitrate_bps"] == 8000 and data["input_fps"] == 30
        assert "SOURCE_SECRET_CANARY" not in json.dumps([r[0] for r in rows])
    send(6, 8, safe_error_code="source_lost")
    send(7, 9, input_epoch=123, input_bytes=1, video_frames=1)
    report = diagnostics.report(store, output, 1, start + timedelta(minutes=1))
    # Read at a future test clock; records() itself uses the explicit bounded interval.
    exported = list(
        diagnostics.records(
            store, output, start.isoformat(), (start + timedelta(minutes=1)).isoformat()
        )
    )
    assert sum(r["kind"] == "sample" for r in exported) == 5
    assert exported[0]["input_bitrate_bps"] is None and exported[0]["input_fps"] is None
    assert report["retention_days"] == 7


def test_diagnostics_replay_scope_and_retention(store: BroadcastStore, monkeypatch) -> None:  # noqa: F811
    output = store.create_output(session(store), manual_output(), "diagnostic-output")
    control, keys = enable_nodes(store)
    foreign = store.add_route(output, "relay-b", "diagnostic-foreign-route")
    event = DiagnosticEvent(sequence=1, at=time.time(), component="mediamtx", code="io_timeout")
    for seq in (2, 3):
        data = heartbeat(keys["relay-a"], sequence=seq, diagnostic_events=[event])
        control.heartbeat("relay-a", data)
    with store.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM broadcast_diagnostic_events").fetchone()[0] == 1
    event.route_id = foreign
    with pytest.raises(BroadcastError, match="diagnostic_route_scope"):
        control.heartbeat(
            "relay-a", heartbeat(keys["relay-a"], sequence=4, diagnostic_events=[event])
        )
    with store.database.connect() as db:
        assert (
            db.execute(
                "SELECT last_sequence FROM broadcast_media_nodes WHERE node_id='relay-a'"
            ).fetchone()[0]
            == 3
        )
        db.execute("UPDATE broadcast_diagnostic_events SET received_at='2000-01-01T00:00:00+00:00'")
    diagnostics.prune(store)
    with store.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM broadcast_diagnostic_events").fetchone()[0] == 0
    monkeypatch.setattr(diagnostics, "MAX_EVENTS", 2)
    event.route_id = None
    for seq in range(4, 10):
        event.sequence = seq
        control.heartbeat(
            "relay-a", heartbeat(keys["relay-a"], sequence=seq, diagnostic_events=[event])
        )
    diagnostics.prune(store)
    with store.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM broadcast_diagnostic_events").fetchone()[0] == 2
    # Old agents omit new fields and still participate in media control.
    assert (
        MediaHeartbeat.model_validate(
            heartbeat(keys["relay-a"], sequence=11).model_dump()
        ).diagnostics_version
        == 0
    )
