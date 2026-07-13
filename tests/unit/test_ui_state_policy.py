from pathlib import Path


def test_only_confirmed_live_state_is_presented_as_on_air() -> None:
    root = Path(__file__).resolve().parents[2]
    javascript = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    dashboard = (root / "app" / "templates" / "dashboard.html").read_text(encoding="utf-8")

    assert "running: {" not in javascript
    assert "['running', 'live']" not in dashboard
    assert "destination_state == 'live'" in dashboard
