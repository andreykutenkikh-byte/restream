from __future__ import annotations

from fastapi.testclient import TestClient
from test_api_smoke import FakeMediaMTX, login

from app.broadcast.models import SessionCreate
from app.core.config import Settings
from app.main import create_app


def test_pairing_is_admin_only_and_ingestion_is_source_scoped(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app) as admin:
        csrf, _ = login(admin, settings, admin_password)
        node = app.state.relays.provision_node(display_name="Test", address="network.example")
        app.state.broadcasts.create_session(
            SessionCreate(name="Test", ingress_node_id=node.node_id), "synthetic-network-session"
        )
        source = app.state.broadcasts.snapshot()["sessions"][0]["source_id"]
        path = f"/api/broadcasts/sources/{source}/obs-monitor"
        assert admin.post(path, json={}).status_code == 403
        headers = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
        paired = admin.post(path, json={}, headers=headers)
        assert paired.status_code == 200
        token = paired.json()["token"]
        payload = {
            "sequence": 1,
            "boot_id": "synthetic-output-boot",
            "active": True,
            "reconnecting": False,
            "duration_ms": 1000,
            "total_frames": 60,
            "dropped_frames": 1,
            "bytes_sent": 1000,
        }
        assert admin.post("/obs-monitor/v1/sample", json=payload).status_code == 401
        bearer = {"Authorization": "Bearer " + token}
        assert admin.post("/obs-monitor/v1/sample", json=payload, headers=bearer).status_code == 200
        assert token not in admin.get("/api/broadcasts/ui-state").text
        assert (
            admin.post("/obs-monitor/v1/sample", content="x" * 4097, headers=bearer).status_code
            == 413
        )
        assert admin.post(path + "/revoke", json={}, headers=headers).status_code == 200
        assert admin.post("/obs-monitor/v1/sample", json=payload, headers=bearer).status_code == 401
