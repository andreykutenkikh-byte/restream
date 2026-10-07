"""Actual HTML/JS + HTTPS API. Public ingress IPs are metadata only, never dialled.

Only node telemetry is synthetic. No request/response stubbing, media or production.
Screenshots are allowlisted closed-dialog UI; no traces or network dumps are saved.
"""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import test_moblin_hud_browser as hud_fixtures
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_moblin_hud_browser import HudServer

from app.broadcast.envelope import open_envelope, public_key
from app.broadcast.media_control import MediaHeartbeat, MediaNodeEnable, Observation
from app.broadcast.models import CAPABILITIES, BroadcastError, ResourceLimits
from app.db import utc_now

hud_server = hud_fixtures.hud_server
pytestmark = pytest.mark.skipif(
    os.environ.get("ADOJAPAN_HUD_BROWSER_SMOKE") != "1", reason="Explicit browser gate"
)


def expect(locator: Any) -> Any:
    # The ordinary test environment intentionally excludes the browser dependency.
    from playwright.sync_api import expect as browser_expect

    return browser_expect(locator)


@pytest.fixture(params=["chromium", "webkit"])
def browser(request: pytest.FixtureRequest, hud_server: HudServer) -> Iterator[Any]:
    yield from hud_fixtures.browser.__wrapped__(request)


class MediaLab:
    def __init__(self, server: HudServer) -> None:
        self.store, self.media = server.app.state.broadcasts, server.app.state.broadcast_media
        self.keys: dict[str, X25519PrivateKey] = {}
        self.seq = 0
        self.video, self.forward, self.fail = False, False, False
        self._pulse_lock = threading.Lock()
        self._paused = False
        self.failure: BaseException | None = None
        for name, ip in (("Сервер A", "8.8.8.8"), ("Сервер B", "1.1.1.1")):
            node = server.app.state.relays.provision_node(
                display_name=name, address=secrets.token_hex(8) + ".example"
            ).node_id
            key = X25519PrivateKey.generate()
            self.keys[node] = key
            self.media.enable(
                node,
                MediaNodeEnable(
                    public_key=public_key(key), srt_host=ip, srt_port=19000, limits=ResourceLimits()
                ),
            )
        self.a, self.b = self.keys
        self.pulse()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def pulse(self) -> None:
        self.seq += 1
        for node, key in self.keys.items():
            generation, observations = 0, []
            if self.seq > 1:
                envelope = self.media.desired(node)
                plan = open_envelope(key, envelope, node)
                generation = envelope["context"]["generation"]
                for route in plan["routes"]:
                    lease = route["egress_lease"]
                    measured = (
                        route["media_enabled"] and self.video and (node == self.a or self.forward)
                    )
                    connected = bool(lease and measured and not (self.fail and node == self.b))
                    observations.append(
                        Observation(
                            route_id=route["id"],
                            source_kind=("direct" if node == self.a else "forwarded")
                            if measured
                            else "unknown",
                            source_identity="synthetic-source" if measured else None,
                            video_pts=float(self.seq) if measured else None,
                            audio_pts=float(self.seq) if measured else None,
                            video_frames=self.seq * 30 if measured else 0,
                            audio_packets=self.seq * 48 if measured else 0,
                            bitrate_bps=4000000 if measured else None,
                            publisher_connected=connected,
                            publisher_running=bool(lease),
                            runtime_secret_present=bool(lease),
                            publisher_frames=self.seq * 30 if connected else 0,
                            publisher_bytes=self.seq * 500000 if connected else 0,
                            egress_generation=route["egress_generation"],
                            egress_lease_id=lease["id"] if lease else None,
                            safe_error_code="publisher_failed"
                            if self.fail and node == self.b
                            else None,
                        )
                    )
            self.media.heartbeat(
                node,
                MediaHeartbeat(
                    boot_id="synthetic-ui-browser-boot",
                    public_key=public_key(key),
                    sequence=self.seq,
                    plan_generation=generation,
                    rtmp_port=19001,
                    capabilities=sorted(CAPABILITIES),
                    observations=observations,
                ),
            )

    def run(self) -> None:
        try:
            while not self.stop.wait(0.3):
                with self._pulse_lock:
                    if not self._paused:
                        with suppress(BroadcastError):
                            self.pulse()
        except BaseException as exc:
            self.failure = exc

    def set_paused(self, paused: bool) -> None:
        # Wait for any heartbeat already in flight before tests alter telemetry.
        # Otherwise that heartbeat can overwrite deliberately stale observations.
        with self._pulse_lock:
            self._paused = paused

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        assert self.failure is None
        assert not self.thread.is_alive()


