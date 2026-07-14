import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_only_confirmed_live_state_is_presented_as_on_air() -> None:
    javascript = _read("app/static/app.js")
    dashboard = _read("app/templates/dashboard.html")

    assert "running: {" not in javascript
    assert "['running', 'live']" not in dashboard
    assert "destination_state == 'live'" in dashboard


def test_ingest_preview_markup_is_safe_and_accessible() -> None:
    dashboard = _read("app/templates/dashboard.html")
    match = re.search(r"<video\b(?P<attributes>[^>]*)>", dashboard, flags=re.DOTALL)

    assert match is not None
    attributes = match.group("attributes")
    for boolean_attribute in ("autoplay", "muted", "playsinline", "controls"):
        assert re.search(rf"\b{boolean_attribute}\b", attributes)
    assert 'preload="metadata"' in attributes
    assert "data-ingest-video" in attributes
    assert "src=" not in attributes
    assert 'data-preview-state="' in dashboard
    assert "Предпросмотр появится после запуска трансляции в OBS" in dashboard
    assert "Подготавливаем предпросмотр…" in dashboard
    assert "Входящий поток продолжает приниматься." in dashboard
    assert "data-preview-retry" in dashboard


def test_preview_uses_only_pinned_local_scripts() -> None:
    dashboard = _read("app/templates/dashboard.html")
    base = _read("app/templates/base.html")
    script_sources = re.findall(r'<script[^>]+src="([^"]+)"', dashboard + base)

    assert "/static/vendor/hls.min.js" in script_sources
    assert "/static/preview-player.js" in script_sources
    assert all(not source.startswith(("http://", "https://", "//")) for source in script_sources)
    hls_bundle = ROOT / "app/static/vendor/hls.min.js"
    assert hls_bundle.is_file()
    assert hashlib.sha256(hls_bundle.read_bytes()).hexdigest() == (
        "442f599c34f103c3355b375a23bdff560592d7117d09a8c847242ea3de2d40e0"
    )
    license_text = _read("app/static/vendor/hls.LICENSE")
    assert "Apache License" in license_text


def test_preview_lifecycle_and_metadata_rendering_policy() -> None:
    javascript = _read("app/static/app.js")
    controller = _read("app/static/preview-player.js")

    assert 'const PREVIEW_URL = "/api/ingest/preview/index.m3u8"' in javascript
    assert "sourceUrl: PREVIEW_URL" in javascript
    assert "INGEST_POLL_LIVE_MS = 2000" in javascript
    assert "previewController?.setStreamState(state)" in javascript
    assert 'previewController?.suspend("offline")' in javascript
    assert 'output.textContent = present ? value : "—"' in javascript
    assert "data-metadata" in javascript
    assert "enableWorker: false" in controller
    assert "blockedAfterError" in controller
    assert "this.hlsClass.isSupported()" in controller
    assert "application/vnd.apple.mpegurl" in controller
    assert "stream_key" not in controller
    assert "worker_auth" not in controller.lower()


def test_preview_layout_keeps_two_columns_until_very_narrow_screens() -> None:
    stylesheet = _read("app/static/styles.css")

    assert "aspect-ratio: 16 / 9" in stylesheet
    assert ".signal-preview video" in stylesheet
    narrow_rules = stylesheet.split("@media (max-width: 420px)", maxsplit=1)[1]
    assert ".signal-metadata" in narrow_rules
    assert "grid-template-columns: 1fr" in narrow_rules
    mobile_rules = stylesheet.split("@media (max-width: 680px)", maxsplit=1)[1].split(
        "@media (max-width: 420px)", maxsplit=1
    )[0]
    assert ".signal-metadata" not in mobile_rules
