import json

from fastapi.testclient import TestClient
from test_api_smoke import FakeMediaMTX, login

from app.broadcast.models import OutputCreate, SessionCreate
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE


def test_admin_report_export_and_hud_isolation(settings, admin_password):
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app) as client:
        assert client.get("/api/broadcasts/outputs/unknown/diagnostics").status_code == 401
        login(client, settings, admin_password)
        grant = app.state.relays.provision_node(display_name="Synthetic", address="a.example")
        store = app.state.broadcasts
        sid = store.create_session(
            SessionCreate(name="Diagnostic", ingress_node_id=grant.node_id), "diagnostic-session"
        )
        oid = store.create_output(
            sid,
            OutputCreate(
                mode="manual",
                primary_url="rtmps://a.rtmps.youtube.com/live2",
                name="Diagnostic",
                node_id=grant.node_id,
                stream_key="STREAM_SECRET_CANARY",
            ),
            "diagnostic-output",
        )
        root = f"/api/broadcasts/outputs/{oid}/diagnostics"
        report = client.get(root)
        assert report.status_code == 200 and report.headers["cache-control"] == "no-store"
        exported = client.get(root + "/export")
        assert (
            exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        )
        rows = [json.loads(line) for line in exported.text.splitlines()]
        assert rows[0]["kind"] == "metadata"
        assert "STREAM_SECRET_CANARY" not in exported.text
        assert client.get(root, params={"hours": 169}).status_code == 422
        assert client.get(root, params={"until": "2026-01-01T12:00:00"}).status_code == 422
        page = client.get(f"/broadcasts/outputs/{oid}/diagnostics")
        assert page.status_code == 200 and "Скачать полный отчёт" in page.text
        pair = app.state.moblin_hud.create_pairing("Monitor")
        monitor = app.state.moblin_hud.consume_pairing(pair.pairing_token)
        client.cookies.clear()
        client.cookies.set(HUD_SESSION_COOKIE, monitor.session_token)
        assert client.get(root).status_code == 401
        assert client.get(root + "/export").status_code == 401
        assert client.get(f"/broadcasts/outputs/{oid}/diagnostics").status_code == 401
