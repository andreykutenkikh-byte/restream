from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient
from test_api_smoke import FakeMediaMTX, login

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
        assert client.get("/broadcasts").headers["cache-control"] == "no-store"
        assert client.post(url, content="x" * 5000, headers=headers).status_code == 413
        pairing = app.state.moblin_hud.create_pairing("Monitor")
        monitor = app.state.moblin_hud.consume_pairing(pairing.pairing_token)
        client.cookies.clear()
        client.cookies.set(HUD_SESSION_COOKIE, monitor.session_token)
        assert client.get("/api/broadcasts").status_code == 401
        assert client.post(url, json=output, headers=headers).status_code == 401
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
