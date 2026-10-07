from __future__ import annotations

import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_api_smoke import FakeMediaMTX, login

from app.broadcast.envelope import public_key
from app.broadcast.media_control import MediaHeartbeat, MediaNodeEnable
from app.broadcast.models import CAPABILITIES, OutputCreate, ResourceLimits, SessionCreate
from app.broadcast.switch_api import COOKIE
from app.core.config import Settings
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE


def test_operator_pairing_scoping_csrf_monitor_isolation_revoke_and_expiry(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app, base_url="https://testserver") as client:
        csrf, _ = login(client, settings, admin_password)
        store, media = app.state.broadcasts, app.state.broadcast_media
        nodes = []
        for i in range(2):
            node = app.state.relays.provision_node(
                display_name=f"Synthetic {i}", address=f"relay-{i}.example"
            ).node_id
            key = X25519PrivateKey.generate()
            media.enable(
                node,
                MediaNodeEnable(
                    public_key=public_key(key),
                    srt_host="127.0.0.1",
                    srt_port=19000 + i,
                    limits=ResourceLimits(),
                ),
            )
            media.heartbeat(
                node,
                MediaHeartbeat(
                    boot_id="operator-synthetic-boot",
                    public_key=public_key(key),
                    capabilities=sorted(CAPABILITIES),
                    sequence=1,
                    plan_generation=0,
                ),
            )
            nodes.append(node)
        sid = store.create_session(
            SessionCreate(name="Allowed session", ingress_node_id=nodes[0]), "allowed-session-key"
        )
        other = store.create_session(
            SessionCreate(name="Unrelated session", ingress_node_id=nodes[0]), "other-session-key"
        )
        outputs = []
        for i, session in enumerate((sid, other)):
            outputs.append(
                store.create_output(
                    session,
                    OutputCreate(
                        name="Synthetic output",
                        node_id=nodes[0],
                        primary_url="rtmps://a.rtmps.youtube.com/live2",
                        backup_url="rtmps://b.rtmps.youtube.com/live2?backup=1",
                        stream_key=SecretStr(f"synthetic-write-only-{i}"),
                    ),
                    f"synthetic-output-{i}",
                )
            )
        target = store.add_route(outputs[0], nodes[1], "operator-backup-route")
        store.intent(outputs[0], True, "operator-start-output")
        admin = {"X-CSRF-Token": csrf, "Origin": "https://testserver"}
        endpoint = f"/api/broadcasts/sessions/{sid}/operators"
        assert (
            client.post(
                endpoint, json={"label": "Phone", "allow_switching": False}, headers=admin
            ).status_code
            == 422
        )
        pairing = client.post(
            endpoint, json={"label": "Phone", "allow_switching": True}, headers=admin
        ).json()
        token = pairing["pairing_token"]
        monitor_pair = app.state.moblin_hud.create_pairing("Monitor")
        monitor = app.state.moblin_hud.consume_pairing(monitor_pair.pairing_token)
        client.cookies.clear()
        client.cookies.set(HUD_SESSION_COOKIE, monitor.session_token)
        assert client.get("/moblin-hud/api/broadcasts").json()["scope"] == "stream_monitor"
        assert client.get("/stream-operator/api/state").status_code == 401
        assert (
            client.post(
                f"/stream-operator/api/outputs/{outputs[0]}/switch",
                json={"target_route_id": target},
                headers=admin,
            ).status_code
            == 401
        )
        pair_url = "/stream-operator/api/pair"
        assert (
            client.post(
                pair_url, json={"token": token}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        response = client.post(
            pair_url, json={"token": token}, headers={"Origin": "https://testserver"}
        )
        assert response.status_code == 204
        cookie = response.headers["set-cookie"].lower()
        assert (
            "httponly" in cookie
            and "secure" in cookie
            and "samesite=strict" in cookie
            and "domain=" not in cookie
        )
        assert (
            client.post(
                pair_url, json={"token": token}, headers={"Origin": "https://testserver"}
            ).status_code
            == 401
        )
        state = client.get("/stream-operator/api/state")
        assert state.headers["cache-control"] == "no-store"
        assert state.json()["scope"] == "stream_operator" and len(state.json()["sessions"]) == 1
        assert "synthetic-write-only" not in state.text and "Unrelated session" not in state.text
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": state.json()["csrf_token"],
            "Idempotency-Key": "operator-switch-key",
        }
        url = f"/stream-operator/api/outputs/{outputs[0]}/switch"
        assert (
            client.post(
                url, json={"target_route_id": target}, headers={"Origin": "https://testserver"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                url,
                json={"target_route_id": target},
                headers={**headers, "Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/stream-operator/api/outputs/{outputs[1]}/switch",
                json={"target_route_id": target},
                headers=headers,
            ).status_code
            == 403
        )
        response = client.post(url, json={"target_route_id": target}, headers=headers)
        assert response.status_code == 200, response.text
        assert (
            client.post(url, json={"target_route_id": target}, headers=headers).json()
            == response.json()
        )
        assert (
            client.post(
                f"/api/broadcasts/outputs/{outputs[0]}/intent",
                json={"enabled": False},
                headers=headers,
            ).status_code
            == 401
        )
        assert client.get("/api/broadcasts").status_code == 401
        assert client.get("/api/broadcasts/ui-state").status_code == 401
        for path, payload in [
            ("/api/broadcasts/prepare", {"ingress_node_id": nodes[0]}),
            (f"/api/broadcasts/sessions/{sid}/connection", {}),
            (f"/api/broadcasts/outputs/{outputs[0]}/connection", {}),
        ]:
            assert client.post(path, json=payload, headers=headers).status_code == 401
        assert (
            client.post(
                f"/api/broadcasts/sessions/{sid}/moblin-profiles", json={}, headers=headers
            ).status_code
            == 401
        )
        assert client.post(url, content="x" * 5000, headers=headers).status_code == 413
        assert (
            client.post(
                f"/stream-operator/api/switches/{response.json()['id']}/cancel",
                json={},
                headers=headers,
            ).status_code
            == 200
        )
        saved_token = client.cookies.get(COOKIE)
        client.cookies.clear()
        csrf, _ = login(client, settings, admin_password)
        assert (
            client.post(
                f"/api/broadcasts/operators/{pairing['id']}/revoke",
                json={},
                headers={**admin, "X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
        client.cookies.clear()
        client.cookies.set(COOKIE, saved_token)
        assert client.get("/stream-operator/api/state").status_code == 401
        with store.database.connect() as db:
            dump = "\n".join(db.iterdump())
        assert token not in dump and saved_token not in dump and "synthetic-write-only" not in dump
        assert "stream_key" not in json.dumps(store.snapshot())
