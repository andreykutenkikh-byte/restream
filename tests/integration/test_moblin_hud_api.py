from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi.testclient import TestClient

import app.moblin_hud_api as hud_api
from app.core.config import Settings
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE, _safe_name
from app.services.mediamtx import IngestState, IngestStatus


class FakeMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


def _login(client: TestClient, settings: Settings, password: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _admin_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Origin": "https://testserver"}


def _relay_state(*, live: bool) -> dict[str, Any]:
    return {
        "service_state": "active" if live else "inactive",
        "enabled": False,
        "main_process": "running" if live else "stopped",
        "srt_listener": "listening" if live else "closed",
        "source": "LIVE" if live else "NONE",
        "input_bitrate_bps": 4_000_000 if live else None,
        "youtube_forward": "active" if live else "inactive",
        "overall": "healthy",
        "youtube_url_configured": True,
        "youtube_key_configured": True,
        "healthy": True,
        "portrait_profile": True,
        "error_code": None,
    }


def _heartbeat(*, live: bool) -> dict[str, Any]:
    return {
        "agent_version": "1.0.0",
        "protocol_version": 1,
        "hostname": "safe-relay-host",
        "relay": _relay_state(live=live),
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


def _create_pairing(client: TestClient, csrf: str) -> dict[str, str]:
    response = client.post(
        "/api/moblin-hud/pairings",
        json={},
        headers=_admin_headers(csrf),
    )
    assert response.status_code == 200
    return response.json()


def _touch_relay(
    app: Any,
    node_id: str,
    sequence: int,
    *,
    bitrate_bps: int | None,
    live: bool = True,
) -> None:
    observed_at = (datetime.now(UTC) + timedelta(microseconds=sequence)).isoformat()
    with app.state.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE relay_nodes
            SET service_state = ?, main_process = ?, srt_listener = ?, source = ?,
                input_bitrate_bps = ?, youtube_forward = ?, overall = 'healthy',
                last_seen_at = ?
            WHERE node_id = ?
            """,
            (
                "active" if live else "inactive",
                "running" if live else "stopped",
                "listening" if live else "closed",
                "LIVE" if live else "NONE",
                bitrate_bps if live else None,
                "active" if live else "inactive",
                observed_at,
                node_id,
            ),
        )
        connection.execute(
            "UPDATE restream_nodes SET last_seen_at = ? WHERE id = ?",
            (observed_at, node_id),
        )
        connection.execute("COMMIT")


def test_pairing_hud_quality_and_revoke_end_to_end(
    settings: Settings,
    admin_password: str,
    monkeypatch: Any,
) -> None:
    secure_settings = replace(
        settings,
        public_control_url="https://testserver",
        cookie_secure=True,
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(hud_api, "monotonic", lambda: clock["now"])
    app = create_app(secure_settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        active = app.state.relays.provision_node(
            display_name="Hong Kong",
            address="relay-a.internal.example",
        )
        standby = app.state.relays.provision_node(
            display_name="Tokyo",
            address="relay-b.internal.example",
        )
        for grant, live in ((active, True), (standby, False)):
            response = client.post(
                "/relay-agent/v1/heartbeat",
                json=_heartbeat(live=live),
                headers={"Authorization": f"Bearer {grant.node_token}"},
            )
            assert response.status_code == 200

        csrf = _login(client, secure_settings, admin_password)
        pairing = _create_pairing(client, csrf)
        split = urlsplit(pairing["pairing_url"])
        assert split.scheme == "https"
        assert split.query == ""
        assert split.fragment.startswith("pair=")
        raw_pairing_token = split.fragment.removeprefix("pair=")
        assert len(raw_pairing_token) >= 40

        encoded_settings = pairing["moblin_url"].removeprefix("moblin://?")
        assert json.loads(unquote(encoded_settings)) == {
            "webBrowser": {"home": pairing["pairing_url"]}
        }
        admin_session = client.cookies.get("adojapan_session")
        assert admin_session
        assert admin_session not in pairing["pairing_url"]
        assert admin_session not in pairing["moblin_url"]

        pair = client.post(
            "/moblin-hud/api/pair",
            json={"token": raw_pairing_token},
            headers={"Origin": "https://testserver"},
        )
        assert pair.status_code == 200
        cookie = pair.headers["set-cookie"]
        assert f"{HUD_SESSION_COOKIE}=" in cookie
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie
        assert "Domain=" not in cookie
        assert raw_pairing_token not in pair.text

        replay = client.post(
            "/moblin-hud/api/pair",
            json={"token": raw_pairing_token},
            headers={"Origin": "https://testserver"},
        )
        assert replay.status_code in {401, 409}
        assert raw_pairing_token not in replay.text

        clock["now"] = 0.0
        first_status = client.get("/moblin-hud/api/status")
        assert first_status.status_code == 200
        clock["now"] = 2.0
        duplicate = client.get("/moblin-hud/api/status")
        assert duplicate.json()["health"]["level"] == "unknown"
        assert app.state.relay_quality.route_sample_count(active.node_id) == 1

        levels: list[str] = [str(first_status.json()["health"]["level"])]
        response = first_status
        for sample in range(1, 8):
            _touch_relay(app, active.node_id, sample, bitrate_bps=4_000_000)
            clock["now"] = float(sample * 2 + 2)
            response = client.get("/moblin-hud/api/status")
            assert response.status_code == 200
            levels.append(str(response.json()["health"]["level"]))
        assert levels[0] == "unknown"
        assert levels[-1] == "green"

        serialized = json.dumps(response.json(), ensure_ascii=False)
        assert active.node_token not in serialized
        assert standby.node_token not in serialized
        assert "relay-a.internal.example" not in serialized
        assert "relay-b.internal.example" not in serialized
        assert "srt://" not in serialized.lower()
        assert "stream_key" not in serialized.lower()
        assert "youtube_url" not in serialized.lower()
        assert response.json()["scope"] == "stream_monitor"

        _touch_relay(app, active.node_id, 20, bitrate_bps=1_000_000)
        clock["now"] = 18.0
        first_dip = client.get("/moblin-hud/api/status").json()
        _touch_relay(app, active.node_id, 21, bitrate_bps=1_000_000)
        clock["now"] = 22.0
        yellow = client.get("/moblin-hud/api/status").json()
        _touch_relay(app, active.node_id, 22, bitrate_bps=1_000_000)
        clock["now"] = 28.0
        red = client.get("/moblin-hud/api/status").json()
        assert first_dip["health"]["level"] != "red"
        assert yellow["health"]["level"] == "yellow"
        assert red["health"]["level"] == "red"
        assert red["recommendation"]["target_display_name"] == "Tokyo"
        assert red["recommendation"]["confidence"] == "standby_server_readiness"
        assert red["recommendation"]["route_to_target_measured"] is False

        for sequence, observed_at in enumerate((30.0, 32.0, 34.0), start=30):
            _touch_relay(app, active.node_id, sequence, bitrate_bps=4_000_000)
            clock["now"] = observed_at
            recovered = client.get("/moblin-hud/api/status").json()
        assert recovered["health"]["level"] == "green"

        previous_session = app.state.moblin_hud_stream_session_sequence
        _touch_relay(app, active.node_id, 40, bitrate_bps=None, live=False)
        clock["now"] = 36.0
        assert client.get("/moblin-hud/api/status").json()["stream_state"] == "idle"
        _touch_relay(app, active.node_id, 41, bitrate_bps=4_000_000)
        clock["now"] = 38.0
        restarted = client.get("/moblin-hud/api/status").json()
        assert restarted["health"]["level"] == "unknown"
        assert app.state.moblin_hud_stream_session_sequence > previous_session

        logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logout.status_code == 200
        assert client.get("/api/nodes").status_code == 401
        assert client.get("/moblin-hud/api/status").status_code == 200

        csrf = _login(client, secure_settings, admin_password)
        revoke = client.post(
            f"/api/moblin-hud/devices/{pairing['device_id']}/revoke",
            json={},
            headers=_admin_headers(csrf),
        )
        assert revoke.status_code == 200
        assert client.get("/moblin-hud/api/status").status_code == 401

        with app.state.database.connect() as connection:
            dump = "\n".join(connection.iterdump())
        assert raw_pairing_token not in dump
        audit = app.state.database.list_audit_events(limit=100)
        assert raw_pairing_token not in json.dumps(audit)
        assert {item["event_type"] for item in audit} >= {
            "moblin_hud.pairing_created",
            "moblin_hud.device_paired",
            "moblin_hud.device_revoked",
        }


def test_hud_endpoints_enforce_admin_origin_csrf_auth_and_body_limit(
    settings: Settings,
    admin_password: str,
) -> None:
    secure_settings = replace(
        settings,
        public_control_url="https://testserver",
        cookie_secure=True,
    )
    app = create_app(secure_settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get("/moblin-hud")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert client.get("/moblin-hud/api/status").status_code == 401
        assert client.post("/api/moblin-hud/pairings", json={}).status_code == 401

        csrf = _login(client, secure_settings, admin_password)
        missing_origin = client.post(
            "/api/moblin-hud/pairings",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert missing_origin.status_code == 403
        missing_csrf = client.post(
            "/api/moblin-hud/pairings",
            json={},
            headers={"Origin": "https://testserver"},
        )
        assert missing_csrf.status_code == 403

        oversized = client.post(
            "/moblin-hud/api/pair",
            content=b"x" * 5000,
            headers={"Origin": "https://testserver", "Content-Type": "application/json"},
        )
        assert oversized.status_code == 413


def test_pairing_attempts_are_rate_limited_without_reflecting_tokens(
    settings: Settings,
) -> None:
    secure_settings = replace(
        settings,
        public_control_url="https://testserver",
        cookie_secure=True,
    )
    app = create_app(secure_settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        marker = "A" * 43
        for _ in range(8):
            response = client.post(
                "/moblin-hud/api/pair",
                json={"token": marker},
                headers={"Origin": "https://testserver"},
            )
            assert response.status_code == 401
            assert marker not in response.text
        limited = client.post(
            "/moblin-hud/api/pair",
            json={"token": marker},
            headers={"Origin": "https://testserver"},
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert marker not in limited.text


def test_route_names_remove_locator_like_values() -> None:
    for unsafe in (
        "HK-2001:db8::1",
        "relay 203.0.113.7:8890",
        "srt://relay.example/live",
        "root@relay.example",
        "/var/lib/relay",
        r"C:\relay\secret",
    ):
        assert _safe_name(unsafe, "Relay") == "Relay"
    assert _safe_name("Hong Kong", "Relay") == "Hong Kong"