def login(page: Any, server: HudServer, password: str) -> None:
    page.goto(server.origin + "/login")
    page.get_by_label("Логин", exact=True).fill("admin")
    page.locator('[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    page.wait_for_url(server.origin + "/")
    expect(page.get_by_role("heading", name="Моя трансляция")).to_be_visible()
    expect(page.locator("#obs-connect")).to_be_enabled()


def connection(page: Any) -> str:
    page.locator("#obs-connect").click()
    field = page.get_by_role("dialog").get_by_label("Сервер", exact=True)
    expect(field).to_be_visible()
    value = field.input_value()
    page.get_by_role("button", name="Скопировать адрес", exact=True).click()
    page.get_by_role("button", name="Закрыть", exact=True).click()
    expect(page.locator("dialog")).to_have_count(0)
    return str(value)


def prepare(page: Any, node: str, name: str = "Моя трансляция") -> None:
    page.locator("#obs-connect").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Название эфира").fill(name)
    dialog.get_by_label("Сервер приёма").select_option(node)
    dialog.get_by_role("button", name="Подготовить подключение", exact=True).evaluate(
        "b => { b.click(); b.click(); }"
    )
    expect(page.get_by_role("dialog").get_by_label("Сервер", exact=True)).to_be_visible()
    page.get_by_role("button", name="Закрыть", exact=True).click()


def save_key(page: Any) -> None:
    page.locator("#youtube-settings").click()
    page.get_by_label("Ключ трансляции YouTube", exact=True).fill("synthetic-browser-ui-key")
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#youtube-state")).to_have_text("Ключ сохранён")
    expect(page.locator("dialog")).to_have_count(0)


def screenshot(page: Any, browser: Any, size: str, scenario: str) -> None:
    expect(page.locator("dialog")).to_have_count(0)
    content = page.content()
    assert "synthetic-browser-ui-key" not in content
    assert "srt://" not in content and "passphrase=" not in content and "#pair=" not in content
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    directory = Path("logs/transmission-ui")
    directory.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(directory / f"{browser.browser_type.name}-{size}-{scenario}.png"), full_page=True
    )


