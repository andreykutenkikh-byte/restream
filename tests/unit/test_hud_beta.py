"""Safety and actual application contract of the disposable local beta."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import SESSION_COOKIE
from app.moblin_hud_api import HUD_SESSION_COOKIE
from scripts import hud_beta


def _controls(app: Any) -> tuple[Any, ...]:
    with app.state.database.connect() as connection:
        return (
            tuple(tuple(row) for row in connection.execute("SELECT * FROM relay_commands")),
            tuple(tuple(row) for row in connection.execute("SELECT * FROM node_commands")),
            tuple(tuple(row) for row in connection.execute("SELECT * FROM node_install_jobs")),
            tuple(tuple(row) for row in connection.execute("SELECT * FROM destinations")),
            app.state.database.get_ingest_encrypted(),
        )


def test_real_beta_panel_pairing_and_protocol_never_start_control_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        pytest.fail("Beta attempted a network lookup or subprocess")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # An existing environment must not supply a production database or credentials.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "do-not-open.sqlite3"))
    monkeypatch.setenv("PUBLIC_CONTROL_URL", "https://do-not-contact.invalid")
    monkeypatch.setenv("BOOTSTRAP_WORKER_SECRET", "do-not-use-this-existing-secret")
    app = hud_beta.build_app(tmp_path, 8443, hud_beta.SYNTHETIC_PASSWORD)
    settings = app.state.settings
    assert settings.database_path == tmp_path / "hud-beta.sqlite3"
    assert settings.bootstrap_worker_secret == ""
    assert settings.cookie_secure and settings.public_control_url == "https://127.0.0.1:8443"
    origin = settings.public_control_url

    with TestClient(app, base_url=origin) as client:
        grant = app.state.relays.provision_node(
            display_name="SYNTHETIC beta relay — no video",
            address=hud_beta.HOST,
        )
        initial = _controls(app)
        assert initial[:4] == ((), (), (), ())
        heartbeat = client.post(
            "/relay-agent/v1/heartbeat",
            json=hud_beta.heartbeat_payload(),
            headers={"Authorization": f"Bearer {grant.node_token}"},
        )
        assert heartbeat.status_code == 200
        login = client.post(
            "/api/auth/login",
            json={
                "login": "beta",
                "password": hud_beta.SYNTHETIC_PASSWORD,
            },
        )
        assert login.status_code == 200
        admin_cookie = client.cookies.get(SESSION_COOKIE)
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        for path in ("/", "/servers", "/api/ingest/status", "/api/nodes", "/api/relay-nodes"):
            assert client.get(path).status_code == 200
        assert client.get("/api/ingest/status").json()["state"] == "offline"
        assert client.get("/static/moblin-hud.js").status_code == 200
        pairing = client.post("/api/moblin-hud/pairings", json={}, headers=headers)
        assert pairing.status_code == 200
        pair_token = urlsplit(pairing.json()["pairing_url"]).fragment.removeprefix("pair=")
        client.cookies.clear()
        assert client.get("/moblin-hud").status_code == 200
        paired = client.post(
            "/moblin-hud/api/pair", json={"token": pair_token}, headers={"Origin": origin}
        )
        assert paired.status_code == 200
        hud_cookie = client.cookies.get(HUD_SESSION_COOKIE)
        assert hud_cookie
        assert client.get("/api/nodes").status_code == 401
        status = client.get("/moblin-hud/api/status")
        assert status.status_code == 200
        assert "SYNTHETIC beta relay" in status.text
        for secret in (pair_token, hud_cookie, grant.node_token, hud_beta.SYNTHETIC_PASSWORD):
            assert secret not in status.text
        assert (
            client.post(
                "/moblin-hud/api/pair", json={"token": pair_token}, headers={"Origin": origin}
            ).status_code
            == 401
        )
        assert _controls(app) == initial

        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, admin_cookie)
        assert (
            client.post(
                f"/api/moblin-hud/devices/{pairing.json()['device_id']}/revoke",
                headers=headers,
            ).status_code
            == 200
        )
        client.cookies.clear()
        client.cookies.set(HUD_SESSION_COOKIE, hud_cookie)
        assert client.get("/moblin-hud/api/status").status_code == 401
        assert _controls(app) == initial
    assert not (tmp_path / "do-not-open.sqlite3").exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/nodes/synthetic/relay/start"),
        ("post", "/api/nodes/synthetic/relay/stop"),
        ("post", "/api/nodes/synthetic/relay/refresh"),
        ("put", "/api/nodes/synthetic/relay/configure-youtube"),
        ("post", "/api/ingest/rotate"),
        ("post", "/api/destinations"),
        ("post", "/api/bootstrap/jobs"),
        ("get", "/relay-agent/v1/commands/next"),
        ("get", "/api/ingest/preview/index.m3u8"),
        ("post", "/api/nodes/synthetic/relay/preview"),
        ("post", "/future/control/endpoint"),
    ],
)
def test_beta_rejects_control_media_and_unknown_routes(
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    app = hud_beta.build_app(tmp_path, 8443, hud_beta.SYNTHETIC_PASSWORD)
    with TestClient(app, base_url="https://127.0.0.1:8443") as client:
        before = _controls(app)
        response = client.request(method, path)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "beta_read_only"
        assert _controls(app) == before


async def test_beta_worker_launcher_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        await hud_beta.NoWorkers().spawn(["ffmpeg"])


def test_beta_refuses_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "hud-beta.sqlite3"
    database.write_bytes(b"preserve this existing database")
    with pytest.raises(ValueError, match="fresh database"):
        hud_beta.build_app(tmp_path, 8443, hud_beta.SYNTHETIC_PASSWORD)
    assert database.read_bytes() == b"preserve this existing database"


def test_demo_schedule_is_bounded_and_omits_heartbeats_during_loss(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elapsed = 0.0
    observations: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.url.host == hud_beta.HOST
        assert request.url.path == "/relay-agent/v1/heartbeat"
        observations.append(elapsed)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(hud_beta, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep))
    token = "fixture-token-never-log"
    with httpx.Client(
        base_url="https://127.0.0.1:8443", transport=httpx.MockTransport(receive)
    ) as client:
        hud_beta.run_demo(client, token, duration=135, phase_seconds=45)
    assert elapsed == 135
    assert any(moment < 45 for moment in observations)
    assert not any(45 <= moment < 90 for moment in observations)
    assert any(moment >= 90 for moment in observations)
    assert all(
        right - left >= 5 for left, right in zip(observations, observations[1:], strict=False)
    )
    output = capsys.readouterr().out
    assert "telemetry paused" in output and "LIVE telemetry restored" in output
    assert token not in output and hud_beta.SYNTHETIC_PASSWORD not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "0.0.0.0"],  # noqa: S104 - verify that non-loopback binds are rejected
        ["--port", "-1"],
        ["--duration", "0"],
        ["--duration", "3601"],
        ["--phase-seconds", "5"],
        ["--synthetic-password=secret"],
    ],
)
def test_cli_rejects_unbounded_or_unsafe_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        hud_beta.parse_args(arguments)


def test_public_fixture_password_is_opt_in() -> None:
    assert hud_beta.parse_args([]).synthetic_password is False
    assert hud_beta.parse_args(["--synthetic-password"]).synthetic_password is True
