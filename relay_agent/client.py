"""HTTPS-only relay-agent control-plane client with bounded JSON responses."""

from __future__ import annotations

import http.client
import json
import re
import ssl
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from relay_agent import AGENT_VERSION, PROTOCOL_VERSION
from relay_agent.errors import RelayAgentError
from relay_agent.models import (
    HostMetrics,
    JsonObject,
    RelayCommand,
    RelayCompletion,
    RelaySnapshot,
    is_uuid,
)
from relay_agent.security import SensitiveToken

CONTROL_ORIGIN = "https://restream.adojapan.ru"
MAX_CONTROL_RESPONSE_BYTES = 64 * 1024
MAX_PREVIEW_SEGMENT_BYTES = 3 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._+-]{1,64}\Z")


@dataclass(frozen=True, slots=True)
class HeartbeatIntervals:
    heartbeat_seconds: int
    poll_seconds: int
    preview_requested: bool = False


class ControlClient:
    """Calls only the five fixed v1 relay-agent routes."""

    def __init__(
        self,
        token: SensitiveToken,
        *,
        origin: str = CONTROL_ORIGIN,
        agent_version: str = AGENT_VERSION,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname != "restream.adojapan.ru"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RelayAgentError("invalid_control_origin")
        if not _VERSION_PATTERN.fullmatch(agent_version):
            raise RelayAgentError("invalid_agent_version")
        self._token = token
        self._host = parsed.hostname
        self._port = parsed.port or 443
        self._agent_version = agent_version
        self._ssl_context = ssl_context or ssl.create_default_context()

    def heartbeat(
        self,
        *,
        hostname: str,
        relay: RelaySnapshot,
        host: HostMetrics,
        current_command_id: str | None,
    ) -> HeartbeatIntervals:
        if (
            not isinstance(hostname, str)
            or not 1 <= len(hostname) <= 253
            or not all(character.isprintable() for character in hostname)
            or (current_command_id is not None and not is_uuid(current_command_id))
        ):
            raise RelayAgentError("invalid_heartbeat")
        _, response = self._request(
            "POST",
            "/relay-agent/v1/heartbeat",
            {
                "agent_version": self._agent_version,
                "protocol_version": PROTOCOL_VERSION,
                "hostname": hostname,
                "relay": relay.to_json(),
                "host": host.to_json(),
                "current_command_id": current_command_id,
            },
            expected_statuses={200},
            timeout_seconds=15.0,
        )
        required_keys = {
            "status",
            "node_id",
            "heartbeat_interval_seconds",
            "command_poll_interval_seconds",
        }
        if (
            not isinstance(response, dict)
            or not required_keys.issubset(response)
            or not set(response).issubset(required_keys | {"preview_requested"})
        ):
            raise RelayAgentError("invalid_control_response")
        if (
            response["status"] != "ok"
            or not is_uuid(response["node_id"])
            or response["heartbeat_interval_seconds"] != 5
            or response["command_poll_interval_seconds"] != 5
            or not isinstance(response.get("preview_requested", False), bool)
        ):
            raise RelayAgentError("invalid_control_response")
        return HeartbeatIntervals(5, 5, bool(response.get("preview_requested", False)))

    def upload_preview_segment(self, generation: str, sequence: int, payload: bytes) -> None:
        """Upload one bounded MPEG-TS segment to the separate media endpoint."""

        try:
            parsed_generation = UUID(generation)
        except (TypeError, ValueError):
            parsed_generation = None
        if (
            parsed_generation is None
            or parsed_generation.version != 4
            or str(parsed_generation) != generation
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence <= 2**63 - 1
            or not isinstance(payload, bytes)
            or not 0 < len(payload) <= MAX_PREVIEW_SEGMENT_BYTES
        ):
            raise RelayAgentError("invalid_preview_upload")
        path = f"/relay-media/v1/preview/segments/{generation}/{sequence}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token.reveal_for_authorization_header()}",
            "Content-Length": str(len(payload)),
            "Content-Type": "video/mp2t",
            "User-Agent": f"AdoJapan-HK-Relay-Agent/{self._agent_version}",
        }
        connection = http.client.HTTPSConnection(
            self._host,
            self._port,
            timeout=15.0,
            context=self._ssl_context,
        )
        try:
            connection.request("PUT", path, body=payload, headers=headers)
            response = connection.getresponse()
            status = response.status
            response_body = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise RelayAgentError("preview_upload_unavailable") from exc
        finally:
            connection.close()
        if len(response_body) > MAX_CONTROL_RESPONSE_BYTES:
            raise RelayAgentError("preview_upload_rejected")
        if status in {401, 403}:
            raise RelayAgentError("credential_rejected")
        if status != 204 or response_body:
            raise RelayAgentError("preview_upload_rejected")

    def next_command(self, *, wait_seconds: int = 20) -> RelayCommand | None:
        if (
            not isinstance(wait_seconds, int)
            or isinstance(wait_seconds, bool)
            or not 0 <= wait_seconds <= 20
        ):
            raise RelayAgentError("invalid_poll_wait")
        status, response = self._request(
            "GET",
            f"/relay-agent/v1/commands/next?wait={wait_seconds}",
            None,
            expected_statuses={200, 204},
            timeout_seconds=wait_seconds + 15.0,
        )
        if status == 204:
            return None
        return RelayCommand.parse(response)

    def acknowledge(self, command_id: str) -> None:
        if not is_uuid(command_id):
            raise RelayAgentError("invalid_protocol")
        _, response = self._request(
            "POST",
            f"/relay-agent/v1/commands/{command_id}/ack",
            {},
            expected_statuses={200},
            timeout_seconds=15.0,
        )
        if not isinstance(response, dict) or response != {"status": "acknowledged"}:
            raise RelayAgentError("invalid_control_response")

    def complete(self, command_id: str, completion: RelayCompletion) -> None:
        if not is_uuid(command_id):
            raise RelayAgentError("invalid_protocol")
        _, response = self._request(
            "POST",
            f"/relay-agent/v1/commands/{command_id}/complete",
            completion.to_json(),
            expected_statuses={200},
            timeout_seconds=15.0,
        )
        if not isinstance(response, dict) or response != {"status": "completed"}:
            raise RelayAgentError("invalid_control_response")

    def _request(
        self,
        method: str,
        path: str,
        body: JsonObject | None,
        *,
        expected_statuses: set[int],
        timeout_seconds: float,
    ) -> tuple[int, object | None]:
        if method not in {"GET", "POST"} or not path.startswith("/relay-agent/v1/"):
            raise RelayAgentError("invalid_control_request")
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(encoded) > MAX_CONTROL_RESPONSE_BYTES:
                raise RelayAgentError("control_request_too_large")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token.reveal_for_authorization_header()}",
            "Content-Type": "application/json",
            "User-Agent": f"AdoJapan-HK-Relay-Agent/{self._agent_version}",
            # The command poll uses this assertion instead of the potentially
            # stale version from the last heartbeat.  Older agents omit the
            # header and are intentionally limited to legacy command types.
            "X-Relay-Agent-Version": self._agent_version,
        }
        connection = http.client.HTTPSConnection(
            self._host,
            self._port,
            timeout=timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            response_body = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise RelayAgentError("control_unavailable") from exc
        finally:
            connection.close()
        if len(response_body) > MAX_CONTROL_RESPONSE_BYTES:
            raise RelayAgentError("control_response_too_large")
        if status in {401, 403}:
            raise RelayAgentError("credential_rejected")
        if status not in expected_statuses:
            raise RelayAgentError("control_request_rejected")
        if status == 204:
            if response_body:
                raise RelayAgentError("invalid_control_response")
            return status, None
        if content_type != "application/json":
            raise RelayAgentError("invalid_control_response")
        try:
            decoded = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RelayAgentError("invalid_control_response") from exc
        return status, cast(object, decoded)
