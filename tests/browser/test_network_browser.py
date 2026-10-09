from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from test_transmission_browser import (
    MediaLab,
    browser,
    connection,
    expect,
    hud_server,
    login,
    prepare,
    pytestmark,
    save_key,
    screenshot,
)

from app.broadcast.network_models import NETWORK_CAPABILITY, IngressMeasurement, LinkMeasurement
from app.broadcast.server_quality import QualityReader
from app.broadcast.server_quality import context as quality_context

__all__ = ["browser", "hud_server", "pytestmark"]


def test_monitoring_and_armed_output_selection(
    browser: Any, hud_server: Any, admin_password: str
) -> None:
    lab = MediaLab(hud_server)
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    try:
        page = context.new_page()
        login(page, hud_server, admin_password)
        prepare(page, lab.a)
        original = connection(page)
        target = page.locator(f'[data-node-id="{lab.b}"]')
        target.get_by_role("button", name="Добавить сервер к эфиру").click()
        expect(target.get_by_role("button", name="Выбрать для отправки")).to_be_visible()
        expect(target).to_contain_text("Нужен тест видеопотока")
        with lab.store.transaction() as db:
            source = db.execute("SELECT id FROM broadcast_sources").fetchone()[0]
            route = db.execute(
                "SELECT id FROM broadcast_routes WHERE node_id=?", (lab.b,)
            ).fetchone()[0]
            from app.broadcast.media_control import MediaHeartbeat

            lab.media.network.record(
                db,
                lab.a,
                MediaHeartbeat(
                    boot_id="synthetic-network-boot",
                    public_key="x" * 44,
                    sequence=1,
                    plan_generation=0,
                    capabilities=[NETWORK_CAPABILITY],
                    network_version=1,
                    link_measurements=[
                        LinkMeasurement(
                            route_id=route,
                            sampled_at=time.time(),
                            kind="tcp",
                            reachable=True,
                            rtt_ms=22.5,
                        )
                    ],
                    ingress_measurements=[
                        IngressMeasurement(
                            source_id=source,
                            sampled_at=time.time(),
                            protocol="rtmp",
                            connection_epoch="network-input",
                            bitrate_bps=10_000_000,
                        )
                    ],
                ),
            )
        page.locator("#tx-refresh").click()
        expect(target).to_contain_text("22,5 мс")
        expect(page.locator("#tx-input-network")).to_contain_text("10 Мбит/с")
        expect(page.locator("#tx-input-network")).to_contain_text("Точные потери пакетов")
        assert "0 %" not in page.locator("#tx-input-network").inner_text()
        assert target.get_by_role("button", name="Проверить скорость до эфира").is_disabled()
        with lab.store.transaction() as db:
            db.execute(
                "UPDATE broadcast_ingress_metrics SET observed_at='2000-01-01T00:00:00+00:00'"
            )
        save_key(page)
        page.locator("#send-action").click()  # armed sending; OBS remains stopped
        target.get_by_role("button", name="Выбрать до следующего эфира").click()
        page.get_by_role("dialog").get_by_role(
            "button", name="Остановить отправку и выбрать"
        ).click()
        expect(page.locator("dialog")).to_have_count(0, timeout=35000)
        expect(target).to_have_attribute("data-current", "true")
        expect(page.locator("#send-action")).to_have_text("Начать отправку")
        assert connection(page) == original
        screenshot(page, browser, "mobile", "network-monitoring")
    finally:
        context.close()
        lab.close()


def test_quality_explanation_and_candidates_preserve_manual_selection(
    browser: Any, hud_server: Any, admin_password: str
) -> None:
    lab = MediaLab(hud_server)
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    try:
        page = ctx.new_page()
        login(page, hud_server, admin_password)
        prepare(page, lab.a)
        original = connection(page)
        current = page.locator(f'[data-node-id="{lab.a}"]')
        with lab.store.transaction() as db:
            route = db.execute(
                "SELECT id,output_id FROM broadcast_routes WHERE node_id=?", (lab.a,)
            ).fetchone()
            boot_id = db.execute(
                "SELECT boot_id FROM broadcast_media_nodes WHERE node_id=?", (lab.a,)
            ).fetchone()[0]
            scope = quality_context(db, route["id"])
            now = datetime.now(UTC) - timedelta(seconds=5)
            for i in range(0, 1861, 5):
                stamp = (now - timedelta(seconds=1860 - i)).isoformat()
                data = dict(
                    assessment_context=scope,
                    boot=hashlib.sha256(boot_id.encode()).hexdigest()[:24],
                    source_epoch="synthetic-source",
                    role="current",
                    desired_enabled=True,
                    source_kind="direct",
                    input_epoch=1,
                    egress_generation=1,
                    publisher_epoch=1,
                    publisher_frames=i * 30,
                    publisher_running=True,
                    publisher_connected=True,
                    publisher_progress_age_ms=100,
                    publisher_retries=0,
                    selector_queue_packets=0,
                    ingress_network={"state": "FRESH", "bitrate_bps": 10_000_000},
                )
                db.execute(
                    "INSERT INTO broadcast_quality_history(output_id,route_id,node_id,"
                    "observed_at,signature,payload_json) VALUES(?,?,?,?,'synthetic',?)",
                    (route["output_id"], route["id"], lab.a, stamp, json.dumps(data)),
                )
        lab.store.quality = QualityReader()
        page.locator("#tx-refresh").click()
        expect(current).to_contain_text("Стабилен по измерениям")
        expect(page.locator("#tx-quality-summary")).to_contain_text("По измерениям подходит")
        current.get_by_text("Почему такая оценка", exact=True).click()
        expect(current).to_contain_text("не менее 30", ignore_case=True)
        expect(current).to_contain_text("воспроизведение у зрителя")
        page.locator("#tx-refresh").click()
        expect(current.locator(".tx-quality details")).to_have_attribute("open", "")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert connection(page) == original
        with lab.store.database.connect() as db:
            assert db.execute("SELECT desired_enabled FROM broadcast_outputs").fetchone()[0] == 0
        screenshot(page, browser, "mobile", "server-quality")
    finally:
        ctx.close()
        lab.close()
