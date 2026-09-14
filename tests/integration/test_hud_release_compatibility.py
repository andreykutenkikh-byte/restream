"""Exercise HUD-only behavior against the unchanged main heartbeat protocol."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import app.moblin_hud_api as hud_api
from app.core.config import Settings
from app.core.security import encrypt_destination_key
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE
from app.services.mediamtx import IngestState, IngestStatus
from relay_agent.models import RelaySnapshot

CONTROL_TABLES = (
    "destinations",
    "ingest_config",
    "restream_nodes",
    "relay_nodes",
    "node_commands",
    "relay_commands",
    "node_install_jobs",
    "node_credentials",
    "node_enrollment_tokens",
)


class OfflineMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        raise AssertionError("HUD must not kick a publisher")


def _legacy_heartbeat() -> dict[str, Any]:
    # Use the real, pinned-main agent serializer: unavailable bitrate is OMITTED.
    state = RelaySnapshot(
        service_state="active",
        enabled=True,
        main_process="running",
        srt_listener="listening",
        source="LIVE",
        youtube_forward="active",
        overall="healthy",
        youtube_url_configured=True,
        youtube_key_configured=True,
        healthy=True,
        portrait_profile=True,
    ).to_json()
    assert "input_bitrate_bps" not in state
    return {
        "agent_version": "1.0.0",
        "protocol_version": 1,
        "hostname": "legacy-relay",
        "relay": state,
        "host": {
            "uptime_seconds": 100,
            "load_1m": 0.1,
            "cpu_percent": 10.0,
            "memory_total_bytes": 2_000_000_000,
            "memory_available_bytes": 1_000_000_000,
            "disk_total_bytes": 20_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
        "current_command_id": None,
    }


def _login(client: TestClient, settings: Settings, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"login": settings.admin_login, "password": password}
    )
    assert response.status_code == 200
    return {"Origin": "https://testserver", "X-CSRF-Token": response.json()["csrf_token"]}


def _pair(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    response = client.post("/api/moblin-hud/pairings", json={}, headers=headers)
    assert response.status_code == 200
    pairing = response.json()
    token = urlsplit(pairing["pairing_url"]).fragment.removeprefix("pair=")
    assert (
        client.post(
            "/moblin-hud/api/pair", json={"token": token}, headers={"Origin": "https://testserver"}
        ).status_code
        == 200
    )
    return pairing["device_id"], token


def _control_state(app: Any) -> dict[str, list[tuple[Any, ...]]]:
    with app.state.database.connect() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608
            ]
            for table in CONTROL_TABLES
        }


def _guard_actuators(app: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(*_: Any, **__: Any) -> None:
        calls.append("actuator")
        raise AssertionError("Read-only HUD reached a media/control actuator")

    for target, names in (
        (app.state.relays, ("create_command", "provision_node")),
        (app.state.nodes, ("create_command", "create_pending_node", "issue_enrollment")),
        (app.state.bootstrap, ("create_job",)),
        (app.state.runtime, ("rotate_ingest_key",)),
        (app.state.runtime.mediamtx, ("kick_publishers",)),
        (app.state.runtime.workers, ("start", "stop", "reconcile")),
        (app.state.runtime.workers._launcher, ("spawn",)),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    return calls


def test_admin_and_hud_reads_pairing_replay_and_revoke_never_control_media(
    settings: Settings,
    admin_password: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = replace(settings, public_control_url="https://testserver", cookie_secure=True)
    app = create_app(settings, mediamtx=OfflineMediaMTX())  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        relay = app.state.relays.provision_node(
            display_name="Existing relay", address="legacy-relay.internal.example"
        )
        assert (
            client.post(
                "/relay-agent/v1/heartbeat",
                json=_legacy_heartbeat(),
                headers={"Authorization": f"Bearer {relay.node_token}"},
            ).status_code
            == 200
        )
        app.state.database.create_destination(
            name="Existing output",
            server_url="rtmps://existing-output.example/live",
            encrypted_key=encrypt_destination_key(
                "synthetic-output-key", settings.master_encryption_key
            ),
            enabled=False,
        )
        before = _control_state(app)
        with monkeypatch.context() as guarded:
            calls = _guard_actuators(app, guarded)
            headers = _login(client, settings, admin_password)
            assert client.get("/").status_code == 200
            assert client.get("/api/ingest/status").status_code == 200
            assert client.get("/api/relay-nodes").status_code == 200
            assert client.get("/api/destinations").status_code == 200
            device_id, pairing_token = _pair(client, headers)
            assert client.get("/moblin-hud").status_code == 200
            assert client.get("/moblin-hud/api/status").status_code == 200
            replay = client.post(
                "/moblin-hud/api/pair",
                json={"token": pairing_token},
                headers={"Origin": "https://testserver"},
            )
            assert replay.status_code == 401
            hud_cookie = client.cookies.get(HUD_SESSION_COOKIE)
            client.cookies.clear()
            client.cookies.set(HUD_SESSION_COOKIE, hud_cookie)
            assert client.get("/moblin-hud/api/status").status_code == 200
            assert client.get("/api/nodes").status_code == 401
            assert client.get("/api/destinations").status_code == 401
            assert (
                client.post(
                    f"/api/nodes/{relay.node_id}/relay/start", json={}, headers=headers
                ).status_code
                == 401
            )
            assert client.post("/api/ingest/rotate", headers=headers).status_code == 401
            assert (
                client.post("/api/moblin-hud/pairings", json={}, headers=headers).status_code == 401
            )
            headers = _login(client, settings, admin_password)
            assert (
                client.post(
                    f"/api/moblin-hud/devices/{device_id}/revoke", headers=headers
                ).status_code
                == 200
            )
            assert client.get("/moblin-hud/api/status").status_code == 401
            assert calls == []
            assert _control_state(app) == before
        assert pairing_token not in caplog.text
        assert hud_cookie not in caplog.text


def test_legacy_omitted_bitrate_remains_unknown_after_long_fresh_monitoring(
    settings: Settings, admin_password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(settings, public_control_url="https://testserver", cookie_secure=True)
    app = create_app(settings, mediamtx=OfflineMediaMTX())  # type: ignore[arg-type]
    elapsed = {"seconds": 0.0}
    initial = datetime.now(UTC)

    def clock() -> datetime:
        return initial + timedelta(seconds=elapsed["seconds"])

    monkeypatch.setattr(hud_api, "monotonic", lambda: elapsed["seconds"])
    monkeypatch.setattr(
        hud_api,
        "_heartbeat_age",
        lambda value: (clock() - datetime.fromisoformat(value)).total_seconds(),
    )
    monkeypatch.setattr(app.state.relays, "clock", clock)
    monkeypatch.setattr(app.state.nodes, "clock", clock)
    with TestClient(app, base_url="https://testserver") as client:
        relay = app.state.relays.provision_node(
            display_name="Legacy relay", address="legacy.example"
        )
        _pair(client, _login(client, settings, admin_password))
        for seconds in (0.0, 5.0, 15.0, 35.0, 65.0, 180.0):
            elapsed["seconds"] = seconds
            assert (
                client.post(
                    "/relay-agent/v1/heartbeat",
                    json=_legacy_heartbeat(),
                    headers={"Authorization": f"Bearer {relay.node_token}"},
                ).status_code
                == 200
            )
            response = client.get("/moblin-hud/api/status")
            assert response.status_code == 200
            payload = response.json()
            assert payload["current_route"]["source"] == "LIVE"
            assert payload["current_route"]["input_bitrate_bps"] is None
            assert payload["current_route"]["stable_baseline_bps"] is None
            assert payload["health"]["level"] == "unknown"
            assert "media_stalled" not in payload["health"]["reason_codes"]
            assert payload["recommendation"]["action"] == "watch"


def test_paired_cookie_survives_app_restart_without_creating_commands(
    settings: Settings, admin_password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(settings, public_control_url="https://testserver", cookie_secure=True)
    original = create_app(settings, mediamtx=OfflineMediaMTX())  # type: ignore[arg-type]
    with TestClient(original, base_url="https://testserver") as client:
        _pair(client, _login(client, settings, admin_password))
        hud_cookie = client.cookies.get(HUD_SESSION_COOKIE)
        ingest_key = original.state.database.get_ingest_encrypted()
        before = _control_state(original)
    restarted = create_app(settings, mediamtx=OfflineMediaMTX())  # type: ignore[arg-type]
    with TestClient(restarted, base_url="https://testserver") as client:
        client.cookies.set(HUD_SESSION_COOKIE, hud_cookie)
        with monkeypatch.context() as guarded:
            calls = _guard_actuators(restarted, guarded)
            assert client.get("/moblin-hud").status_code == 200
            assert client.get("/moblin-hud/api/status").status_code == 200
            assert client.get("/api/nodes").status_code == 401
            assert calls == []
        assert _control_state(restarted) == before
        assert restarted.state.database.get_ingest_encrypted() == ingest_key
        with restarted.state.database.connect() as connection:
            persisted = "\n".join(connection.iterdump())
        assert hud_cookie not in persisted
        assert len(restarted.state.moblin_hud.list_devices()) == 1
        assert "session_digest" not in json.dumps(restarted.state.moblin_hud.list_devices())
