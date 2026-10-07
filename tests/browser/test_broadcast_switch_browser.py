"""Real HTTPS admin/operator/monitor pages; only agent telemetry is synthetic."""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import pytest
import test_moblin_hud_browser as hud_fixtures
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import SecretStr
from test_moblin_hud_browser import HudServer

from app.broadcast.envelope import open_envelope, public_key
from app.broadcast.media_control import MediaHeartbeat, MediaNodeEnable, Observation
from app.broadcast.models import (
    CAPABILITIES,
    BroadcastError,
    OutputCreate,
    ResourceLimits,
    SessionCreate,
)
from app.moblin_hud_api import HUD_SESSION_COOKIE

hud_server = hud_fixtures.hud_server
pytestmark = pytest.mark.skipif(
    os.environ.get("ADOJAPAN_HUD_BROWSER_SMOKE") != "1", reason="Explicit Chromium + WebKit gate"
)


@pytest.fixture(params=["chromium", "webkit"])
def browser(request: pytest.FixtureRequest, hud_server: HudServer) -> Iterator[Any]:
    # Dispose the engine (including speculative TLS sockets) before the HTTPS server.
    # Context disposal alone does not own all of the browser's network connections.
    yield from hud_fixtures.browser.__wrapped__(request)


class Scenario:
    def __init__(self, server: HudServer) -> None:
        self.server = server
        self.store, self.media = server.app.state.broadcasts, server.app.state.broadcast_media
        self.keys = {}
        self.sequence = 0
        self.error_target, self.source_lost = False, False
        for name in ("Hong Kong", "Japan"):
            node = server.app.state.relays.provision_node(
                display_name=name, address=secrets.token_hex(8) + ".example"
            ).node_id
            key = X25519PrivateKey.generate()
            self.keys[node] = key
            self.media.enable(
                node,
                MediaNodeEnable(
                    public_key=public_key(key),
                    srt_host="127.0.0.1",
                    srt_port=19000 + len(self.keys),
                    limits=ResourceLimits(),
                ),
            )
        self.a, self.b = self.keys
        self.pulse()
        self.sid = self.store.create_session(
            SessionCreate(name="Scoped broadcast", ingress_node_id=self.a), secrets.token_hex(16)
        )
        self.output = self.store.create_output(
            self.sid,
            OutputCreate(
                name="YouTube LIVE",
                node_id=self.a,
                primary_url="rtmps://a.rtmps.youtube.com/live2",
                backup_url="rtmps://b.rtmps.youtube.com/live2?backup=1",
                stream_key=SecretStr("synthetic-browser-output-key"),
            ),
            secrets.token_hex(16),
        )
        self.target = self.store.add_route(self.output, self.b, secrets.token_hex(16))
        self.store.intent(self.output, True, secrets.token_hex(16))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def pulse(self) -> None:
        self.sequence += 1
        for node, key in self.keys.items():
            if self.sequence == 1:
                self.media.heartbeat(
                    node,
                    MediaHeartbeat(
                        boot_id="switch-browser-boot",
                        public_key=public_key(key),
                        sequence=1,
                        plan_generation=0,
                        capabilities=sorted(CAPABILITIES),
                    ),
                )
                continue
            envelope = self.media.desired(node)
            plan = open_envelope(key, envelope, node)
            measurements = []
            for route in plan["routes"]:
                lease = route["egress_lease"]
                direct = node == self.a and not self.source_lost
                connected = bool(lease and direct)
                measurements.append(
                    Observation(
                        route_id=route["id"],
                        source_kind="direct" if direct else "unknown",
                        source_identity="synthetic-phone" if direct else None,
                        video_pts=float(self.sequence) if direct else None,
                        audio_pts=float(self.sequence) if direct else None,
                        video_frames=self.sequence * 30 if direct else 0,
                        audio_packets=self.sequence * 48 if direct else 0,
                        bitrate_bps=4000000 if direct else None,
                        publisher_frames=self.sequence * 30 if connected else 0,
                        publisher_bytes=self.sequence * 500000 if connected else 0,
                        publisher_connected=connected,
                        publisher_running=bool(lease),
                        runtime_secret_present=bool(lease),
                        egress_generation=route["egress_generation"],
                        egress_lease_id=lease["id"] if lease else None,
                        safe_error_code="publisher_failed"
                        if self.error_target and node == self.b
                        else "source_lost"
                        if self.source_lost and node == self.a
                        else None,
                    )
                )
            self.media.heartbeat(
                node,
                MediaHeartbeat(
                    boot_id="switch-browser-boot",
                    public_key=public_key(key),
                    sequence=self.sequence,
                    plan_generation=envelope["context"]["generation"],
                    capabilities=sorted(CAPABILITIES),
                    observations=measurements,
                ),
            )

    def run(self) -> None:
        while not self.stop.wait(0.5):
            with suppress(BroadcastError):
                self.pulse()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


