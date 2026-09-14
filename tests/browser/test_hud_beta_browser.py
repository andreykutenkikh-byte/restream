"""Run the actual beta CLI and both browsers through an unaccelerated demo cycle.

This is part of the opt-in HUD browser gate, not the ordinary Python test gate.
No API response, application clock, heartbeat age, or alert policy is replaced.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.api import SESSION_COOKIE
from app.moblin_hud_api import HUD_SESSION_COOKIE
from scripts.hud_beta import SYNTHETIC_PASSWORD

pytestmark = pytest.mark.skipif(
    os.environ.get("ADOJAPAN_HUD_BROWSER_SMOKE") != "1",
    reason="Explicit browser gate: ADOJAPAN_HUD_BROWSER_SMOKE=1 with the browser group",
)

ROOT = Path(__file__).resolve().parents[2]


class SecretLogCheck:
    """Keep pytest failure argument reprs from exposing ephemeral credentials."""

    def __init__(self) -> None:
        self.values: list[str] = [SYNTHETIC_PASSWORD]

    def __repr__(self) -> str:
        return "SecretLogCheck(<redacted>)"

    def contains_secret(self, text: str) -> bool:
        return any(value in text for value in self.values)


def _control_snapshot(database: Path) -> tuple[Any, ...]:
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        return tuple(
            tuple(connection.execute(query))
            for query in (
                "SELECT * FROM relay_commands",
                "SELECT * FROM node_commands",
                "SELECT * FROM node_install_jobs",
                "SELECT * FROM destinations",
                "SELECT * FROM ingest_config",
            )
        )


async def _until(predicate: Callable[[], bool], *, seconds: float, message: str) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.1)


async def _exercise_browser(
    name: str,
    browser: Any,
    origin: str,
    started: float,
    secrets: SecretLogCheck,
    artifacts: Path,
) -> None:
    admin_context = await browser.new_context(ignore_https_errors=True)
    hud_context = await browser.new_context(
        ignore_https_errors=True, viewport={"width": 420, "height": 900}
    )
    page_errors: list[str] = []
    requests: list[tuple[str, str]] = []
    hud_requests: list[tuple[str, str]] = []
    admin_responses: list[tuple[str, str, int]] = []

    async def status_snapshot() -> dict[str, Any]:
        # Read the real HTTPS API only after the real page renders each phase.
        # Reading here avoids engine-specific delivery delays in response callbacks.
        response = await hud_context.request.get(origin + "/moblin-hud/api/status")
        assert response.status == 200
        payload = await response.json()
        route = payload.get("current_route") or {}
        return {
            "time": time.monotonic() - started,
            "level": payload["health"]["level"],
            "reasons": payload["health"]["reason_codes"],
            "source": route.get("source"),
            "bitrate": route.get("input_bitrate_bps"),
            "heartbeat_age": route.get("heartbeat_age_seconds"),
        }

    try:
        admin = await admin_context.new_page()
        admin.on("pageerror", lambda error: page_errors.append(error.message))
        admin.on("request", lambda request: requests.append((request.method, request.url)))
        admin.on(
            "response",
            lambda response: admin_responses.append(
                (
                    response.request.method,
                    urlsplit(response.url).path,
                    response.status,
                )
            ),
        )
        await admin.goto(origin + "/login")
        await admin.locator("#login").fill("beta")
        await admin.locator("#password").fill(SYNTHETIC_PASSWORD)
        await admin.locator('[data-login-form] button[type="submit"]').click()
        await admin.wait_for_url(origin + "/")
        assert await admin.locator('script[src*="moblin-hud-admin.js"]').count() == 1
        async with admin.expect_response(
            lambda response: urlsplit(response.url).path == "/api/moblin-hud/pairings"
        ) as pending:
            await admin.locator("[data-hud-create-pairing]").click()
        pairing_response = await pending.value
        assert pairing_response.status == 200
        pairing = await pairing_response.json()
        pairing_url = await admin.locator("[data-hud-pairing-url]").input_value()
        pair_token = urlsplit(pairing_url).fragment.removeprefix("pair=")
        assert pair_token
        secrets.values.append(pair_token)
        moblin_link = await admin.locator("[data-hud-moblin-link]").get_attribute("href")
        assert moblin_link and moblin_link.startswith("moblin://?")
        secrets.values.extend(cookie["value"] for cookie in await admin_context.cookies())

        hud = await hud_context.new_page()
        hud.on("pageerror", lambda error: page_errors.append(error.message))
        hud.on("request", lambda request: requests.append((request.method, request.url)))
        hud.on("request", lambda request: hud_requests.append((request.method, request.url)))
        try:
            async with hud.expect_response(
                lambda response: urlsplit(response.url).path == "/moblin-hud/api/pair"
            ) as paired:
                await hud.goto(pairing_url)
        except Exception:
            # Playwright navigation exceptions otherwise include the secret fragment.
            raise AssertionError("HUD pairing navigation failed") from None
        await hud.wait_for_url(origin + "/moblin-hud")
        assert await hud.locator('script[src*="moblin-hud.js"]').count() == 1
        cookies = await hud_context.cookies()
        assert all(cookie["name"] != SESSION_COOKIE for cookie in cookies)
        hud_cookies = [cookie for cookie in cookies if cookie["name"] == HUD_SESSION_COOKIE]
        assert len(hud_cookies) == 1
        cookie = hud_cookies[0]
        secrets.values.append(cookie["value"])
        strict_header = (
            "samesite=strict"
            in ((await (await paired.value).header_value("set-cookie")) or "").lower()
        )
        assert strict_header, "HUD pairing did not issue SameSite=Strict"
        assert cookie["secure"] and cookie["httpOnly"]
        # Exercise the actual SameSite boundary; Windows WebKit's cookie metadata
        # can differ from the server header, so API metadata is not the proof.
        cross_site = await hud_context.new_page()
        await cross_site.goto(origin.replace("127.0.0.1", "localhost") + "/login")
        await cross_site.evaluate(
            "url => { const a = document.createElement('a'); a.href = url; "
            "a.id = 'cross-site-check'; a.textContent = 'Check'; document.body.append(a); }",
            origin + "/moblin-hud/api/status",
        )
        async with cross_site.expect_response(
            lambda response: urlsplit(response.url).path == "/moblin-hud/api/status"
        ) as cross_site_response:
            await cross_site.locator("#cross-site-check").click()
        cross_site_result = await cross_site_response.value
        request_headers = await cross_site_result.request.all_headers()
        assert request_headers.get("sec-fetch-site") == "cross-site"
        if cross_site_result.status != 401:
            metadata = {
                key: request_headers.get(key)
                for key in ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")
            }
            metadata.update(
                {
                    key + "_host": urlsplit(request_headers.get(key, "")).hostname
                    for key in ("origin", "referer")
                }
            )
            print(f"BETA {name} cross-site response={cross_site_result.status}; {metadata}")
        assert cross_site_result.status == 401, "Cross-site navigation authenticated HUD"
        await cross_site.close()
        assert (await hud_context.request.get(origin + "/api/nodes")).status == 401

        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'green'", timeout=45_000
        )
        normal = await status_snapshot()
        assert normal["source"] == "LIVE" and normal["bitrate"] == 4_000_000
        assert await hud.locator("[data-hud-bitrate]").inner_text() == "4 Мбит/с"
        assert "SYNTHETIC" in await hud.locator("[data-hud-server]").inner_text()
        await hud.screenshot(path=str(artifacts / f"{name}-normal.png"), full_page=True)

        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'unknown' && "
            "document.querySelector('[data-hud-title]').textContent === 'Нет свежей телеметрии'",
            timeout=90_000,
        )
        loss = await status_snapshot()
        assert "telemetry_unavailable" in loss["reasons"]
        assert loss["source"] == "UNKNOWN" and loss["bitrate"] is None
        assert loss["heartbeat_age"] > 30
        await hud.wait_for_function("() => document.body.dataset.hudState === 'unknown'")
        assert await hud.locator("[data-hud-source]").inner_text() == "Нет данных"
        assert await hud.locator("[data-hud-trend]").inner_text() == "Динамика уточняется"
        assert "Потеря мониторинга не означает остановку эфира" in (
            await hud.locator("[data-hud-message]").inner_text()
        )
        await hud.screenshot(path=str(artifacts / f"{name}-telemetry-loss.png"), full_page=True)

        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'green'", timeout=60_000
        )
        recovered = await status_snapshot()
        assert recovered["source"] == "LIVE" and recovered["bitrate"] == 4_000_000
        assert recovered["heartbeat_age"] < 10
        await hud.screenshot(path=str(artifacts / f"{name}-recovered.png"), full_page=True)

        # A separate actual browser network fault must use the connection warning,
        # then recover through ordinary retry without a visibility/polling kick.
        await hud_context.set_offline(True)
        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'monitoring'", timeout=15_000
        )
        assert (
            "Не переключайте сервер" in await hud.locator("[data-hud-recommendation]").inner_text()
        )
        await hud_context.set_offline(False)
        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'green'", timeout=15_000
        )

        # Check the beta boundary through an authenticated admin connection.
        denied = await admin_context.request.post(origin + "/api/ingest/rotate", data={})
        assert denied.status == 403
        assert (await denied.json())["error"]["code"] == "beta_read_only"
        await admin.reload()
        await admin.locator(f"[data-hud-revoke='{pairing['device_id']}']").click()
        await hud.wait_for_function(
            "() => document.body.dataset.hudState === 'revoked'", timeout=10_000
        )
        assert (await hud_context.request.get(origin + "/moblin-hud/api/status")).status == 401
        assert not page_errors, "A real panel/HUD entrypoint raised a JavaScript error"
        unexpected_origins = {
            (
                urlsplit(url).scheme,
                urlsplit(url).hostname,
                urlsplit(url).path
                if urlsplit(url).scheme in {"http", "https"}
                else "<non-network>",
            )
            for _, url in requests
            if urlsplit(url).hostname != "127.0.0.1"
        }
        assert not unexpected_origins, unexpected_origins
        allowed_posts = {
            "/api/auth/login",
            "/api/moblin-hud/pairings",
            "/moblin-hud/api/pair",
            f"/api/moblin-hud/devices/{pairing['device_id']}/revoke",
        }
        # main's admin dashboard requests its existing preview lease automatically.
        # The synthetic beta rejects it; no HUD request may touch preview at all.
        blocked_preview_paths = {
            path
            for method, path, status in admin_responses
            if method == "POST"
            and status == 403
            and re.fullmatch(r"/api/nodes/[A-Za-z0-9-]+/relay/preview/lease", path)
        }
        unexpected_calls = [
            (method, urlsplit(url).path)
            for method, url in requests
            if method != "GET" and urlsplit(url).path not in allowed_posts | blocked_preview_paths
        ]
        assert not unexpected_calls, unexpected_calls
        assert not any("/preview" in urlsplit(url).path for _, url in hud_requests)
        leaked = any(secrets.contains_secret(url) for _, url in requests)
        assert not leaked, "A credential appeared in a browser request URL"
        print(
            f"BETA {name}: normal={normal['time']:.1f}s, "
            f"telemetry_loss={loss['time']:.1f}s (age={loss['heartbeat_age']:.1f}s), "
            f"recovered={recovered['time']:.1f}s; connection retry and UI revoke passed",
            flush=True,
        )
    finally:
        await hud_context.close()
        await admin_context.close()


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
async def test_standalone_beta_real_time(tmp_path: Path, engine_name: str) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.fail("Enabled browser gate requires: uv sync --locked --group browser")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    artifacts = (
        ROOT / "logs" / "hud-beta-browser"
        if os.environ.get("ADOJAPAN_HUD_BETA_ARTIFACTS") == "1"
        else tmp_path / "screenshots"
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    output_path = tmp_path / "beta-output.txt"
    environment = {**os.environ, "TMP": str(runtime), "TEMP": str(runtime), "TMPDIR": str(runtime)}
    environment["PYTHONIOENCODING"] = "utf-8"
    secrets = SecretLogCheck()

    async with async_playwright() as playwright:
        # Both parameters are mandatory in CI. Launch before the first fixture phase.
        browser = await getattr(playwright, engine_name).launch(headless=True)
        try:
            with output_path.open("w", encoding="utf-8") as output:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "-m",
                        "scripts.hud_beta",
                        "--port",
                        "0",
                        "--duration",
                        "150",
                        "--synthetic-password",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                started = time.monotonic()
                try:
                    await _until(
                        lambda: "https://127.0.0.1:" in output_path.read_text(encoding="utf-8"),
                        seconds=15,
                        message="Standalone beta did not print its loopback HTTPS URL",
                    )
                    text = output_path.read_text(encoding="utf-8")
                    match = re.search(r"https://127\.0\.0\.1:[0-9]+", text)
                    assert match is not None
                    origin = match.group(0)
                    databases = list(runtime.rglob("hud-beta.sqlite3"))
                    assert len(databases) == 1
                    database = databases[0]
                    initial = _control_snapshot(database)
                    assert initial[:4] == ((), (), (), ())
                    await _exercise_browser(
                        engine_name,
                        browser,
                        origin,
                        started,
                        secrets,
                        artifacts,
                    )
                    assert _control_snapshot(database) == initial
                    await _until(
                        lambda: process.poll() is not None,
                        seconds=60,
                        message="The beta duration limit did not shut down its process",
                    )
                    assert process.returncode == 0, "Standalone beta returned failure"
                    assert not database.exists(), "Standalone beta left its temporary database"
                    assert not list(runtime.iterdir()), (
                        "Standalone beta left temporary TLS material"
                    )
                    captured = output_path.read_text(encoding="utf-8")
                    assert "telemetry paused" in captured and "LIVE telemetry restored" in captured
                    assert "disposable database and TLS key removed" in captured
                    leaked = secrets.contains_secret(captured)
                    assert not leaked, "A credential appeared in beta console output"
                finally:
                    if process.poll() is None:
                        process.terminate()
                        await asyncio.to_thread(process.wait, timeout=10)
        finally:
            await browser.close()
