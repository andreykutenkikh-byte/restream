"""Real HTTPS HUD entry point with an isolated API, evaluator and synthetic node.

The optional browser group is intentionally separate from the ordinary Python gate.
When explicitly enabled, missing Playwright or either engine is a hard failure.
No responses from the HUD API are stubbed and no media or production node is used.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import app.moblin_hud_api as hud_api
from app.core.config import Settings
from app.main import create_app
from app.moblin_hud_api import HUD_SESSION_COOKIE
from app.services.mediamtx import IngestState, IngestStatus

pytestmark = pytest.mark.skipif(
    os.environ.get("ADOJAPAN_HUD_BROWSER_SMOKE") != "1",
    reason="Explicit browser gate: ADOJAPAN_HUD_BROWSER_SMOKE=1 with the browser group",
)


class NoMedia:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


class CapturedLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@dataclass
class HudServer:
    app: Any
    origin: str
    client: httpx.Client
    token: str = field(repr=False)
    clock: float = 0.0
    base_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    access_logs: CapturedLogs = field(default_factory=CapturedLogs)
    application_logs: CapturedLogs = field(default_factory=CapturedLogs)

    def now(self) -> datetime:
        return self.base_time + timedelta(seconds=self.clock)

    def heartbeat_age(self, value: str | None) -> float | None:
        return (
            max(0.0, (self.now() - datetime.fromisoformat(value)).total_seconds())
            if value
            else None
        )

    def heartbeat(self, source: str, *, advance: float = 2.0) -> None:
        self.clock += advance
        live = source == "LIVE"
        payload = {
            "agent_version": "1.0.0",
            "protocol_version": 1,
            "hostname": "browser-fixture",
            "relay": {
                "service_state": "active",
                "enabled": False,
                "main_process": "running",
                "srt_listener": "listening",
                "source": source,
                "input_bitrate_bps": 4_000_000 if live else None,
                "youtube_forward": "active",
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
                "cpu_percent": 10.0,
                "memory_total_bytes": 2_000_000_000,
                "memory_available_bytes": 1_000_000_000,
                "disk_total_bytes": 20_000_000_000,
                "disk_free_bytes": 10_000_000_000,
            },
            "current_command_id": None,
        }
        response = self.client.post(
            "/relay-agent/v1/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert response.status_code == 200


def _tls_files(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, certificate_path = directory / "fixture.key", directory / "fixture.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, certificate_path


@pytest.fixture
def hud_server(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[HudServer]:
    key_path, certificate_path = _tls_files(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
        secure_settings = replace(
            settings, public_domain="127.0.0.1", public_control_url=origin, cookie_secure=True
        )
        app = create_app(secure_settings, mediamtx=NoMedia())  # type: ignore[arg-type]
        tls_context = ssl.create_default_context(cafile=str(certificate_path))
        with httpx.Client(
            base_url=origin, verify=tls_context, trust_env=False, timeout=5
        ) as client:
            fixture = HudServer(app=app, origin=origin, client=client, token="")
            app.state.relays.clock = fixture.now
            app.state.nodes.clock = fixture.now
            monkeypatch.setattr(hud_api, "monotonic", lambda: fixture.clock)
            monkeypatch.setattr(hud_api, "_heartbeat_age", fixture.heartbeat_age)
            access_logger = logging.getLogger("uvicorn.access")
            old_access_level = access_logger.level
            access_logger.setLevel(logging.INFO)
            access_logger.addHandler(fixture.access_logs)
            app_logger = logging.getLogger("app")
            old_app_level = app_logger.level
            app_logger.setLevel(logging.INFO)
            app_logger.addHandler(fixture.application_logs)
            config = uvicorn.Config(
                app,
                ssl_keyfile=str(key_path),
                ssl_certfile=str(certificate_path),
                log_config=None,
                access_log=True,
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                for _ in range(200):
                    if server.started:
                        break
                    assert thread.is_alive(), "Isolated HTTPS server exited before readiness"
                    threading.Event().wait(0.025)
                assert server.started, "Isolated HTTPS server did not become ready"
                grant = app.state.relays.provision_node(
                    display_name="Fixture relay", address="browser-fixture.internal.example"
                )
                fixture.token = grant.node_token
                fixture.heartbeat("SLATE")
                yield fixture
            finally:
                # Release the fixture's persistent TLS connection before waiting
                # for the server's graceful connection drain.
                client.close()
                server.should_exit = True
                thread.join(timeout=10)
                access_logger.removeHandler(fixture.access_logs)
                access_logger.setLevel(old_access_level)
                app_logger.removeHandler(fixture.application_logs)
                app_logger.setLevel(old_app_level)
                assert not thread.is_alive(), "Isolated HTTPS server failed to stop"


@pytest.fixture(params=["chromium", "webkit"])
def browser(request: pytest.FixtureRequest) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail("Enabled browser gate requires: uv sync --locked --group browser")
    with sync_playwright() as playwright:
        # No skip/fallback here: a required engine that cannot launch fails CI.
        engine = getattr(playwright, request.param).launch(headless=True)
        try:
            yield engine
        finally:
            engine.close()


def _poll(page: Any) -> dict[str, Any]:
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
    ) as pending:
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    response = pending.value
    assert response.status == 200
    payload = response.json()
    page.wait_for_function(
        "level => document.body.dataset.hudState === level", arg=payload["health"]["level"]
    )
    return payload


def _soundless_fetch_probe() -> str:
    return """(() => {
      const fetch = window.fetch.bind(window);
      const probe = window.__hudSmoke = { active: 0, maximum: 0, statuses: 0 };
      window.fetch = async (...args) => {
        if (args[0] !== '/moblin-hud/api/status') return fetch(...args);
        probe.active += 1;
        probe.maximum = Math.max(probe.maximum, probe.active);
        probe.statuses += 1;
        try {
          const response = await fetch(...args);
          await response.clone().arrayBuffer();
          if (probe.hold) {
            probe.held = true;
            await new Promise(resolve => { probe.release = resolve; });
          }
          return response;
        } finally { probe.active -= 1; }
      };
    })();"""


def test_ordinary_hud_browser_contract(
    browser: Any, hud_server: HudServer, admin_password: str
) -> None:
    fixture = hud_server
    login = fixture.client.post(
        "/api/auth/login",
        json={"login": fixture.app.state.settings.admin_login, "password": admin_password},
    )
    assert login.status_code == 200
    admin_headers = {"Origin": fixture.origin, "X-CSRF-Token": login.json()["csrf_token"]}
    pairing_response = fixture.client.post(
        "/api/moblin-hud/pairings", json={}, headers=admin_headers
    )
    assert pairing_response.status_code == 200
    pairing = pairing_response.json()
    pair_token = urlsplit(pairing["pairing_url"]).fragment.removeprefix("pair=")
    assert pair_token
    request_urls: list[str] = []
    page_errors: list[str] = []
    console_errors: list[tuple[str, str, str]] = []
    phase = {"name": "normal"}
    with browser.new_context(ignore_https_errors=True) as context:
        context.add_init_script(_soundless_fetch_probe())
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.on("request", lambda request: request_urls.append(request.url))
        page.on("pageerror", lambda error: page_errors.append(error.message))
        page.on(
            "console",
            lambda message: (
                console_errors.append(
                    (phase["name"], message.text, message.location.get("url", ""))
                )
                if message.type == "error"
                else None
            ),
        )
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as first:
            navigation = page.goto(pairing["pairing_url"], wait_until="load")
        assert navigation.status == 200
        assert first.value.status == 200
        initial = first.value.json()
        assert initial["stream_state"] == "idle"
        assert page.locator('script[src*="moblin-hud.js"]').count() == 1
        assert page.url == fixture.origin + "/moblin-hud"
        cookies = [cookie for cookie in context.cookies() if cookie["name"] == HUD_SESSION_COOKIE]
        assert len(cookies) == 1
        cookie = cookies[0]
        assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict"
        assert cookie["path"] == "/"
        assert context.request.get(fixture.origin + "/api/nodes").status == 401
        assert not page_errors and not console_errors

        # A duplicate ordinary script evaluation must reuse the document's controller.
        page.evaluate(
            "window.__hudSmoke.instance = document[Symbol.for('adojapan.moblinHud.instance')]"
        )
        page.add_script_tag(url=fixture.origin + "/static/moblin-hud.js")
        assert page.evaluate(
            "window.__hudSmoke.instance === document[Symbol.for('adojapan.moblinHud.instance')]"
        )
        assert len([url for url in request_urls if urlsplit(url).path.endswith("/api/pair")]) == 1
        for _ in range(8):
            fixture.heartbeat("LIVE")
            status = _poll(page)
        assert status["health"]["level"] == "green"
        assert page.locator("[data-hud-source]").inner_text() == "Moblin LIVE"
        assert page.locator("[data-hud-bitrate]").inner_text() == "4 Мбит/с"

        # Keep a real response unsettled while visibility changes invalidate it.
        # The browser must not open another slot until that old promise settles.
        page.evaluate("window.__hudSmoke.hold = true")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("window.__hudSmoke.held === true")
        held_requests = page.evaluate("window.__hudSmoke.statuses")
        page.evaluate(
            "document.dispatchEvent(new Event('visibilitychange'));"
            "document.dispatchEvent(new Event('visibilitychange'))"
        )
        assert page.evaluate("window.__hudSmoke.statuses") == held_requests
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as settled:
            page.evaluate("window.__hudSmoke.hold = false; window.__hudSmoke.release()")
        assert settled.value.status == 200
        assert page.evaluate("window.__hudSmoke.maximum") == 1

        fixture.heartbeat("SLATE")
        lost = _poll(page)
        assert lost["stream_state"] == "active" and lost["health"]["level"] == "black"
        assert lost["recommendation"]["reason_code"] == "recovery_grace"
        assert lost["recommendation"]["action"] == "watch"
        assert page.locator("[data-hud-title]").inner_text() == "Входящее видео пропало"
        assert "Сервер передаёт заставку" in page.locator("[data-hud-message]").inner_text()
        fixture.heartbeat("LIVE")
        recovered = _poll(page)
        assert recovered["recommendation"]["action"] in {"watch", "stay"}
        assert recovered["recommendation"]["reason_code"] != "recovery_grace"
        for _ in range(3):
            fixture.heartbeat("LIVE")
            _poll(page)

        fixture.heartbeat("NONE")
        loss = _poll(page)
        assert loss["stream_state"] == "active"
        assert loss["recommendation"]["reason_code"] == "recovery_grace"
        fixture.heartbeat("NONE", advance=119)
        assert _poll(page)["recommendation"]["action"] == "watch"
        fixture.heartbeat("NONE", advance=2)
        assert _poll(page)["recommendation"]["action"] == "reconnect"
        fixture.heartbeat("LIVE")
        assert _poll(page)["recommendation"]["action"] in {"watch", "stay"}
        assert page.evaluate("window.__hudSmoke.maximum") == 1

        # The same consumed link now carries a valid cookie; no replay POST/401.
        before_pair_requests = sum(urlsplit(url).path.endswith("/api/pair") for url in request_urls)
        page.wait_for_function("window.__hudSmoke.active === 0")
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as reopened:
            page.goto(pairing["pairing_url"], wait_until="load")
        assert reopened.value.status == 200
        assert (
            sum(urlsplit(url).path.endswith("/api/pair") for url in request_urls)
            == before_pair_requests
        )
        assert page.url == fixture.origin + "/moblin-hud"
        assert not page_errors and not console_errors

        # Exercise persisted-page lifecycle in the real engine, plus a real navigation return.
        page.wait_for_function("window.__hudSmoke.active === 0")
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
        page.wait_for_function("window.__hudSmoke.active === 0")
        stopped_requests = page.evaluate("window.__hudSmoke.statuses")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        assert page.evaluate("window.__hudSmoke.statuses") == stopped_requests
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as resumed:
            page.evaluate(
                "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))"
            )
        assert resumed.value.status == 200
        page.wait_for_function("window.__hudSmoke.active === 0")
        page.goto("about:blank")
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as returned:
            page.go_back(wait_until="load")
        assert returned.value.status == 200

        # Abort one actual browser request. Only this fault window may emit its resource error.
        phase["name"] = "network"
        page.route("**/moblin-hud/api/status", lambda route: route.abort("failed"), times=1)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.body.dataset.hudState === 'monitoring'")
        assert "Не переключайте сервер" in page.locator("[data-hud-recommendation]").inner_text()
        # Actual bounded retry must recover without a manual visibility kick.
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as retried:
            fixture.heartbeat("LIVE")
        assert retried.value.status == 200
        page.wait_for_function("document.body.dataset.hudState !== 'monitoring'")
        phase["name"] = "normal"
        assert page.evaluate("window.__hudSmoke.maximum") == 1

        phase["name"] = "revoke"
        revoked = fixture.client.post(
            f"/api/moblin-hud/devices/{pairing['device_id']}/revoke", headers=admin_headers
        )
        assert revoked.status_code == 200
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.body.dataset.hudState === 'revoked'")
        assert page.locator("[data-hud-title]").inner_text() == "Доступ HUD отключён"
        page.wait_for_function("window.__hudSmoke.active === 0")
        requests_at_revoke = page.evaluate("window.__hudSmoke.statuses")
        page.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));"
            "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));"
            "document.dispatchEvent(new Event('visibilitychange'))"
        )
        assert page.evaluate("window.__hudSmoke.statuses") == requests_at_revoke
        assert not page_errors
        for fault_phase, message, url in console_errors:
            assert fault_phase in {"network", "revoke"}, (fault_phase, message, url)
            assert urlsplit(url).path == "/moblin-hud/api/status", (message, url)
            if fault_phase == "network":
                assert any(marker in message.lower() for marker in ("failed", "load", "network"))
            else:
                assert "401" in message or "unauthorized" in message.lower()
        assert fixture.access_logs.messages, "Access logs must be captured, not disabled"
        all_logs = "\n".join(fixture.access_logs.messages + fixture.application_logs.messages)
        assert "/moblin-hud/api/pair" in all_logs and "/moblin-hud/api/status" in all_logs
        leaked = any(
            secret in all_logs or any(secret in url for url in request_urls)
            for secret in (pair_token, cookie["value"], fixture.token)
        )
        assert not leaked, "Synthetic HUD credential appeared in a request URL or captured log"
        assert all(not urlsplit(url).query for url in request_urls if "/moblin-hud/api/" in url)