def test_failed_installation_is_not_offered_as_a_legacy_or_ready_route(
    browser: Any,
    hud_server: HudServer,
    admin_password: str,
) -> None:
    store = hud_server.app.state.broadcasts
    node_id = store.snapshot()["nodes"][0]["id"]
    with store.transaction() as db:
        db.execute("UPDATE restream_nodes SET status='failed' WHERE id=?", (node_id,))
        db.execute(
            "INSERT INTO node_install_jobs(id,node_id,state,current_step,safe_error_code,"
            "created_at,updated_at) VALUES "
            "('failed-browser-install',?,'failed','docker_check','docker_install_failed',?,?)",
            (node_id, utc_now(), utc_now()),
        )
    context = browser.new_context(ignore_https_errors=True)
    try:
        page = context.new_page()
        login(page, hud_server, admin_password)
        card = page.locator(f'[data-node-id="{node_id}"]')
        expect(card).to_contain_text("Установка сервера не завершена")
        expect(card).to_contain_text("Не удалось установить Docker")
        expect(card).not_to_contain_text("Legacy")
        expect(card.get_by_role("button")).to_have_count(0)
        expect(card.get_by_role("link", name="Управление сервером")).to_be_visible()
        with store.transaction() as db:
            db.execute("UPDATE restream_nodes SET status='installing' WHERE id=?", (node_id,))
        page.locator("#tx-refresh").click()
        expect(card).to_contain_text("Установка сервера ещё выполняется")
    finally:
        context.close()


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_first_setup_copy_key_switch_reload_and_multiple_choice(
    browser: Any, hud_server: HudServer, admin_password: str, size: str
) -> None:
    context = browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1440, "height": 1100}
        if size == "desktop"
        else {"width": 390, "height": 844},
    )
    lab = None
    try:
        page = context.new_page()
        errors: list[str] = []
        posts: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: posts.append(request.url) if request.method == "POST" else None,
        )
        login(page, hud_server, admin_password)
        expect(page.get_by_role("heading", name="Подключение OBS", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="YouTube", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Сервер передачи", exact=True)).to_be_visible()
        expect(page.locator("#tx-servers")).to_contain_text("Сервер не настроен для передачи видео")
        assert len(posts) == 1  # Login only; page load has no mutation.
        assert hud_server.app.state.broadcasts.snapshot()["sessions"] == []
        lab = MediaLab(hud_server)
        page.locator("#tx-refresh").click()
        expect(page.locator("#tx-servers")).to_contain_text("8.8.8.8")
        prepare(page, lab.a)
        output = lab.store.snapshot()["sessions"][0]["outputs"][0]
        oid = output["id"]
        assert not output["desired_enabled"]
        original = connection(page)
        assert original.startswith("srt://8.8.8.8:19000?")
        assert original == connection(page)
        lab.video = True
        expect(page.locator("#input-status")).to_have_text("Видео поступает", timeout=15000)
        expect(page.locator("#input-bitrate")).to_have_text("4 Мбит/с")
        expect(page.locator("#output-status")).to_have_text("Отправка остановлена")
        with lab.store.database.connect() as db:
            assert not db.execute("SELECT 1 FROM broadcast_egress_leases").fetchone()
        lab.video = False
        expect(page.locator("#input-status")).to_have_text("Ожидаем видео из OBS", timeout=15000)
        with lab.store.database.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM broadcast_sessions").fetchone()[0] == 1
            source_seed = db.execute("SELECT encrypted FROM broadcast_source_secrets").fetchone()[0]
        save_key(page)
        page.reload()
        expect(page.locator("#youtube-state")).to_have_text("Ключ сохранён")
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        page.locator("#send-action").click()
        expect(page.locator("#send-action")).to_have_text("Остановить отправку")
        expect(page.locator("#output-status")).to_have_text("Ожидаем видео из OBS")
        lab.video = True
        expect(page.locator("#output-status")).to_have_text("Отправка работает", timeout=15000)
        expect(page.locator("#input-bitrate")).to_have_text("4 Мбит/с")
        # Unavailable preview does not suppress independent measured live status.
        expect(page.locator("#tx-preview-title")).to_have_text("Предпросмотр недоступен")
        target = page.locator(f'[data-node-id="{lab.b}"]')
        target.get_by_role("button", name="Добавить сервер к эфиру").click()
        target.get_by_role("button", name="Добавить резервный адрес YouTube").click()
        dialog = page.get_by_role("dialog")
        assert dialog.get_by_label("Ключ трансляции YouTube").input_value() == ""
        dialog.get_by_label("Резервный адрес из YouTube Studio").fill(
            "rtmps://b.rtmps.youtube.com/live2?backup=1"
        )
        dialog.get_by_role("button", name="Сохранить", exact=True).click()
        expect(page.locator("dialog")).to_have_count(0)
        with lab.store.database.connect() as db:
            binding = tuple(db.execute("SELECT * FROM youtube_bindings").fetchone())
        target.get_by_role("button", name="Переключить передачу на этот сервер").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("OBS продолжит передавать на прежний сервер")
        dialog.get_by_role("button", name="Подтвердить переключение").evaluate(
            "b => { b.click(); b.click(); }"
        )
        expect(page.locator("dialog")).to_have_count(0)
        expect(page.locator(f'[data-node-id="{lab.a}"]')).to_have_attribute("data-current", "true")
        page.reload()
        expect(page.locator("#tx-operation")).to_contain_text("Подготавливаем", timeout=10000)
        screenshot(page, browser, size, "preparing")
        lab.forward = True
        expect(target).to_have_attribute("data-current", "true", timeout=30000)
        expect(page.locator("#tx-operation")).to_contain_text(
            "Переключение подтверждено", timeout=30000
        )
        assert connection(page) == original
        with lab.store.database.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM broadcast_switches").fetchone()[0] == 1
            assert tuple(db.execute("SELECT * FROM youtube_bindings").fetchone()) == binding
            assert (
                db.execute("SELECT encrypted FROM broadcast_source_secrets").fetchone()[0]
                == source_seed
            )
        screenshot(page, browser, size, "configured")
        old = page.locator(f'[data-node-id="{lab.a}"]')
        old.get_by_text("Сменить также подключение источника", exact=True).click()
        old.get_by_role("button", name="Переключить подключение OBS/Moblin", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text(
            "Потребуется изменить адрес источника и переподключиться"
        )
        page.get_by_role("button", name="Закрыть", exact=True).click()
        # A stale observation must not keep a green input/publisher metric.
        lab.set_paused(True)
        with lab.store.transaction() as db:
            db.execute(
                "UPDATE broadcast_media_observations SET observed_at='2000-01-01T00:00:00+00:00'"
            )
        page.locator("#tx-refresh").click()
        expect(page.locator("#input-bitrate")).to_have_text("Нет данных")
        expect(page.locator("#output-status")).not_to_have_text("Отправка работает")
        lab.set_paused(False)
        page.locator("#add-broadcast").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Название эфира").fill("Второй эфир")
        dialog.get_by_role("button", name="Подготовить подключение", exact=True).click()
        expect(page.get_by_role("dialog").get_by_label("Сервер", exact=True)).to_be_visible()
        page.get_by_role("button", name="Закрыть", exact=True).click()
        page.goto(hud_server.origin + "/")
        expect(page.locator("#broadcast-choice option")).to_have_count(3)
        assert page.locator("#broadcast-choice").input_value() == ""
        expect(page.locator("#send-action")).to_be_disabled()
        page.locator("#broadcast-choice").select_option(oid)
        expect(page.locator("#youtube-state")).to_have_text("Ключ сохранён")
        page.locator("#send-action").click()
        expect(page.get_by_role("dialog")).to_contain_text(
            "Событие на YouTube автоматически не завершается"
        )
        page.get_by_role("dialog").get_by_role(
            "button", name="Остановить отправку", exact=True
        ).click()
        expect(page.locator("#output-status")).to_have_text("Отправка остановлена", timeout=15000)
        expect(page.locator("#send-action")).to_be_enabled()
        assert "synthetic-browser-ui-key" not in page.content()
        assert errors == []
        assert not any("passphrase=" in url for url in posts)
    finally:
        context.close()
        if lab:
            lab.close()


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_failed_target_never_becomes_current_and_capability_gate(
    browser: Any, hud_server: HudServer, admin_password: str, size: str
) -> None:
    lab = MediaLab(hud_server)
    context = browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 900}
        if size == "desktop"
        else {"width": 390, "height": 844},
    )
    try:
        page = context.new_page()
        login(page, hud_server, admin_password)
        prepare(page, lab.a)
        save_key(page)
        page.locator("#send-action").click()
        lab.video = True
        expect(page.locator("#output-status")).to_have_text("Отправка работает", timeout=15000)
        target = page.locator(f'[data-node-id="{lab.b}"]')
        target.get_by_role("button", name="Добавить сервер к эфиру").click()
        target.get_by_role("button", name="Добавить резервный адрес YouTube").click()
        page.get_by_label("Резервный адрес из YouTube Studio").fill(
            "rtmps://b.rtmps.youtube.com/live2?backup=1"
        )
        page.get_by_role("button", name="Сохранить", exact=True).click()
        expect(page.locator("dialog")).to_have_count(0)
        target.get_by_role("button", name="Переключить передачу на этот сервер").click()
        page.get_by_role("button", name="Подтвердить переключение").click()
        expect(page.locator("dialog")).to_have_count(0)
        lab.fail = True
        expect(page.locator("#tx-operation")).to_contain_text(
            "Не удалось переключиться", timeout=30000
        )
        expect(target).to_have_attribute("data-current", "false")
        expect(page.locator(f'[data-node-id="{lab.a}"]')).to_have_attribute("data-current", "true")
        lab.set_paused(True)
        with lab.store.transaction() as db:
            db.execute(
                "UPDATE broadcast_media_nodes SET capabilities_json='[]' WHERE node_id=?", (lab.b,)
            )
        page.locator("#tx-refresh").click()
        expect(target).to_contain_text("Требует настройки")
        expect(
            target.get_by_role("button", name="Переключить передачу на этот сервер")
        ).to_have_count(0)
        screenshot(page, browser, size, "failure")
    finally:
        context.close()
        lab.close()


def test_select_ip_before_sending_preserves_obs_and_does_not_start(
    browser: Any, hud_server: HudServer, admin_password: str
) -> None:
    lab = MediaLab(hud_server)
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    try:
        page = context.new_page()
        login(page, hud_server, admin_password)
        prepare(page, lab.a)
        original = connection(page)
        target = page.locator(f'[data-node-id="{lab.b}"]')
        page.locator("#obs-connect").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Протокол подключения").select_option("rtmp")
        expect(dialog.get_by_label("Ключ трансляции OBS")).to_be_visible()
        assert (
            dialog.get_by_label("Сервер", exact=True)
            .input_value()
            .startswith("rtmp://8.8.8.8:19001/source/")
        )
        key = dialog.get_by_label("Ключ трансляции OBS").input_value()
        assert key.startswith("direct?user=phone&pass=")
        dialog.get_by_role("button", name="Скопировать ключ").click()
        dialog.get_by_label("Протокол подключения").select_option("srt")
        expect(dialog.get_by_label("Ключ трансляции OBS")).to_have_count(0)
        expect(dialog.get_by_label("Сервер", exact=True)).to_have_value(original)
        dialog.get_by_role("button", name="Закрыть", exact=True).click()
        assert key not in page.content()
        expect(target.locator("strong")).to_have_text("1.1.1.1")
        target.get_by_role("button", name="Добавить сервер к эфиру").click()
        target.get_by_role("button", name="Выбрать для отправки").click()
        expect(target).to_have_attribute("data-current", "true")
        expect(target).to_contain_text("Выбран")
        expect(page.locator("#tx-topology")).to_contain_text("приём: 8.8.8.8 → передача: 1.1.1.1")
        page.reload()
        expect(target).to_have_attribute("data-current", "true")
        assert connection(page) == original
        with lab.store.database.connect() as db:
            assert not db.execute(
                "SELECT 1 FROM broadcast_outputs WHERE desired_enabled=1"
            ).fetchone()
            assert not db.execute("SELECT 1 FROM broadcast_egress_leases").fetchone()
        screenshot(page, browser, "mobile", "selected-before-start")
        save_key(page)
        lab.video = lab.forward = True
        page.locator("#send-action").click()
        expect(page.locator("#output-status")).to_have_text("Отправка работает", timeout=15000)
        expect(target).to_have_attribute("data-current", "true")
    finally:
        context.close()
        lab.close()
