from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.mediamtx import IngestState, IngestStatus


class FakeMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


def live_heartbeat(*, agent_version: str = "1.1.0", source: str = "LIVE") -> dict[str, Any]:
    active = source in {"LIVE", "SLATE"}
    return {
        "agent_version": agent_version,
        "protocol_version": 1,
        "hostname": "hk-relay",
        "relay": {
            "service_state": "active" if active else "inactive",
            "enabled": False,
            "main_process": "running" if active else "stopped",
            "srt_listener": "listening" if active else "closed",
            "source": source,
            "input_bitrate_bps": 4_000_000 if source == "LIVE" else None,
            "youtube_forward": "active" if active else "inactive",
            "overall": "healthy",
            "youtube_url_configured": True,
            "youtube_key_configured": True,
            "healthy": True,
            "portrait_profile": True,
            "error_code": None,
        },
        "host": {
            "uptime_seconds": 100,
            "load_1m": 0.1,
            "cpu_percent": 2.5,
            "memory_total_bytes": 2_000_000_000,
            "memory_available_bytes": 1_000_000_000,
            "disk_total_bytes": 20_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
        "current_command_id": None,
    }


def login(client: TestClient, settings: Settings, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    return {
        "X-CSRF-Token": response.json()["csrf_token"],
        "Origin": "http://testserver",
    }


def ts_segment(marker: int = 0) -> bytes:
    return (bytes((0x47, marker)) + bytes(186)) * 20


def test_preview_is_demand_driven_outbound_only_and_session_protected(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    observed_at = datetime(2026, 9, 2, tzinfo=UTC)
    app.state.relays.clock = lambda: observed_at
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}

        old_agent = client.post(
            "/relay-agent/v1/heartbeat",
            json=live_heartbeat(agent_version="1.0.0"),
            headers=bearer,
        )
        assert old_agent.status_code == 200
        assert "preview_requested" not in old_agent.json()
        assert client.get(f"/api/nodes/{grant.node_id}/relay/preview/index.m3u8").status_code == 401

        observed_at += timedelta(seconds=2)
        capable_agent = client.post(
            "/relay-agent/v1/heartbeat",
            json=live_heartbeat(),
            headers=bearer,
        )
        assert capable_agent.status_code == 200
        assert capable_agent.json()["preview_requested"] is False

        admin = login(client, settings, admin_password)
        lease_path = f"/api/nodes/{grant.node_id}/relay/preview/lease"
        assert client.post(lease_path).status_code == 403
        assert (
            client.post(
                lease_path,
                headers={"X-CSRF-Token": admin["X-CSRF-Token"]},
            ).status_code
            == 403
        )
        leased = client.post(lease_path, headers=admin)
        assert leased.status_code == 204
        assert "no-store" in leased.headers["cache-control"]

        observed_at += timedelta(seconds=2)
        requested = client.post(
            "/relay-agent/v1/heartbeat",
            json=live_heartbeat(),
            headers=bearer,
        )
        assert requested.status_code == 200
        assert requested.json()["preview_requested"] is True

        generation = str(uuid4())
        upload_path = f"/relay-media/v1/preview/segments/{generation}/17"
        sentinel = b"PREVIEW_MEDIA_MUST_STAY_IN_MEMORY_71"
        preview_payload = bytearray(ts_segment())
        preview_payload[1 : 1 + len(sentinel)] = sentinel
        preview_bytes = bytes(preview_payload)
        assert (
            client.put(
                upload_path,
                content=ts_segment(),
                headers={"Content-Type": "video/mp2t"},
            ).status_code
            == 401
        )
        wrong_type = client.put(
            upload_path,
            content=ts_segment(),
            headers={**bearer, "Content-Type": "application/octet-stream"},
        )
        assert wrong_type.status_code == 400
        bad_sync = client.put(
            upload_path,
            content=bytes(188),
            headers={**bearer, "Content-Type": "video/mp2t"},
        )
        assert bad_sync.status_code == 422

        uploaded = client.put(
            upload_path,
            content=preview_bytes,
            headers={**bearer, "Content-Type": "video/mp2t"},
        )
        assert uploaded.status_code == 204
        assert "no-store" in uploaded.headers["cache-control"]

        playlist_path = f"/api/nodes/{grant.node_id}/relay/preview/index.m3u8"
        playlist = client.get(playlist_path)
        assert playlist.status_code == 200
        assert playlist.headers["content-type"].startswith("application/vnd.apple.mpegurl")
        assert "no-store" in playlist.headers["cache-control"]
        assert f"segment/{generation}/17.ts" in playlist.text
        assert "relay-media" not in playlist.text

        segment = client.get(f"/api/nodes/{grant.node_id}/relay/preview/segment/{generation}/17.ts")
        assert segment.status_code == 200
        assert segment.content == preview_bytes
        assert segment.headers["content-type"].startswith("video/mp2t")
        assert "no-store" in segment.headers["cache-control"]

        assert app.state.relay_preview.stored_bytes == len(preview_bytes)
        assert sentinel not in settings.database_path.read_bytes()
        assert sentinel.decode("ascii") not in str(app.state.database.list_audit_events())
        logged_out = client.post("/api/auth/logout", headers=admin)
        assert logged_out.status_code == 200
        assert app.state.relay_preview.stored_bytes == 0


def test_upload_contract_fails_closed_and_non_live_heartbeat_purges_cache(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    observed_at = datetime(2026, 9, 2, tzinfo=UTC)
    app.state.relays.clock = lambda: observed_at
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=live_heartbeat(), headers=bearer
            ).status_code
            == 200
        )
        admin = login(client, settings, admin_password)
        lease_path = f"/api/nodes/{grant.node_id}/relay/preview/lease"
        assert client.post(lease_path, headers=admin).status_code == 204

        generation = str(uuid4())
        upload_path = f"/relay-media/v1/preview/segments/{generation}/1"
        malformed = client.put(
            upload_path,
            content=ts_segment(),
            headers={
                **bearer,
                "Content-Type": "video/mp2t",
                "Transfer-Encoding": "chunked",
            },
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "invalid_preview_segment"
        oversized = client.put(
            upload_path,
            content=b"x",
            headers={
                **bearer,
                "Content-Type": "video/mp2t",
                "Content-Length": str(3 * 1024 * 1024 + 1),
            },
        )
        assert oversized.status_code == 413

        valid = client.put(
            upload_path,
            content=ts_segment(),
            headers={**bearer, "Content-Type": "video/mp2t"},
        )
        assert valid.status_code == 204

        observed_at += timedelta(seconds=2)
        stopped = client.post(
            "/relay-agent/v1/heartbeat",
            json=live_heartbeat(source="NONE"),
            headers=bearer,
        )
        assert stopped.status_code == 200
        assert stopped.json()["preview_requested"] is False
        assert (
            client.get(
                f"/api/nodes/{grant.node_id}/relay/preview/segment/{generation}/1.ts"
            ).status_code
            == 409
        )

        observed_at += timedelta(seconds=2)
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=live_heartbeat(), headers=bearer
            ).status_code
            == 200
        )
        not_requested = client.put(
            f"/relay-media/v1/preview/segments/{uuid4()}/2",
            content=ts_segment(),
            headers={**bearer, "Content-Type": "video/mp2t"},
        )
        assert not_requested.status_code == 409
        assert not_requested.json()["error"]["code"] == "preview_not_requested"


