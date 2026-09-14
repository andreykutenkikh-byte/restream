from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

import app.moblin_hud_api as hud_api
from app.core.config import Settings
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE, _safe_name
from app.services.mediamtx import IngestState, IngestStatus
from app.services.relay_quality import RECOVERY_GRACE_SECONDS, RelayQualityTracker


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    secure_settings = replace(
        settings,
        public_control_url="https://testserver",
        cookie_secure=True,
    )
    clock = {"now": 0.0}
    caplog.set_level("DEBUG")
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

        assert app.state.relay_quality.baseline_for(active.node_id) == 4_000_000
        _touch_relay(app, active.node_id, 40, bitrate_bps=None, live=False)
        clock["now"] = 36.0
        assert client.get("/moblin-hud/api/status").json()["stream_state"] == "idle"
        _touch_relay(app, active.node_id, 41, bitrate_bps=4_000_000)
        clock["now"] = 38.0
        restarted = client.get("/moblin-hud/api/status").json()
        assert restarted["health"]["level"] == "unknown"
        assert app.state.relay_quality.baseline_for(active.node_id) is None

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
        assert raw_pairing_token not in caplog.text
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


@dataclass
class HudScenario:
    app: Any
    client: TestClient
    node_id: str
    node_token: str
    now: float = 0.0

    def heartbeat(
        self,
        source: str,
        *,
        grant: Any = None,
        stopped: bool = False,
        failed: bool = False,
        bitrate: int = 4_000_000,
    ) -> None:
        payload = _heartbeat(live=source == "LIVE")
        payload["relay"].update(
            {
                "source": source,
                "service_state": "failed" if failed else "inactive" if stopped else "active",
                "main_process": "failed" if failed else "stopped" if stopped else "running",
                "srt_listener": "closed" if stopped or failed else "listening",
                "youtube_forward": "inactive" if stopped else "failed" if failed else "active",
                "overall": "failed" if failed else "healthy",
                "input_bitrate_bps": bitrate if source == "LIVE" else None,
            }
        )
        token = grant.node_token if grant is not None else self.node_token
        response = self.client.post(
            "/relay-agent/v1/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    def status(self, now: float) -> dict[str, Any]:
        self.now = now
        response = self.client.get("/moblin-hud/api/status")
        assert response.status_code == 200
        return response.json()

    def observe(self, now: float, source: str, **kwargs: Any) -> dict[str, Any]:
        self.now = now
        self.heartbeat(source, **kwargs)
        return self.status(now)


@pytest.fixture
def hud_scenario(
    settings: Settings,
    admin_password: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[HudScenario]:
    configured = replace(settings, public_control_url="https://testserver", cookie_secure=True)
    app = create_app(configured, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        grant = app.state.relays.provision_node(
            display_name="Hong Kong",
            address="relay-fixture.internal.example",
        )
        scenario = HudScenario(app, client, grant.node_id, grant.node_token)
        epoch = datetime.now(UTC)
        clock = lambda: epoch + timedelta(seconds=scenario.now)  # noqa: E731
        monkeypatch.setattr(hud_api, "monotonic", lambda: scenario.now)
        monkeypatch.setattr(app.state.relays, "_time", clock)
        monkeypatch.setattr(app.state.nodes, "_time", clock)
        monkeypatch.setattr(
            hud_api,
            "_heartbeat_age",
            lambda value: (
                max(0.0, (clock() - datetime.fromisoformat(value)).total_seconds())
                if isinstance(value, str)
                else None
            ),
        )
        csrf = _login(client, configured, admin_password)
        pairing = _create_pairing(client, csrf)
        token = urlsplit(pairing["pairing_url"]).fragment.removeprefix("pair=")
        assert (
            client.post(
                "/moblin-hud/api/pair",
                json={"token": token},
                headers={"Origin": "https://testserver"},
            ).status_code
            == 200
        )
        yield scenario


def test_initial_slate_and_backend_restart_do_not_invent_live_history(
    hud_scenario: HudScenario,
) -> None:
    scenario = hud_scenario
    initial = scenario.observe(0, "SLATE")
    assert initial["stream_state"] == "idle"
    assert initial["health"]["level"] == "unknown"
    assert initial["recommendation"]["action"] == "unavailable"
    scenario.observe(2, "LIVE")
    assert scenario.observe(4, "SLATE")["health"]["level"] == "black"
    scenario.app.state.relay_quality = RelayQualityTracker()
    scenario.app.state.moblin_hud_status_observations = {}
    restarted = scenario.status(6)
    assert restarted["stream_state"] == "idle"
    assert restarted["current_route"] is None


@pytest.mark.parametrize("source", ["SLATE", "NONE"])
def test_running_source_loss_preserves_route_and_waits_for_recovery(
    hud_scenario: HudScenario,
    source: str,
) -> None:
    scenario = hud_scenario
    scenario.observe(0, "LIVE")
    lost = scenario.observe(2, source)
    assert lost["stream_state"] == "active"
    assert lost["current_route"]["route_id"] == scenario.node_id
    assert lost["health"]["level"] == "black"
    assert lost["health"]["title"] == "Входящее видео пропало"
    assert lost["recommendation"]["action"] == "watch"
    assert lost["recommendation"]["reason_code"] == "recovery_grace"
    assert ("Сервер передаёт заставку" in lost["health"]["message"]) == (source == "SLATE")
    resumed = scenario.observe(4, "LIVE")
    assert resumed["recommendation"]["action"] == "watch"
    assert resumed["recommendation"]["reason_code"] == "recovering_source"
    assert resumed["recommendation"]["target_route_id"] is None
    scenario.observe(6, "LIVE")
    assert scenario.observe(8, "LIVE")["health"]["level"] == "green"


def test_only_coherent_stop_is_idle_and_resets_baseline(hud_scenario: HudScenario) -> None:
    scenario = hud_scenario
    for now in range(0, 16, 2):
        scenario.observe(float(now), "LIVE")
    assert scenario.app.state.relay_quality.baseline_for(scenario.node_id) == 4_000_000
    assert scenario.observe(16, "NONE")["stream_state"] == "active"
    stopped = scenario.observe(18, "NONE", stopped=True)
    assert stopped["stream_state"] == "idle"
    assert stopped["current_route"] is None
    assert scenario.app.state.relay_quality.baseline_for(scenario.node_id) is None
    assert scenario.observe(20, "LIVE")["health"]["level"] == "unknown"


def test_stale_telemetry_is_not_video_loss_or_broadcast_stop(hud_scenario: HudScenario) -> None:
    scenario = hud_scenario
    scenario.observe(0, "LIVE")
    scenario.observe(2, "SLATE")
    samples = scenario.app.state.relay_quality.route_sample_count(scenario.node_id)
    stale = scenario.status(33)
    assert stale["stream_state"] == "unknown"
    assert stale["health"]["level"] == "unknown"
    assert stale["health"]["reason_codes"] == ["telemetry_unavailable"]
    assert stale["recommendation"]["action"] == "watch"
    assert stale["current_route"]["source"] == "UNKNOWN"
    assert stale["current_route"]["youtube_forward_state"] == "unknown"
    assert "заставку" not in stale["health"]["message"]
    expired = scenario.status(123)
    assert expired["stream_state"] == "unknown"
    assert expired["current_route"] is None
    assert expired["recommendation"]["action"] == "watch"
    assert scenario.app.state.relay_quality.route_sample_count(scenario.node_id) <= samples


@pytest.mark.parametrize("with_standby", [False, True])
def test_persistent_loss_expires_grace_only_with_fresh_evidence(
    hud_scenario: HudScenario,
    with_standby: bool,
) -> None:
    scenario = hud_scenario
    backup = None
    if with_standby:
        backup = scenario.app.state.relays.provision_node(
            display_name="Tokyo",
            address="backup-fixture.internal.example",
        )
        scenario.heartbeat("NONE", grant=backup, stopped=True)
    scenario.observe(0, "LIVE")
    scenario.observe(2, "SLATE")
    for now in range(10, 121, 10):
        scenario.now = float(now)
        if backup is not None:
            scenario.heartbeat("NONE", grant=backup, stopped=True)
        watched = scenario.observe(float(now), "SLATE")
        assert watched["recommendation"]["reason_code"] == "recovery_grace"
    expired = scenario.observe(2 + RECOVERY_GRACE_SECONDS, "SLATE")
    expected = "switch_recommended" if with_standby else "reconnect"
    assert expired["recommendation"]["action"] == expected
    if backup is not None:
        assert expired["recommendation"]["target_route_id"] == backup.node_id
        assert expired["recommendation"]["confidence"] == "standby_server_readiness"
    assert expired["recommendation"]["route_to_target_measured"] is False
    resumed = scenario.observe(124, "LIVE")
    assert resumed["recommendation"]["action"] == "watch"
    assert resumed["recommendation"]["target_route_id"] is None
    assert resumed["recommendation"]["reason_code"] == "recovering_source"
    assert scenario.status(126)["recommendation"]["action"] == "watch"


def test_fresh_process_failure_is_distinct_from_missing_telemetry(
    hud_scenario: HudScenario,
) -> None:
    scenario = hud_scenario
    scenario.observe(0, "LIVE")
    failed = scenario.observe(2, "NONE", failed=True)
    assert failed["health"]["level"] == "black"
    assert "relay_process_failed" in failed["health"]["reason_codes"]
    assert "заставку" not in failed["health"]["message"]
    assert failed["recommendation"]["reason_code"] != "recovery_grace"
    assert scenario.status(33)["health"]["level"] == "unknown"


def test_initial_process_failure_is_visible_without_inventing_a_previous_session(
    hud_scenario: HudScenario,
) -> None:
    failed = hud_scenario.observe(0, "NONE", failed=True)
    assert failed["stream_state"] == "unknown"
    assert failed["health"]["level"] == "black"
    assert failed["health"]["reason_codes"] == ["relay_process_failed"]
    assert failed["current_route"] is None
    assert failed["recommendation"]["action"] == "watch"


def test_multiple_fresh_live_routes_are_ambiguous(hud_scenario: HudScenario) -> None:
    scenario = hud_scenario
    scenario.observe(0, "LIVE")
    backup = scenario.app.state.relays.provision_node(
        display_name="Tokyo",
        address="backup-fixture.internal.example",
    )
    scenario.now = 2
    scenario.heartbeat("LIVE", grant=backup)
    ambiguous = scenario.status(2)
    assert ambiguous["stream_state"] == "ambiguous"
    assert ambiguous["current_route"] is None
    assert ambiguous["recommendation"]["action"] == "watch"


def test_standby_updates_and_time_ticks_do_not_sample_old_active_heartbeat(
    hud_scenario: HudScenario,
) -> None:
    scenario = hud_scenario
    backup = scenario.app.state.relays.provision_node(
        display_name="Tokyo",
        address="backup-fixture.internal.example",
    )
    scenario.heartbeat("NONE", grant=backup, stopped=True)
    scenario.observe(0, "LIVE")
    tracker = scenario.app.state.relay_quality
    for now in (2.0, 4.0, 6.0, 12.0, 32.0):
        scenario.now = now
        scenario.heartbeat("NONE", grant=backup, stopped=True)
        result = scenario.status(now)
        assert tracker.route_sample_count(scenario.node_id) == 1
        assert tracker.baseline_for(scenario.node_id) is None
        assert tracker.ema_for(scenario.node_id) == 4_000_000
    assert result["stream_state"] == "unknown"
    assert tracker.route_sample_count(backup.node_id) == 6


def test_elapsed_red_threshold_advances_without_resampling_a_fresh_heartbeat(
    hud_scenario: HudScenario,
) -> None:
    scenario = hud_scenario
    for now in range(0, 16, 2):
        scenario.observe(float(now), "LIVE")
    for now in (16.0, 18.0, 20.0):
        degraded = scenario.observe(now, "LIVE", bitrate=1_000_000)
    assert degraded["health"]["level"] == "yellow"
    tracker = scenario.app.state.relay_quality
    samples = tracker.route_sample_count(scenario.node_id)
    ema = tracker.ema_for(scenario.node_id)
    elapsed = scenario.status(24)
    assert elapsed["health"]["level"] == "red"
    assert elapsed["recommendation"]["action"] == "watch"
    assert elapsed["recommendation"]["target_route_id"] is None
    assert tracker.route_sample_count(scenario.node_id) == samples
    assert tracker.ema_for(scenario.node_id) == ema


def test_confirmed_zero_bitrate_loss_keeps_grace_and_survives_baseline_expiration(
    hud_scenario: HudScenario,
) -> None:
    scenario = hud_scenario
    for now in range(0, 16, 2):
        scenario.observe(float(now), "LIVE")
    for now in range(16, 139, 2):
        missing = scenario.observe(float(now), "LIVE", bitrate=0)
        if now > 46:
            assert missing["health"]["level"] == "black"
            assert "media_stalled" in missing["health"]["reason_codes"]
            assert missing["recommendation"]["action"] == (
                "watch" if now < 16 + RECOVERY_GRACE_SECONDS else "reconnect"
            )
            if now < 16 + RECOVERY_GRACE_SECONDS:
                assert missing["recommendation"]["reason_code"] == "recovery_grace"
    assert scenario.app.state.relay_quality.baseline_for(scenario.node_id) is None
    resumed = scenario.observe(140, "LIVE")
    assert resumed["health"]["level"] == "unknown"
    assert resumed["recommendation"]["action"] == "watch"
    assert resumed["recommendation"]["reason_code"] == "recovering_source"
    scenario.observe(142, "LIVE")
    assert scenario.observe(144, "LIVE")["health"]["level"] == "green"


def test_main_direct_freshness_and_zero_progress_timer_do_not_manufacture_samples(
    hud_scenario: HudScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = hud_scenario
    main: dict[str, Any] = {
        "state": "live",
        "bytes_received": 1_000_000,
        "bitrate_bps": 4_000_000,
        "metadata": {"width": 1080, "height": 1920},
    }

    async def ingest() -> dict[str, Any]:
        return dict(main)

    monkeypatch.setattr(scenario.app.state.runtime, "ingest_view", ingest)
    monkeypatch.setattr(
        scenario.app.state.runtime,
        "list_destination_views",
        lambda: [{"enabled": True, "state": "running"}],
    )
    for now in range(0, 16, 2):
        main["bytes_received"] += 1_000_000
        scenario.status(float(now))
    main["bitrate_bps"] = 0
    scenario.status(16)
    tracker = scenario.app.state.relay_quality
    samples = tracker.route_sample_count("main")
    ema = tracker.ema_for("main")
    stalled = scenario.status(47)
    assert stalled["current_route"]["route_id"] == "main"
    assert stalled["health"]["level"] == "black"
    assert "media_stalled" in stalled["health"]["reason_codes"]
    assert tracker.route_sample_count("main") == samples
    assert tracker.ema_for("main") == ema


@pytest.mark.parametrize("output_state", ["waiting_for_input", "stopped"])
def test_main_input_loss_does_not_invent_independent_process_stop_evidence(
    hud_scenario: HudScenario,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    scenario = hud_scenario
    main: dict[str, Any] = {
        "state": "live",
        "bytes_received": 1_000_000,
        "bitrate_bps": 4_000_000,
    }
    destination: dict[str, Any] = {"enabled": True, "state": "live"}

    async def ingest() -> dict[str, Any]:
        return dict(main)

    monkeypatch.setattr(scenario.app.state.runtime, "ingest_view", ingest)
    monkeypatch.setattr(
        scenario.app.state.runtime,
        "list_destination_views",
        lambda: [destination],
    )
    assert scenario.status(0)["current_route"]["route_id"] == "main"
    main.update(state="offline", bitrate_bps=None)
    destination["state"] = output_state
    lost = scenario.status(2)
    assert lost["stream_state"] == "active"
    assert lost["current_route"]["route_id"] == "main"
    assert lost["health"]["level"] == "black"
    assert lost["recommendation"]["reason_code"] == "recovery_grace"
    assert "заставку" not in lost["health"]["message"]
    main["state"] = "error"
    unknown = scenario.status(4)
    assert unknown["stream_state"] == "unknown"
    assert unknown["health"]["reason_codes"] == ["telemetry_unavailable"]
    assert unknown["recommendation"]["action"] == "watch"


def test_hud_page_only_exposes_validated_pairing_boolean(hud_scenario: HudScenario) -> None:
    client = hud_scenario.client
    token = client.cookies.get(HUD_SESSION_COOKIE)
    assert token
    page = client.get("/moblin-hud")
    assert 'data-hud-paired="true"' in page.text
    assert token not in page.text
    client.cookies.clear()
    client.cookies.set(
        HUD_SESSION_COOKIE, "unvalidated-cookie", domain="testserver.local", path="/"
    )
    page = client.get("/moblin-hud")
    assert 'data-hud-paired="false"' in page.text
    assert "unvalidated-cookie" not in page.text
