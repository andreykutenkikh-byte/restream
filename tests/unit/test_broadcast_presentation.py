from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
import test_broadcast_control as fixtures
from pydantic import SecretStr
from test_broadcast_media import enable_nodes
from test_broadcast_media import heartbeat as media_heartbeat
from test_broadcast_switching import SwitchLab

from app.broadcast.envelope import open_envelope
from app.broadcast.models import BroadcastError
from app.broadcast.presentation import BroadcastPresentation, Prepare, YouTubeSettings
from app.broadcast.store import BroadcastStore
from app.broadcast.switching import SwitchController
from app.db import utc_now

store = fixtures.store


def ui(store: BroadcastStore) -> BroadcastPresentation:  # noqa: F811
    media, _ = enable_nodes(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8'")
    return BroadcastPresentation(store, media, SwitchController(store, media))


def dump(store: BroadcastStore) -> str:  # noqa: F811
    with store.database.connect() as db:
        return "\n".join(db.iterdump())


@pytest.mark.parametrize("error", ["docker_install_failed", "remote_command_timeout", "private"])
def test_failed_bootstrap_is_distinct_from_legacy_and_recovers(
    store: BroadcastStore,
    error: str,
) -> None:  # noqa: F811
    view = ui(store)
    with store.transaction() as db:
        db.execute("DELETE FROM broadcast_media_nodes WHERE node_id='relay-a'")
        db.execute("UPDATE restream_nodes SET status='failed' WHERE id='relay-a'")
        db.execute(
            "INSERT INTO node_install_jobs(id,node_id,state,current_step,safe_error_code,"
            "safe_error_message,created_at,updated_at) VALUES "
            "('failed-install','relay-a','failed','docker_check',?,'private-output',?,?)",
            (error, utc_now(), utc_now()),
        )
    before = dump(store)
    node = next(n for n in view.state()["nodes"] if n["id"] == "relay-a")
    assert node["setup_error"] == "node_install_failed"
    assert node["installation_error"] == (None if error == "private" else error)
    assert "private" not in json.dumps(view.state())
    assert before == dump(store)
    for status, expected in [
        ("installing", "node_install_in_progress"),
        ("ready", "media_node_not_enabled"),
    ]:
        with store.transaction() as db:
            db.execute("UPDATE restream_nodes SET status=? WHERE id='relay-a'", (status,))
        node = next(n for n in view.state()["nodes"] if n["id"] == "relay-a")
        assert node["setup_error"] == expected
        assert node["installation_error"] is None


@pytest.mark.parametrize("status", ["connecting", "installing", "failed"])
def test_independent_ready_media_is_not_hidden_by_old_bootstrap_state(
    store: BroadcastStore,
    status: str,
) -> None:  # noqa: F811
    view = ui(store)
    with store.transaction() as db:
        db.execute("UPDATE restream_nodes SET status=? WHERE id='relay-a'", (status,))
    node = next(n for n in view.state()["nodes"] if n["id"] == "relay-a")
    assert node["setup_error"] is None
    assert node["installation_error"] is None
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET enabled=0 WHERE node_id='relay-a'")
    node = next(n for n in view.state()["nodes"] if n["id"] == "relay-a")
    assert node["setup_error"] == "media_node_not_enabled"


def test_explicit_prepare_atomic_idempotent_and_reveal_read_only(store: BroadcastStore) -> None:  # noqa: F811
    view = ui(store)
    data = Prepare(ingress_node_id="relay-a")
    before = dump(store)
    view.state()
    assert before == dump(store)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: view.prepare(data, "prepare-request-001"), range(3)))
    assert results == [results[0]] * 3
    result = results[0]
    state = view.state()
    assert len(state["sessions"]) == 1
    assert len(state["sessions"][0]["outputs"]) == 1
    output = state["sessions"][0]["outputs"][0]
    assert not output["desired_enabled"] and not output["credential_stored"]
    before = dump(store)
    first = view.connection(result["session_id"], None)
    assert first == view.connection(result["session_id"], None)
    assert dump(store) == before
    assert not first["listener_confirmed"]
    parsed = urlsplit(first["url"])
    assert parsed.scheme == "srt" and parsed.hostname == "8.8.8.8"
    params = parse_qs(parsed.query)
    assert params["pbkeylen"] == ["32"]
    assert params["passphrase"][0] in params["streamid"][0]
    assert params["passphrase"][0] not in json.dumps(state)
    assert params["passphrase"][0] not in dump(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET last_seen_at='2000-01-01T00:00:00+00:00'")
    before = dump(store)
    assert first == view.connection(result["session_id"], None)
    assert dump(store) == before
    assert view.state()["nodes"][0]["setup_error"] == "media_heartbeat_stale"
    with store.transaction() as db:
        db.execute("UPDATE restream_nodes SET status='revoked' WHERE id='relay-a'")
    with pytest.raises(BroadcastError, match="media_node_not_enabled"):
        view.connection(result["session_id"], None)


def test_prepare_rolls_back_on_capacity_and_private_or_v1_nodes(store: BroadcastStore) -> None:  # noqa: F811
    view = ui(store)
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET limits_json=?",
            (json.dumps({"max_expected_egress_bps": 128000}),),
        )
    with pytest.raises(BroadcastError, match="egress_limit"):
        view.prepare(Prepare(ingress_node_id="relay-a"), "prepare-resource-001")
    assert store.snapshot()["sessions"] == []
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='127.0.0.1'")
    with pytest.raises(BroadcastError, match="public_media_address"):
        view.prepare(Prepare(ingress_node_id="relay-a"), "prepare-resource-002")
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8',capabilities_json='[]'")
    with pytest.raises(BroadcastError, match="media_capability_missing"):
        view.prepare(Prepare(ingress_node_id="relay-a"), "prepare-resource-003")
    assert all(n["setup_error"] for n in view.state()["nodes"])


