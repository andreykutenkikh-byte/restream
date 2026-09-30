"""Broadcast UI against the real isolated HTTPS backend in both required engines."""

from __future__ import annotations

import os
from typing import Any

import pytest
import test_moblin_hud_browser as hud_fixtures
from test_moblin_hud_browser import HudServer

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
        assert errors == []
    finally:
        context.close()
