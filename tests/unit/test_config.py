from __future__ import annotations

import sys

import pytest

from app.cli import main as cli_main
from app.core.config import ConfigurationError, Settings
from app.core.security import generate_master_key, hash_password


def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("WORKER_AUTH_PASSWORD", "w" * 32)
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", generate_master_key())
    monkeypatch.setenv("ADMIN_LOGIN", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("a strong password"))


def test_required_secrets_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        Settings.from_env()


def test_worker_auth_password_is_required_and_has_minimum_entropy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_environment(monkeypatch)
    monkeypatch.delenv("WORKER_AUTH_PASSWORD")
    with pytest.raises(ConfigurationError, match="WORKER_AUTH_PASSWORD"):
        Settings.from_env()

    monkeypatch.setenv("WORKER_AUTH_PASSWORD", "too-short")
    with pytest.raises(ConfigurationError, match="at least 32"):
        Settings.from_env()


def test_production_worker_auth_password_must_be_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("WORKER_AUTH_PASSWORD", "x" * 32)
    with pytest.raises(ConfigurationError, match="independent"):
        Settings.from_env()


def test_cli_generates_strong_worker_auth_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["adojapan-restream", "generate-worker-auth-password"])

    cli_main()

    generated = capsys.readouterr().out.strip()
    assert len(generated) >= 32
    assert generated != "x" * 32


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


def test_production_rejects_insecure_cookie_override(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with pytest.raises(ConfigurationError, match="COOKIE_SECURE must be true"):
        Settings.from_env()


def test_destination_allowlist_can_only_be_configured_in_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_environment(monkeypatch)
    server_url = "rtmp://ci-rtmp-receiver:1935/ci-output"
    monkeypatch.setenv("TEST_DESTINATION_ALLOWLIST", server_url)
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert Settings.from_env().test_destination_allowlist == (server_url,)

    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ConfigurationError, match="only when ENVIRONMENT=test"):
        Settings.from_env()


def test_trusted_proxy_wildcard_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_environment(monkeypatch)
    monkeypatch.setenv("TRUSTED_PROXIES", "*")
    with pytest.raises(ConfigurationError, match="must not trust every address"):
        Settings.from_env()