def test_draft_uses_existing_listener_plan_without_publisher_or_credential(
    store: BroadcastStore,
) -> None:  # noqa: F811
    media, keys = enable_nodes(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8'")
    view = BroadcastPresentation(store, media, SwitchController(store, media))
    prepared = view.prepare(Prepare(ingress_node_id="relay-a"), "prepare-listener-01")
    plan = open_envelope(keys["relay-a"], media.desired("relay-a"), "relay-a")
    connection = parse_qs(urlsplit(view.connection(prepared["session_id"], None)["url"]).query)
    assert list(plan["sources"].values()) == connection["passphrase"]
    assert len(plan["routes"]) == 1
    route = plan["routes"][0]
    assert not route["enabled"] and not route["media_enabled"]
    assert route["destination"] is None and route["egress_lease"] is None


def test_write_only_canonical_key_backup_only_live_change_and_stop_guard(
    store: BroadcastStore,
) -> None:  # noqa: F811
    view = ui(store)
    prepared = view.prepare(Prepare(ingress_node_id="relay-a"), "prepare-youtube-01")
    oid = prepared["output_id"]
    data = YouTubeSettings(
        primary_url="rtmps://a.rtmps.youtube.com/live2", stream_key=SecretStr("synthetic-ui-key")
    )
    view.save_youtube(oid, data, "youtube-settings-01")
    assert "synthetic-ui-key" not in json.dumps(view.state())
    assert "synthetic-ui-key" not in dump(store)
    source = view.connection(prepared["session_id"], None)
    store.intent(oid, True, "intent-youtube-001")
    view.media.desired("relay-a")
    with store.database.connect() as db:
        leases = [tuple(r) for r in db.execute("SELECT * FROM broadcast_egress_leases")]
        authority = [tuple(r) for r in db.execute("SELECT * FROM broadcast_egress_authority")]
    backup = YouTubeSettings(
        primary_url=data.primary_url, backup_url="rtmps://b.rtmps.youtube.com/live2?backup=1"
    )
    view.save_youtube(oid, backup, "youtube-settings-02")
    view.save_youtube(oid, backup, "youtube-settings-02")
    with store.database.connect() as db:
        assert leases == [tuple(r) for r in db.execute("SELECT * FROM broadcast_egress_leases")]
        assert authority == [
            tuple(r) for r in db.execute("SELECT * FROM broadcast_egress_authority")
        ]
        credentials = store.unseal(
            db.execute("SELECT credentials_encrypted FROM youtube_bindings").fetchone()[0]
        )
    assert credentials["stream_key"] == "synthetic-ui-key"
    assert source == view.connection(prepared["session_id"], None)
    with pytest.raises(BroadcastError, match="stop_before"):
        view.save_youtube(oid, data, "youtube-settings-03")
    store.intent(oid, False, "intent-youtube-002")
    assert not view.state()["sessions"][0]["outputs"][0]["stop_confirmed"]
    with pytest.raises(BroadcastError, match="waiting_for_publisher_stop"):
        view.save_youtube(oid, data, "youtube-settings-04")
    with store.transaction() as db:
        db.execute("UPDATE broadcast_egress_leases SET expires_at='2000-01-01T00:00:00+00:00'")
    view.save_youtube(oid, data, "youtube-settings-05")
    assert view.state()["sessions"][0]["outputs"][0]["stop_confirmed"]


def test_switch_preserves_credentials_and_current_connection_until_direct_proof(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8'")
    view = BroadcastPresentation(store, lab.media, lab.controller)
    lab.observe()
    original = view.connection(lab.session, None)
    with store.database.connect() as db:
        binding = tuple(db.execute("SELECT * FROM youtube_bindings").fetchone())
    switch = lab.controller.request(lab.output, lab.b, "ui-egress-switch-1", handoff_ingress=False)
    lab.until(switch, "EGRESS_SWITCH_COMPLETED")
    assert view.connection(lab.session, None) == original
    with store.database.connect() as db:
        assert tuple(db.execute("SELECT * FROM youtube_bindings").fetchone()) == binding
    target = view.connection(lab.session, lab.c)
    assert target["url"] != original["url"]
    switch = lab.controller.request(lab.output, lab.c, "ui-full-switch-01", handoff_ingress=True)
    lab.until(switch, "AWAITING_DIRECT_SOURCE")
    assert view.connection(lab.session, None) == original
    lab.direct = "relay-c"
    lab.until(switch, "COMPLETED")
    assert view.connection(lab.session, None) == target
    plan = open_envelope(lab.keys["relay-c"], lab.media.desired("relay-c"), "relay-c")
    assert plan["routes"][0]["destination"]["stream_key"] == "synthetic-key-one"


def test_stale_plan_is_unknown_and_target_resource_failure_is_visible(
    store: BroadcastStore,
) -> None:  # noqa: F811
    lab = SwitchLab(store)
    lab.observe()
    lab.observe()
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8',generation=generation+1")
        db.execute(
            "UPDATE broadcast_media_nodes SET limits_json=? WHERE node_id='relay-b'",
            (json.dumps({"max_expected_egress_bps": 128000}),),
        )
    view = BroadcastPresentation(store, lab.media, lab.controller)
    routes = view.state()["sessions"][0]["outputs"][0]["routes"]
    assert all(r["media_state"] == "UNKNOWN" for r in routes)
    assert all(r["egress_link"]["state"] == "UNKNOWN" for r in routes)
    assert next(r for r in routes if r["id"] == lab.b)["switch_error"] == "egress_limit"


def test_choose_stopped_sender_without_key_or_backup_preserves_source(
    store: BroadcastStore,
) -> None:  # noqa: F811
    view = ui(store)
    prepared = view.prepare(Prepare(ingress_node_id="relay-a"), "select-prepare-001")
    oid = prepared["output_id"]
    original = view.connection(prepared["session_id"], None)
    target = store.add_route(oid, "relay-b", "select-add-route-1")
    other = view.prepare(Prepare(ingress_node_id="relay-a"), "select-prepare-002")
    with store.database.connect() as db:
        untouched = tuple(
            db.execute(
                "SELECT * FROM broadcast_outputs WHERE id=?", (other["output_id"],)
            ).fetchone()
        )
        binding = tuple(
            db.execute("SELECT * FROM youtube_bindings WHERE output_id=?", (oid,)).fetchone()
        )
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: view.select_server(oid, target, "select-server-001"), range(3)))
    assert view.connection(prepared["session_id"], None) == original
    with store.database.connect() as db:
        assert (
            db.execute(
                "SELECT node_id FROM broadcast_routes WHERE output_id=? AND role='current'", (oid,)
            ).fetchone()[0]
            == "relay-b"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM broadcast_routes "
                "WHERE output_id=? AND youtube_slot='PRIMARY'",
                (oid,),
            ).fetchone()[0]
            == 1
        )
        assert not db.execute("SELECT 1 FROM broadcast_egress_leases").fetchone()
        assert not db.execute("SELECT 1 FROM broadcast_outputs WHERE desired_enabled=1").fetchone()
        assert (
            tuple(db.execute("SELECT * FROM youtube_bindings WHERE output_id=?", (oid,)).fetchone())
            == binding
        )
        assert (
            tuple(
                db.execute(
                    "SELECT * FROM broadcast_outputs WHERE id=?", (other["output_id"],)
                ).fetchone()
            )
            == untouched
        )
    before = dump(store)
    with pytest.raises(BroadcastError):
        view.select_server(other["output_id"], target, "select-foreign-001")
    assert dump(store) == before


