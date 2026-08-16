"""Environment-backed application settings with fail-closed secret validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path, PurePosixPath
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
    mediamtx_hls_url: str
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
    worker_auth_password: str
    test_destination_allowlist: tuple[str, ...] = ()
    bootstrap_socket_path: Path = Path("/run/adojapan-bootstrap/bootstrap.sock")
    bootstrap_worker_secret: str = ""
    node_agent_image: str = "adojapan-restream-node:development"
    node_protocol_version: int = 1
    public_control_url: str = "http://localhost:8000"
    test_ssh_target_allowlist: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ConfigurationError("ENVIRONMENT must be development, test, or production")

        session_secret = _required("SESSION_SECRET")
        if len(session_secret) < 32:
            raise ConfigurationError("SESSION_SECRET must contain at least 32 characters")

        worker_auth_password = _required("WORKER_AUTH_PASSWORD")
        if len(worker_auth_password) < 32:
            raise ConfigurationError("WORKER_AUTH_PASSWORD must contain at least 32 characters")
        if environment == "production" and worker_auth_password == session_secret:
            raise ConfigurationError(
                "WORKER_AUTH_PASSWORD must be independent from SESSION_SECRET in production"
            )

        bootstrap_worker_secret = _required("BOOTSTRAP_WORKER_SECRET")
        if len(bootstrap_worker_secret) < 32:
            raise ConfigurationError("BOOTSTRAP_WORKER_SECRET must contain at least 32 characters")
        if environment == "production" and bootstrap_worker_secret in {
            session_secret,
            worker_auth_password,
        }:
            raise ConfigurationError(
                "BOOTSTRAP_WORKER_SECRET must be independent from other service secrets"
            )

        test_destination_allowlist = tuple(
            item.strip()
            for item in os.getenv("TEST_DESTINATION_ALLOWLIST", "").split(",")
            if item.strip()
        )
        if test_destination_allowlist and environment != "test":
            raise ConfigurationError(
                "TEST_DESTINATION_ALLOWLIST is permitted only when ENVIRONMENT=test"
            )
        test_ssh_target_allowlist = tuple(
            item.strip()
            for item in os.getenv("TEST_SSH_TARGET_ALLOWLIST", "").split(",")
            if item.strip()
        )
        if test_ssh_target_allowlist and environment != "test":
            raise ConfigurationError(
                "TEST_SSH_TARGET_ALLOWLIST is permitted only when ENVIRONMENT=test"
            )

        master_encryption_key = _required("MASTER_ENCRYPTION_KEY")
        admin_login = _required("ADMIN_LOGIN")
        admin_password_hash = _required("ADMIN_PASSWORD_HASH")
        if not admin_password_hash.startswith("$argon2id$"):
            raise ConfigurationError("ADMIN_PASSWORD_HASH must be an Argon2id hash")

        mediamtx_api_url = os.getenv("MEDIAMTX_API_URL", "http://mediamtx:9997").rstrip("/")
        mediamtx_hls_url = os.getenv("MEDIAMTX_HLS_URL", "http://mediamtx:8888").rstrip("/")
        mediamtx_internal_rtmp_url = os.getenv(
            "MEDIAMTX_INTERNAL_RTMP_URL", "rtmp://mediamtx:1935"
        ).rstrip("/")
        if urlparse(mediamtx_api_url).scheme not in {"http", "https"}:
            raise ConfigurationError("MEDIAMTX_API_URL must be an HTTP(S) URL")
        parsed_hls_url = urlparse(mediamtx_hls_url)
        if (
            parsed_hls_url.scheme not in {"http", "https"}
            or not parsed_hls_url.hostname
            or parsed_hls_url.username is not None
            or parsed_hls_url.password is not None
            or parsed_hls_url.params
            or parsed_hls_url.query
            or parsed_hls_url.fragment
            or parsed_hls_url.path not in {"", "/"}
        ):
            raise ConfigurationError("MEDIAMTX_HLS_URL must be an HTTP(S) origin")
        if urlparse(mediamtx_internal_rtmp_url).scheme != "rtmp":
            raise ConfigurationError("MEDIAMTX_INTERNAL_RTMP_URL must be an RTMP URL")

        bootstrap_socket_raw = os.getenv(
            "BOOTSTRAP_SOCKET_PATH", "/run/adojapan-bootstrap/bootstrap.sock"
        )
        bootstrap_socket_posix = PurePosixPath(bootstrap_socket_raw)
        if not bootstrap_socket_posix.is_absolute() or ".." in bootstrap_socket_posix.parts:
            raise ConfigurationError("BOOTSTRAP_SOCKET_PATH must be absolute")
        bootstrap_socket_path = Path(bootstrap_socket_raw)

        node_protocol_version = _integer("NODE_PROTOCOL_VERSION", 1, minimum=1, maximum=1)
        node_agent_image = os.getenv(
            "NODE_AGENT_IMAGE", "adojapan-restream-node:development"
        ).strip()
        if environment == "production":
            node_agent_image = _required("NODE_AGENT_IMAGE")
            if not re.fullmatch(
                r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
                r"@sha256:[0-9a-f]{64}",
                node_agent_image,
            ):
                raise ConfigurationError(
                    "NODE_AGENT_IMAGE must use an immutable sha256 digest in production"
                )
        elif not node_agent_image:
            raise ConfigurationError("NODE_AGENT_IMAGE must not be empty")

        default_control_url = (
            f"https://{os.getenv('PUBLIC_DOMAIN', 'localhost').strip()}"
            if environment == "production"
            else "http://localhost:8000"
        )
        public_control_url = os.getenv("PUBLIC_CONTROL_URL", default_control_url).rstrip("/")
        parsed_control_url = urlparse(public_control_url)
        if (
            parsed_control_url.scheme not in {"http", "https"}
            or not parsed_control_url.hostname
            or parsed_control_url.username is not None
            or parsed_control_url.password is not None
            or parsed_control_url.path not in {"", "/"}
            or parsed_control_url.params
            or parsed_control_url.query
            or parsed_control_url.fragment
        ):
            raise ConfigurationError("PUBLIC_CONTROL_URL must be an HTTP(S) origin")
        if environment == "production" and (
            parsed_control_url.scheme != "https"
            or parsed_control_url.hostname != os.getenv("PUBLIC_DOMAIN", "localhost").strip()
        ):
            raise ConfigurationError(
                "PUBLIC_CONTROL_URL must use the configured production HTTPS domain"
            )

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
        cookie_secure = _boolean("COOKIE_SECURE", environment == "production")
        if environment == "production" and not cookie_secure:
            raise ConfigurationError("COOKIE_SECURE must be true in production")

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
            mediamtx_hls_url=mediamtx_hls_url,
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
            cookie_secure=cookie_secure,
            session_ttl_seconds=_integer("SESSION_TTL_SECONDS", 43200, minimum=300, maximum=604800),
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg").strip(),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe").strip(),
            worker_auth_user=os.getenv("WORKER_AUTH_USER", "adojapan-worker").strip(),
            worker_auth_password=worker_auth_password,
            test_destination_allowlist=test_destination_allowlist,
            bootstrap_socket_path=bootstrap_socket_path,
            bootstrap_worker_secret=bootstrap_worker_secret,
            node_agent_image=node_agent_image,
            node_protocol_version=node_protocol_version,
            public_control_url=public_control_url,
            test_ssh_target_allowlist=test_ssh_target_allowlist,
        )

    @property
    def public_rtmp_url(self) -> str:
        port = "" if self.public_rtmp_port == 1935 else f":{self.public_rtmp_port}"
        return f"rtmp://{self.public_rtmp_host}{port}/live"
