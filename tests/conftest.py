from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.security import generate_master_key, hash_password


@pytest.fixture(scope="session")
def admin_password() -> str:
    return "correct horse battery staple"


@pytest.fixture()
def settings(tmp_path: Path, admin_password: str) -> Settings:
    return Settings(
        environment="test",
        public_domain="testserver",
        public_rtmp_host="testserver",
        public_rtmp_port=1935,
        session_secret="test-session-secret-that-is-long-enough-for-hmac",
        master_encryption_key=generate_master_key(),
        admin_login="admin",
        admin_password_hash=hash_password(admin_password),
        database_path=tmp_path / "restream.db",
        mediamtx_api_url="http://mediamtx.test:9997",
        mediamtx_internal_rtmp_url="rtmp://mediamtx.test:1935",
        max_destinations=2,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.05,
        reconnect_stable_seconds=0.05,
        reconnect_max_fast_failures=3,
        log_level="WARNING",
        trusted_proxies=("127.0.0.1",),
        cookie_secure=False,
        session_ttl_seconds=3600,
        ffmpeg_binary="ffmpeg",
        ffprobe_binary="ffprobe",
        worker_auth_user="test-worker",
    )


@pytest.fixture(autouse=True)
def no_production_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    yield
