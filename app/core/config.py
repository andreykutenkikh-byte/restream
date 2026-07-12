"""Environment-backed application settings with fail-closed secret validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    """Complete runtime settings.

    Secret fields deliberately have no fallback values. This makes both local and
    production startup explicit and prevents an accidentally weak deployment.
    """

    environment: str
    public_domain: str
    public_rtmp_host: str
    public_rtmp_port: int
    session_secret: str
    master_encryption_key: str
    admin_login: str
    admin_password_hash: str
    database_path: Path
    mediamtx_api_url: str
    mediamtx_internal_rtmp_url: str
    max_destinations: int
    reconnect_initial_seconds: float
    reconnect_max_seconds: float
    reconnect_stable_seconds: float
    reconnect_max_fast_failures: int
    log_level: str
    trusted_proxies: tuple[str, ...]
    cookie_secure: bool
    session_ttl_seconds: int
    ffmpeg_binary: str
    ffprobe_binary: str
    worker_auth_user: str

    @classmethod
    def from_env(cls) -> Settings:
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ConfigurationError("ENVIRONMENT must be development, test, or production")

        session_secret = _required("SESSION_SECRET")
        if len(session_secret) < 32:
            raise ConfigurationError("SESSION_SECRET must contain at least 32 characters")

        master_encryption_key = _required("MASTER_ENCRYPTION_KEY")
        admin_login = _required("ADMIN_LOGIN")
        admin_password_hash = _required("ADMIN_PASSWORD_HASH")
        if not admin_password_hash.startswith("$argon2id$"):
            raise ConfigurationError("ADMIN_PASSWORD_HASH must be an Argon2id hash")

        mediamtx_api_url = os.getenv("MEDIAMTX_API_URL", "http://mediamtx:9997").rstrip("/")
        mediamtx_internal_rtmp_url = os.getenv(
            "MEDIAMTX_INTERNAL_RTMP_URL", "rtmp://mediamtx:1935"
        ).rstrip("/")
        if urlparse(mediamtx_api_url).scheme not in {"http", "https"}:
            raise ConfigurationError("MEDIAMTX_API_URL must be an HTTP(S) URL")
        if urlparse(mediamtx_internal_rtmp_url).scheme != "rtmp":
            raise ConfigurationError("MEDIAMTX_INTERNAL_RTMP_URL must be an RTMP URL")

        trusted_proxies = tuple(
            part.strip()
            for part in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
            if part.strip()
        )
        for proxy in trusted_proxies:
            if proxy == "*":
                raise ConfigurationError("TRUSTED_PROXIES must not trust every address")
            try:
                ip_network(proxy, strict=False)
            except ValueError as exc:
                raise ConfigurationError(
                    "TRUSTED_PROXIES must contain only IP addresses or CIDR networks"
                ) from exc
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is invalid")

        return cls(
            environment=environment,
            public_domain=os.getenv("PUBLIC_DOMAIN", "localhost").strip(),
            public_rtmp_host=os.getenv("PUBLIC_RTMP_HOST", "localhost").strip(),
            public_rtmp_port=_integer("PUBLIC_RTMP_PORT", 1935, minimum=1, maximum=65535),
            session_secret=session_secret,
            master_encryption_key=master_encryption_key,
            admin_login=admin_login,
            admin_password_hash=admin_password_hash,
            database_path=Path(os.getenv("SQLITE_PATH", "./data/restream.db")),
            mediamtx_api_url=mediamtx_api_url,
            mediamtx_internal_rtmp_url=mediamtx_internal_rtmp_url,
            max_destinations=_integer("MAX_DESTINATIONS", 2, minimum=1, maximum=10),
            reconnect_initial_seconds=_float(
                "RECONNECT_INITIAL_SECONDS", 2.0, minimum=0.25, maximum=60.0
            ),
            reconnect_max_seconds=_float("RECONNECT_MAX_SECONDS", 30.0, minimum=1.0, maximum=300.0),
            reconnect_stable_seconds=_float(
                "RECONNECT_STABLE_SECONDS", 30.0, minimum=1.0, maximum=600.0
            ),
            reconnect_max_fast_failures=_integer(
                "RECONNECT_MAX_FAST_FAILURES", 6, minimum=1, maximum=100
            ),
            log_level=log_level,
            trusted_proxies=trusted_proxies,
            cookie_secure=_boolean("COOKIE_SECURE", environment == "production"),
            session_ttl_seconds=_integer("SESSION_TTL_SECONDS", 43200, minimum=300, maximum=604800),
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg").strip(),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe").strip(),
            worker_auth_user=os.getenv("WORKER_AUTH_USER", "adojapan-worker").strip(),
        )

    @property
    def public_rtmp_url(self) -> str:
        port = "" if self.public_rtmp_port == 1935 else f":{self.public_rtmp_port}"
        return f"rtmp://{self.public_rtmp_host}{port}/live"

    @property
    def worker_auth_password(self) -> str:
        """Use the session secret as an internal-only MediaMTX reader credential."""

        return self.session_secret