def test_application_shutdown_clears_preview_memory(settings: Settings) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    node_id = str(uuid4())
    generation = str(uuid4())
    with TestClient(app):
        app.state.relay_preview.renew(node_id)
        app.state.relay_preview.put(node_id, generation, 1, ts_segment())
        assert app.state.relay_preview.stored_bytes == len(ts_segment())

    assert app.state.relay_preview.stored_bytes == 0


def test_successful_stop_completion_purges_preview_immediately(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    observed_at = datetime(2026, 9, 2, tzinfo=UTC)
    app.state.relays.clock = lambda: observed_at
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=live_heartbeat(), headers=bearer
            ).status_code
            == 200
        )
        admin = login(client, settings, admin_password)
        assert (
            client.post(
                f"/api/nodes/{grant.node_id}/relay/preview/lease", headers=admin
            ).status_code
            == 204
        )
        generation = str(uuid4())
        assert (
            client.put(
                f"/relay-media/v1/preview/segments/{generation}/1",
                content=ts_segment(),
                headers={**bearer, "Content-Type": "video/mp2t"},
            ).status_code
            == 204
        )

        queued = client.post(f"/api/nodes/{grant.node_id}/relay/stop", headers=admin)
        assert queued.status_code == 202
        command_id = queued.json()["command_id"]
        assert client.get("/relay-agent/v1/commands/next?wait=0", headers=bearer).status_code == 200
        assert (
            client.post(
                f"/relay-agent/v1/commands/{command_id}/ack", json={}, headers=bearer
            ).status_code
            == 200
        )
        stopped_state = live_heartbeat(source="NONE")["relay"]
        completed = client.post(
            f"/relay-agent/v1/commands/{command_id}/complete",
            json={
                "status": "ok",
                "completed_at": observed_at.isoformat(),
                "safe_result": stopped_state,
                "secret_result": None,
            },
            headers=bearer,
        )
        assert completed.status_code == 200
        assert app.state.relay_preview.stored_bytes == 0
