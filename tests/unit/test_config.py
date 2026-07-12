from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings
from app.core.security import generate_master_key, hash_password


def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", generate_master_key())
    monkeypatch.setenv("ADMIN_LOGIN", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("a strong password"))


def test_required_secrets_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        Settings.from_env()


def test_public_rtmp_url_omits_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("PUBLIC_RTMP_HOST", "restream.example.test")
    settings = Settings.from_env()
    assert settings.public_rtmp_url == "rtmp://restream.example.test/live"


def test_public_rtmp_url_includes_alternative_port(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("PUBLIC_RTMP_HOST", "restream.example.test")
    monkeypatch.setenv("PUBLIC_RTMP_PORT", "1936")
    settings = Settings.from_env()
    assert settings.public_rtmp_url == "rtmp://restream.example.test:1936/live"


def test_production_secure_cookie_default(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = Settings.from_env()
    assert settings.cookie_secure is True


def test_trusted_proxy_wildcard_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("TRUSTED_PROXIES", "*")
    with pytest.raises(ConfigurationError, match="must not trust every address"):
        Settings.from_env()
