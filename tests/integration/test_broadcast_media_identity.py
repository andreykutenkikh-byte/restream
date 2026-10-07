from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient
from test_api_smoke import FakeMediaMTX
from test_node_api import create_pending, enroll, heartbeat_payload

from app.broadcast.envelope import open_envelope, public_key
from app.broadcast.media_control import MediaHeartbeat, MediaNodeEnable
from app.broadcast.models import CAPABILITIES, ResourceLimits
from app.core.config import Settings
from app.main import create_app
from app.services.nodes import NodeAuthenticationError
from app.services.relays import RelayAuthenticationError


@pytest.mark.parametrize("identity", ["node", "relay"])
def test_v2_requires_explicit_opt_in_pinned_key_and_unrevoked_identity(
    settings: Settings, identity: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())
    with TestClient(app) as client:
        if identity == "node":
            node_id = create_pending(app.state.nodes)
            token = enroll(client, app.state.nodes, node_id)
            app.state.nodes.record_heartbeat(token, heartbeat_payload())
        else:
            grant = app.state.relays.provision_node(display_name="Synthetic", address="a.example")
            node_id, token = grant.node_id, grant.node_token
        key = X25519PrivateKey.generate()
        heartbeat = MediaHeartbeat(
            boot_id="synthetic-media-identity",
            public_key=public_key(key),
            capabilities=sorted(CAPABILITIES),
            sequence=1,
            plan_generation=0,
        ).model_dump()
        endpoint = "/broadcast-agent/v2/heartbeat"
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post(endpoint, json=heartbeat).status_code == 401
        assert client.post(endpoint, json=heartbeat, headers=headers).status_code == 404
        app.state.broadcast_media.enable(
            node_id,
            MediaNodeEnable(
                public_key=public_key(key),
                srt_host="8.8.8.8",
                srt_port=19000,
                limits=ResourceLimits(),
            ),
        )
        wrong = {**heartbeat, "public_key": public_key(X25519PrivateKey.generate())}
        assert client.post(endpoint, json=wrong, headers=headers).status_code == 403
        response = client.post(endpoint, json=heartbeat, headers=headers)
        assert response.status_code == 200
        assert open_envelope(key, response.json(), node_id)["routes"] == []
        assert token not in response.text
        # V2 admission must not cross-authorize either existing v1 API domain.
        if identity == "node":
            with pytest.raises(RelayAuthenticationError):
                app.state.relays.authenticate(token)
        else:
            with pytest.raises(NodeAuthenticationError):
                app.state.nodes.authenticate(token)
        with app.state.broadcasts.database.connect() as db:
            db.execute("UPDATE broadcast_media_nodes SET enabled=0 WHERE node_id=?", (node_id,))
        assert client.post(endpoint, json=heartbeat, headers=headers).status_code == 403
        app.state.nodes.revoke_node(node_id)
        assert client.post(endpoint, json=heartbeat, headers=headers).status_code == 401
