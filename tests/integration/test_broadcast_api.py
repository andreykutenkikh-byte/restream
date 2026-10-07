from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_api_smoke import FakeMediaMTX, login

from app.broadcast.models import OutputCreate, SessionCreate
from app.core.config import Settings
from app.logging_config import OAuthAccessFilter
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE


def test_admin_write_csrf_origin_fetch_metadata_and_monitor_isolation(
    settings: Settings,
    admin_password: str,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app) as client:
        assert client.get("/api/broadcasts").status_code == 401
        csrf, _ = login(client, settings, admin_password)
        grant = app.state.relays.provision_node(display_name="Synthetic A", address="a.example")
        headers = {
            "X-CSRF-Token": csrf,
            "Origin": "http://testserver",
            "Idempotency-Key": "synthetic-api-session",
        }
        data = {"name": "Session", "ingress_node_id": grant.node_id}
        assert client.post("/api/broadcasts/sessions", json=data).status_code == 403
        assert (
            client.post(
                "/api/broadcasts/sessions",
                json=data,
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/broadcasts/sessions",
                json=data,
                headers={**headers, "Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 403
        )
        response = client.post("/api/broadcasts/sessions", json=data, headers=headers)
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        output = {
            "name": "Output",
            "node_id": grant.node_id,
            "mode": "manual",
            "primary_url": "rtmps://a.rtmps.youtube.com/live2",
            "stream_key": "synthetic-write-only-key",
        }
        url = f"/api/broadcasts/sessions/{sid}/outputs"
        assert client.post(url, json=output, headers=headers).status_code == 201
        assert "synthetic-write-only-key" not in client.get("/api/broadcasts").text
        ui_routes = [
            ("/api/broadcasts/prepare", {"ingress_node_id": grant.node_id}),
            (f"/api/broadcasts/sessions/{sid}/connection", {}),
            ("/api/broadcasts/outputs/unknown/connection", {"stream_key": "synthetic-key"}),
            ("/api/broadcasts/outputs/unknown/server", {"target_route_id": "unknown"}),
        ]
        assert "Моя трансляция" in client.get("/").text
        legacy = client.get("/legacy")
        assert "data-relay-video" in legacy.text
        assert legacy.headers["cache-control"] == "no-store"
        ui_state = client.get("/api/broadcasts/ui-state")
        assert ui_state.status_code == 200
        assert ui_state.headers["cache-control"] == "no-store"
        assert "synthetic-write-only-key" not in ui_state.text
        for path, payload in ui_routes:
            assert client.post(path, json=payload).status_code == 403
            assert (
                client.post(
                    path, json=payload, headers={**headers, "Origin": "https://evil.example"}
                ).status_code
                == 403
            )
        assert client.get("/broadcasts").headers["cache-control"] == "no-store"
        assert client.post(url, content="x" * 5000, headers=headers).status_code == 413
        pairing = app.state.moblin_hud.create_pairing("Monitor")
        monitor = app.state.moblin_hud.consume_pairing(pairing.pairing_token)
        client.cookies.clear()
        client.cookies.set(HUD_SESSION_COOKIE, monitor.session_token)
        assert client.get("/api/broadcasts").status_code == 401
        assert client.post(url, json=output, headers=headers).status_code == 401
        assert client.get("/api/broadcasts/ui-state").status_code == 401
        for path, payload in ui_routes:
            assert client.post(path, json=payload, headers=headers).status_code == 401
        assert "stream_key" not in json.dumps(app.state.broadcasts.snapshot())


def test_oauth_callback_access_log_keeps_no_code_or_state() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        "%s %s %s %s %s",
        (
            "127.0.0.1",
            "GET",
            "/api/broadcasts/youtube/callback?code=secret&state=opaque",
            "1.1",
            303,
        ),
        None,
    )
    OAuthAccessFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "opaque" not in record.getMessage()


def test_youtube_health_events_only_record_changes_without_provider_details(
    settings: Settings, admin_password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {"lifecycle_status": "live", "stream_status": "active", "health_status": "good"}
    monkeypatch.setattr(
        "app.broadcast.api.provider", lambda *_: SimpleNamespace(status=lambda *_: dict(status))
    )
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app) as client:
        csrf, _ = login(client, settings, admin_password)
        node = app.state.relays.provision_node(
            display_name="Event fixture", address="event.example"
        )
        store = app.state.broadcasts
        sid = store.create_session(
            SessionCreate(name="Event fixture", ingress_node_id=node.node_id), "event-session-key"
        )
        output = store.create_output(
            sid,
            OutputCreate(
                name="Event",
                node_id=node.node_id,
                primary_url="rtmps://a.rtmps.youtube.com/live2",
                stream_key=SecretStr("synthetic-event-secret"),
            ),
            "event-output-key",
        )
        with store.transaction() as db:
            db.execute("UPDATE youtube_bindings SET broadcast_id='event',stream_id='stream'")
        headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}
        url = f"/api/broadcasts/outputs/{output}/youtube-status"
        for _ in range(2):
            assert client.post(url, headers=headers).status_code == 200
        status["health_status"] = "bad"
        assert client.post(url, headers=headers).status_code == 200
        with store.database.connect() as db:
            events = db.execute(
                "SELECT event_type,safe_detail_json FROM broadcast_events "
                "WHERE event_type LIKE 'youtube.%'"
            ).fetchall()
        assert [e["event_type"] for e in events].count("youtube.stream_status") == 1
        assert [e["event_type"] for e in events].count("youtube.health_change") == 2
        assert all(e["safe_detail_json"] == "{}" for e in events)