def test_choose_sender_requires_stopped_publishers_and_ready_target(store: BroadcastStore) -> None:  # noqa: F811
    lab = SwitchLab(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8'")
    view = BroadcastPresentation(store, lab.media, lab.controller)
    lab.observe()
    with pytest.raises(BroadcastError, match="stop_before_selecting_server"):
        view.select_server(lab.output, lab.b, "select-live-001-test")
    store.intent(lab.output, False, "stop-select-001-test")
    with pytest.raises(BroadcastError, match="waiting_for_publisher_stop"):
        view.select_server(lab.output, lab.b, "select-wait-001-test")
    lab.observe()
    with store.transaction() as db:
        db.execute(
            "UPDATE broadcast_media_nodes SET last_seen_at='2000-01-01T00:00:00+00:00' "
            "WHERE node_id='relay-b'"
        )
    with pytest.raises(BroadcastError, match="media_heartbeat_stale"):
        view.select_server(lab.output, lab.b, "select-stale-001-test")
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET last_seen_at=?", (utc_now(),))
    lab.observe()
    view.select_server(lab.output, lab.b, "select-ready-001")
    store.intent(lab.output, True, "start-selected-001")
    plan = open_envelope(lab.keys["relay-b"], lab.media.desired("relay-b"), "relay-b")
    assert plan["routes"][0]["enabled"]
    assert plan["routes"][0]["youtube_slot"] == "PRIMARY"
    assert plan["routes"][0]["destination"]["stream_key"] == "synthetic-key-one"
    with store.database.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM broadcast_egress_leases WHERE state='ACTIVE'"
            ).fetchone()[0]
            == 1
        )


