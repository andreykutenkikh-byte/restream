"""Broadcast UI against the real isolated HTTPS backend in both required engines."""

from __future__ import annotations

import os
from typing import Any

import pytest
import test_moblin_hud_browser as hud_fixtures
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_moblin_hud_browser import HudServer

from app.broadcast.envelope import public_key
from app.broadcast.media_control import MediaHeartbeat, MediaNodeEnable
from app.broadcast.models import CAPABILITIES, ResourceLimits

browser = hud_fixtures.browser
hud_server = hud_fixtures.hud_server

pytestmark = pytest.mark.skipif(
    os.environ.get("ADOJAPAN_HUD_BROWSER_SMOKE") != "1", reason="Explicit Chromium + WebKit gate"
)


def test_broadcast_forms_independent_outputs_and_reload_secrecy(
    browser: Any,
    hud_server: HudServer,
    admin_password: str,
) -> None:
    context = browser.new_context(ignore_https_errors=True)
    try:
        media_key = X25519PrivateKey.generate()
        node_id = hud_server.app.state.relays.authenticate(hud_server.token)["node_id"]
        hud_server.app.state.broadcast_media.enable(
            node_id,
            MediaNodeEnable(
                public_key=public_key(media_key),
                srt_host="127.0.0.1",
                srt_port=19000,
                limits=ResourceLimits(),
            ),
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(hud_server.origin + "/login")
        page.locator('[name="login"]').fill("admin")
        page.locator('[name="password"]').fill(admin_password)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(hud_server.origin + "/")
        page.goto(hud_server.origin + "/broadcasts")
        page.locator('#session-form [name="name"]').fill("Tokyo walk")
        page.locator("#session-form button").click()
        page.get_by_role("heading", name="Tokyo walk").wait_for()
        for suffix in ("One", "Two"):
            page.locator('#output-form [name="name"]').fill(f"Output {suffix}")
            page.locator('[name="stream_key"]').fill(f"synthetic-secret-{suffix}")
            page.locator("#output-form button").click()
            page.get_by_role("heading", name=f"Output {suffix}").wait_for()
            assert page.locator('[name="stream_key"]').input_value() == ""
        hud_server.app.state.broadcast_media.heartbeat(
            node_id,
            MediaHeartbeat(
                boot_id="synthetic-browser-boot",
                sequence=1,
                public_key=public_key(media_key),
                capabilities=sorted(CAPABILITIES),
                plan_generation=0,
            ),
        )
        page.locator(".broadcast-output").first.get_by_role(
            "button", name="Начать передачу", exact=True
        ).click()
        page.locator(".broadcast-output").first.get_by_text(
            "START_REQUESTED", exact=True
        ).wait_for()
        page.reload()
        page.get_by_role("heading", name="Output Two").wait_for()
        assert "synthetic-secret" not in page.content()
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert "UNKNOWN" in page.locator("#broadcast-sessions").inner_text()
        assert (
            page.locator(".broadcast-output").nth(1).get_by_text("READY", exact=True).count() == 1
        )
        # Selection targets only the checked output and survives ordinary status renders.
        page.get_by_label("Выбрать Output One", exact=True).check()
        page.get_by_role("button", name="Остановить выбранные", exact=True).click()
        page.locator(".broadcast-output").first.get_by_text("STOP_REQUESTED", exact=True).wait_for()
        assert page.get_by_label("Выбрать Output One", exact=True).is_checked()
        assert (
            page.locator(".broadcast-output").nth(1).get_by_text("READY", exact=True).count() == 1
        )
        page.get_by_role("button", name="Запустить выбранные", exact=True).click()
        page.locator(".broadcast-output").first.get_by_text(
            "START_REQUESTED", exact=True
        ).wait_for()
        assert (
            page.locator(".broadcast-output").nth(1).get_by_text("READY", exact=True).count() == 1
        )
        assert errors == []
    finally:
        context.close()
