import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_only_confirmed_live_state_is_presented_as_on_air() -> None:
    javascript = _read("app/static/relay-dashboard.js")
    dashboard = _read("app/templates/dashboard.html")

    assert 'relay.source === "LIVE"' in javascript
    assert "Видеопоток из Moblin поступает" in javascript
    assert "Moblin не подключён" in javascript
    assert "data-relay-signal-state" in dashboard


def test_ingest_preview_markup_is_safe_and_accessible() -> None:
    dashboard = _read("app/templates/dashboard.html")
    match = re.search(r"<video\b(?P<attributes>[^>]*)>", dashboard, flags=re.DOTALL)

    assert match is not None
    attributes = match.group("attributes")
    for boolean_attribute in ("autoplay", "muted", "playsinline", "controls"):
        assert re.search(rf"\b{boolean_attribute}\b", attributes)
    assert 'preload="metadata"' in attributes
    assert "data-relay-video" in attributes
    assert "src=" not in attributes
    assert 'data-preview-state="offline"' in dashboard
    assert "Видео пока не поступает" in dashboard
    assert "Подготавливаем защищённое превью…" in dashboard
    assert "Приём видеопотока может продолжаться." in dashboard
    assert "data-relay-preview-retry" in dashboard


def test_preview_uses_only_pinned_local_scripts() -> None:
    dashboard = _read("app/templates/dashboard.html")
    base = _read("app/templates/base.html")
    script_sources = re.findall(r'<script[^>]+src="([^"]+)"', dashboard + base)

    assert "/static/vendor/hls.min.js" in script_sources
    assert any(source.startswith("/static/preview-player.js?") for source in script_sources)
    assert all(not source.startswith(("http://", "https://", "//")) for source in script_sources)
    hls_bundle = ROOT / "app/static/vendor/hls.min.js"
    assert hls_bundle.is_file()
    assert hashlib.sha256(hls_bundle.read_bytes()).hexdigest() == (
        "442f599c34f103c3355b375a23bdff560592d7117d09a8c847242ea3de2d40e0"
    )
    license_text = _read("app/static/vendor/hls.LICENSE")
    assert "Apache License" in license_text


def test_preview_lifecycle_and_metadata_rendering_policy() -> None:
    javascript = _read("app/static/relay-dashboard.js")
    controller = _read("app/static/preview-player.js")

    assert "/relay/preview/index.m3u8" in javascript
    assert "sourceUrl:" in javascript
    assert "POLL_ACTIVE_MS = 3000" in javascript
    resume = javascript.index("previewController.resume()")
    set_live = javascript.index('previewController.setStreamState("live")')
    assert resume < set_live
    assert 'previewController?.suspend("offline")' in javascript
    assert "formatBitrate" in javascript
    assert "data-relay-bitrate" in javascript
    assert "enableWorker: false" in controller
    assert "blockedAfterError" in controller
    assert "this.hlsClass.isSupported()" in controller
    assert "application/vnd.apple.mpegurl" in controller
    assert "stream_key" not in controller
    assert "worker_auth" not in controller.lower()


def test_preview_layout_is_portrait_and_reflows_on_mobile() -> None:
    stylesheet = _read("app/static/styles.css")

    assert ".signal-preview--portrait" in stylesheet
    assert "aspect-ratio: 9 / 16" in stylesheet
    mobile_rules = stylesheet.split("@media (max-width: 680px)", maxsplit=1)[1].split(
        "@media (max-width: 420px)", maxsplit=1
    )[0]
    assert ".relay-monitor__layout" in mobile_rules
    assert "grid-template-columns: 1fr" in mobile_rules


def test_primary_dashboard_has_two_setup_actions_and_collapsed_advanced_controls() -> None:
    dashboard = _read("app/templates/dashboard.html")

    assert dashboard.count('data-setup-step="') == 2
    assert "Ключ потока YouTube" in dashboard
    assert "Ссылка для Moblin или OBS" in dashboard
    assert "Дополнительно: заменить RTMPS-адрес" in dashboard
    assert '"configure-youtube-key"' in _read("app/static/relay-dashboard.js")
    assert '<details class="panel relay-advanced"' in dashboard
    assert "data-relay-start" in dashboard
    assert "data-relay-stop" in dashboard
