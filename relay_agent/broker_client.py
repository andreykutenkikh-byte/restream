"""Unprivileged, bounded client for the local root broker."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from relay_agent.broker import (
    BROKER_CLIENT_TIMEOUT_SECONDS,
    MAX_BROKER_MESSAGE_BYTES,
    peer_credentials,
)
from relay_agent.errors import RelayAgentError
from relay_agent.models import JsonObject, RelaySnapshot

BROKER_SOCKET_PATH = Path("/run/adojapan-relay/broker.sock")
# relayctl start polls for up to 7.5 seconds, then the broker takes a fresh bounded health snapshot.
BROKER_CALL_TIMEOUT_SECONDS = BROKER_CLIENT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True, repr=False)
class BrokerResponse:
    status: Literal["ok", "failed", "conflict"]
    safe_result: RelaySnapshot
    secret_result: str | None

    def __repr__(self) -> str:
        return (
            f"BrokerResponse(status={self.status!r}, safe_result={self.safe_result!r}, "
            "secret_result=[REDACTED])"
        )

    @classmethod
    def parse(cls, value: object) -> BrokerResponse:
        if not isinstance(value, dict) or set(value) != {
            "status",
            "safe_result",
            "secret_result",
        }:
            raise RelayAgentError("invalid_broker_response")
        status = value["status"]
        secret_result = value["secret_result"]
        if status not in {"ok", "failed", "conflict"}:
            raise RelayAgentError("invalid_broker_response")
        if secret_result is not None and (
            not isinstance(secret_result, str) or not 1 <= len(secret_result) <= 4096
        ):
            raise RelayAgentError("invalid_broker_response")
        return cls(
            cast(Literal["ok", "failed", "conflict"], status),
            RelaySnapshot.parse(value["safe_result"]),
            secret_result,
        )


def validate_broker_socket(path: Path) -> None:
    try:
        parent = path.parent.lstat()
        metadata = path.lstat()
    except OSError as exc:
        raise RelayAgentError("broker_unavailable") from exc
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & 0o022
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o007
    ):
        raise RelayAgentError("unsafe_broker_socket")


class BrokerClient:
    def __init__(self, path: Path = BROKER_SOCKET_PATH) -> None:
        self._path = path
        self._call_lock = threading.Lock()

    def call(self, action: str, payload: JsonObject | None = None) -> BrokerResponse:
        # The agent has independent heartbeat and command threads.  Keep at most
        # one local request in flight so a status call cannot sit stale in the
        # socket backlog behind a mutating action.
        with self._call_lock:
            return self._call_locked(action, payload)

    def _call_locked(self, action: str, payload: JsonObject | None = None) -> BrokerResponse:
        if action not in {
            "status",
            "start",
            "stop",
            "configure_youtube",
            "configure_youtube_key",
            "clear_youtube",
            "reveal_moblin_url",
        }:
            raise RelayAgentError("unsupported_command")
        deadline_ns = time.monotonic_ns() + int(BROKER_CALL_TIMEOUT_SECONDS * 1_000_000_000)
        request = json.dumps(
            {
                "action": action,
                "payload": payload or {},
                "deadline_monotonic_ns": deadline_ns,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(request) > MAX_BROKER_MESSAGE_BYTES:
            raise RelayAgentError("broker_request_too_large")
        validate_broker_socket(self._path)
        body = bytearray()
        try:
            unix_family = socket.__dict__["AF_UNIX"]
            with socket.socket(unix_family, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._remaining_seconds(deadline_ns))
                connection.connect(os.fspath(self._path))
                if peer_credentials(connection)[1] != 0:
                    raise RelayAgentError("unsafe_broker_peer")
                connection.settimeout(self._remaining_seconds(deadline_ns))
                connection.sendall(request)
                connection.shutdown(socket.SHUT_WR)
                while True:
                    connection.settimeout(self._remaining_seconds(deadline_ns))
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    if len(chunk) > MAX_BROKER_MESSAGE_BYTES - len(body):
                        raise RelayAgentError("broker_response_too_large")
                    body.extend(chunk)
        except RelayAgentError:
            raise
        except (OSError, TimeoutError) as exc:
            raise RelayAgentError("broker_unavailable") from exc
        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RelayAgentError("invalid_broker_response") from exc
        return BrokerResponse.parse(decoded)

    @staticmethod
    def _remaining_seconds(deadline_ns: int) -> float:
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise RelayAgentError("broker_unavailable")
        return remaining
