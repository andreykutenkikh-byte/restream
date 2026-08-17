"""Non-secret node agent settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from node_agent.errors import ConfigurationError

PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 5
MAX_COMMAND_WAIT_SECONDS = 20
_ARCHITECTURE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")


def _parse_positive_float(name: str, value: str, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError("invalid_configuration", f"{name} must be a number") from exc
    if not 0 < parsed <= maximum:
        raise ConfigurationError(
            "invalid_configuration", f"{name} must be greater than zero and at most {maximum:g}"
        )
    return parsed


def _validate_control_url(value: str, *, allow_insecure_http: bool) -> str:
    if len(value) > 2048:
        raise ConfigurationError("invalid_control_url", "NODE_CONTROL_URL is too long")
    parsed = urlsplit(value)
    allowed_schemes = {"https"}
    if allow_insecure_http:
        allowed_schemes.add("http")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(
            "invalid_control_url", "NODE_CONTROL_URL has an invalid host or port"
        ) from exc
    if (
        parsed.scheme not in allowed_schemes
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConfigurationError("invalid_control_url", "NODE_CONTROL_URL must be an HTTPS origin")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("invalid_control_url", "NODE_CONTROL_URL has an invalid port")
    return value.rstrip("/")


def _optional_identity(name: str, value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not all(character.isprintable() for character in normalized)
    ):
        raise ConfigurationError("invalid_configuration", f"{name} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Runtime settings. Credentials deliberately cannot be configured here."""

    control_url: str
    data_dir: Path
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 15.0
    command_wait_seconds: int = MAX_COMMAND_WAIT_SECONDS
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    allow_insecure_http: bool = False
    host_hostname: str | None = None
    host_os_name: str | None = None
    host_os_version: str | None = None
    host_architecture: str | None = None

    def __post_init__(self) -> None:
        normalized_url = _validate_control_url(
            self.control_url, allow_insecure_http=self.allow_insecure_http
        )
        object.__setattr__(self, "control_url", normalized_url)
        object.__setattr__(
            self,
            "host_hostname",
            _optional_identity("NODE_HOSTNAME", self.host_hostname, maximum=253),
        )
        object.__setattr__(
            self,
            "host_os_name",
            _optional_identity("NODE_OS_NAME", self.host_os_name, maximum=64),
        )
        object.__setattr__(
            self,
            "host_os_version",
            _optional_identity("NODE_OS_VERSION", self.host_os_version, maximum=128),
        )
        architecture = _optional_identity("NODE_ARCHITECTURE", self.host_architecture, maximum=32)
        if architecture is not None and not _ARCHITECTURE_PATTERN.fullmatch(architecture):
            raise ConfigurationError("invalid_configuration", "NODE_ARCHITECTURE is invalid")
        object.__setattr__(self, "host_architecture", architecture)
        if not self.data_dir.is_absolute():
            raise ConfigurationError("invalid_data_dir", "NODE_DATA_DIR must be absolute")
        if not 1 <= self.command_wait_seconds <= MAX_COMMAND_WAIT_SECONDS:
            raise ConfigurationError(
                "invalid_command_wait", "command wait must be between 1 and 20 seconds"
            )
        for name, value, maximum in (
            ("connect timeout", self.connect_timeout_seconds, 60.0),
            ("request timeout", self.request_timeout_seconds, 60.0),
            ("initial backoff", self.backoff_initial_seconds, 60.0),
            ("maximum backoff", self.backoff_max_seconds, 300.0),
        ):
            if not 0 < value <= maximum:
                raise ConfigurationError(
                    "invalid_configuration",
                    f"{name} must be greater than zero and at most {maximum:g}",
                )
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ConfigurationError(
                "invalid_configuration", "initial backoff cannot exceed maximum backoff"
            )

    @property
    def enrollment_token_path(self) -> Path:
        return self.data_dir / "enrollment.token"

    @property
    def node_token_path(self) -> Path:
        return self.data_dir / "node.token"

    @property
    def command_journal_path(self) -> Path:
        return self.data_dir / "commands.json"

    @classmethod
    def from_env(cls) -> AgentSettings:
        environment = os.getenv("NODE_AGENT_ENVIRONMENT", "production").strip().lower()
        if environment not in {"development", "production", "test"}:
            raise ConfigurationError(
                "invalid_configuration",
                "NODE_AGENT_ENVIRONMENT must be development, production, or test",
            )
        allow_insecure = environment in {"development", "test"}
        data_dir = Path(os.getenv("NODE_DATA_DIR", "/var/lib/adojapan-node"))
        command_wait_raw = os.getenv("NODE_COMMAND_WAIT_SECONDS", "20")
        try:
            command_wait = int(command_wait_raw)
        except ValueError as exc:
            raise ConfigurationError(
                "invalid_configuration", "NODE_COMMAND_WAIT_SECONDS must be an integer"
            ) from exc
        return cls(
            control_url=os.getenv("NODE_CONTROL_URL", "https://restream.adojapan.ru"),
            data_dir=data_dir,
            connect_timeout_seconds=_parse_positive_float(
                "NODE_CONNECT_TIMEOUT_SECONDS",
                os.getenv("NODE_CONNECT_TIMEOUT_SECONDS", "10"),
                maximum=60.0,
            ),
            request_timeout_seconds=_parse_positive_float(
                "NODE_REQUEST_TIMEOUT_SECONDS",
                os.getenv("NODE_REQUEST_TIMEOUT_SECONDS", "15"),
                maximum=60.0,
            ),
            command_wait_seconds=command_wait,
            backoff_initial_seconds=_parse_positive_float(
                "NODE_BACKOFF_INITIAL_SECONDS",
                os.getenv("NODE_BACKOFF_INITIAL_SECONDS", "1"),
                maximum=60.0,
            ),
            backoff_max_seconds=_parse_positive_float(
                "NODE_BACKOFF_MAX_SECONDS",
                os.getenv("NODE_BACKOFF_MAX_SECONDS", "30"),
                maximum=300.0,
            ),
            allow_insecure_http=allow_insecure,
            host_hostname=os.getenv("NODE_HOSTNAME"),
            host_os_name=os.getenv("NODE_OS_NAME"),
            host_os_version=os.getenv("NODE_OS_VERSION"),
            host_architecture=os.getenv("NODE_ARCHITECTURE"),
        )
