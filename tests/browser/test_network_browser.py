from __future__ import annotations

import time
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