def test_switch_dialog_idempotency_reload_rollback_and_operator_revoke(
    browser: Any, hud_server: HudServer, admin_password: str
) -> None:
    scenario = Scenario(hud_server)
    admin = browser.new_context(ignore_https_errors=True)
    operator = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    monitor = browser.new_context(ignore_https_errors=True)
    errors = []
    try:
        page = admin.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(hud_server.origin + "/login")
        page.locator('[name="login"]').fill("admin")
        page.locator('[name="password"]').fill(admin_password)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(hud_server.origin + "/")
        page.goto(hud_server.origin + "/broadcasts")
        page.get_by_role("button", name="Переключиться на Japan", exact=True).click()
        dialog = page.get_by_role("dialog")
        assert "Повторный ввод не нужен" in dialog.inner_text()
        mode = dialog.get_by_label("Режим переключения")
        mode.select_option("egress")
        assert "EGRESS_ONLY" in dialog.inner_text()
        assert "Старый ingress остаётся в маршруте" in dialog.inner_text()
        mode.select_option("full")
        assert "FULL_ROUTE" in dialog.inner_text()
        assert "OBS:" in dialog.inner_text() and "Moblin:" in dialog.inner_text()
        dialog.get_by_role("button", name="Подготовить и переключить").evaluate(
            "b => { b.click(); b.click(); }"
        )
        page.locator("dialog").wait_for(state="detached")
        page.reload()
        page.locator(".switch-state").filter(has_text="PREPARING_TARGET").wait_for(timeout=15000)
        with scenario.store.database.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM broadcast_switches").fetchone()[0] == 1
        scenario.error_target = True
        page.locator(".switch-state").filter(has_text="FAILED").wait_for(timeout=20000)
        with scenario.store.database.connect() as db:
            assert (
                db.execute("SELECT node_id FROM broadcast_routes WHERE role='current'").fetchone()[
                    0
                ]
                == scenario.a
            )
        assert "synthetic-browser-output-key" not in page.content()
        page.get_by_role("button", name="Разрешить оператору переключение", exact=True).click()
        pairing_dialog = page.get_by_role("dialog")
        pairing_dialog.get_by_label("Название устройства").fill("Browser operator")
        pairing_dialog.get_by_role("button", name="Создать ссылку", exact=True).click()
        assert pairing_dialog.get_by_role("link", name="Открыть операторский HUD").count() == 0
        pairing_dialog.get_by_role("checkbox").check()
        pairing_dialog.get_by_role("button", name="Создать ссылку", exact=True).click()
        pair_link = pairing_dialog.get_by_role("link", name="Открыть операторский HUD")
        pair_link.wait_for()
        pairing_url = pair_link.get_attribute("href")
        assert pairing_url is not None
        pairing_token = pairing_url.split("#pair=")[1]
        pairing_dialog.get_by_role("button", name="Закрыть", exact=True).click()
        op = operator.new_page()
        op.on("pageerror", lambda error: errors.append(str(error)))
        op.goto(pairing_url)
        op.get_by_role("heading", name="Scoped broadcast").wait_for()
        assert "#" not in op.url
        assert (
            op.locator("#operator-sessions")
            .get_by_role("button", name="Начать передачу", exact=True)
            .count()
            == 0
        )
        assert operator.request.get(hud_server.origin + "/api/broadcasts").status == 401
        assert op.evaluate("localStorage.length + sessionStorage.length") == 0
        assert "synthetic-browser-output-key" not in op.content()
        op.locator('[data-severity="live"]').wait_for()
        scenario.source_lost = True
        op.locator('[data-severity="alert"]').wait_for(timeout=10000)
        scenario.source_lost = False
        op.locator('[data-severity="live"]').wait_for(timeout=10000)
        op.route("**/stream-operator/api/state", lambda route: route.abort())
        op.get_by_text("Мониторинг недоступен.", exact=False).wait_for(timeout=15000)
        op.unroute("**/stream-operator/api/state")
        op.get_by_text("Только разрешённая сессия", exact=False).wait_for(timeout=15000)
        op.reload()
        op.get_by_role("heading", name="Scoped broadcast").wait_for()
        monitor_pair = hud_server.app.state.moblin_hud.create_pairing("Read only")
        grant = hud_server.app.state.moblin_hud.consume_pairing(monitor_pair.pairing_token)
        monitor.add_cookies(
            [
                {
                    "name": HUD_SESSION_COOKIE,
                    "value": grant.session_token,
                    "url": hud_server.origin,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        view = monitor.new_page()
        view.goto(hud_server.origin + "/moblin-hud")
        view.locator("#broadcast-monitor").get_by_role(
            "heading", name="Scoped broadcast"
        ).wait_for()
        assert view.locator("#broadcast-monitor button").count() == 0
        assert monitor.request.get(hud_server.origin + "/stream-operator/api/state").status == 401
        page.get_by_role("button", name="Устройства оператора", exact=True).click()
        devices = page.get_by_role("dialog")
        devices.get_by_role("button", name="Отозвать", exact=True).click()
        devices.get_by_text("Отозван", exact=True).wait_for()
        op.get_by_text("Доступ истёк или отозван.", exact=False).wait_for(timeout=10000)
        assert op.locator("#operator-sessions").inner_text() == ""
        assert errors == []
        assert all(pairing_token not in log for log in hud_server.access_logs.messages)
    finally:
        scenario.close()
        for context in (admin, operator, monitor):
            context.request.dispose()
        admin.close()
        operator.close()
        monitor.close()