def test_rtmp_requires_advertised_listener_and_shares_source_credential(
    store: BroadcastStore,
) -> None:  # noqa: F811
    media, keys = enable_nodes(store)
    with store.transaction() as db:
        db.execute("UPDATE broadcast_media_nodes SET srt_host='8.8.8.8'")
    view = BroadcastPresentation(store, media, SwitchController(store, media))
    prepared = view.prepare(Prepare(ingress_node_id="relay-a"), "rtmp-prepare-001")
    sid = prepared["session_id"]
    srt = view.connection(sid, None)
    with pytest.raises(BroadcastError, match="rtmp_ingress_not_configured"):
        view.connection(sid, None, "rtmp")
    media.heartbeat("relay-a", media_heartbeat(keys["relay-a"], 2, rtmp_port=24002))
    before = dump(store)
    rtmp = view.connection(sid, None, "rtmp")
    assert rtmp == view.connection(sid, None, "rtmp")
    assert rtmp["server"].startswith("rtmp://8.8.8.8:24002/source/")
    assert (
        parse_qs(rtmp["stream_key"].split("?", 1)[1])["pass"]
        == parse_qs(urlsplit(srt["url"]).query)["passphrase"]
    )
    assert before == dump(store)
    assert rtmp["stream_key"] not in json.dumps(view.state())
    # Old agents remain valid and remove the advertised RTMP option on rollback.
    media.heartbeat("relay-a", media_heartbeat(keys["relay-a"], 3))
    with pytest.raises(BroadcastError, match="rtmp_ingress_not_configured"):
        view.connection(sid, None, "rtmp")
