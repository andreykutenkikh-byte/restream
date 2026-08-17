"""Secret-safe outbound HTTP client for node protocol v1."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from time import monotonic
from typing import cast

import httpx

from node_agent.credentials import SensitiveToken
from node_agent.errors import (
    ControlAPIError,
    CredentialsRejected,
    EnrollmentRejected,
    ProtocolError,
    ProtocolRejected,
)
from node_agent.models import (
    MAX_CONTROL_LATENCY_MS,
    CommandCompletion,
    EnrollmentResponse,
    JsonObject,
    NodeCommand,
    NodeSnapshot,
)
from node_agent.settings import AgentSettings

_MAX_RESPONSE_BYTES = 64 * 1024
_AGENT_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._+-]{1,64}\Z")


class NodeAPIClient:
    """Calls only the fixed enrollment, heartbeat, and command endpoints."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        agent_version: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not _AGENT_VERSION_PATTERN.fullmatch(agent_version):
            raise ValueError("agent version is invalid")
        self._settings = settings
        self._agent_version = agent_version
        self._clock = clock
        self._latency_lock = threading.Lock()
        self._control_latency_ms: float | None = None
        self._client = httpx.Client(
            base_url=settings.control_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AdoJapan-Restream-Node/{agent_version}",
            },
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.request_timeout_seconds,
                write=settings.request_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def enroll(
        self, enrollment_token: SensitiveToken, snapshot: NodeSnapshot
    ) -> EnrollmentResponse:
        _, payload = self._request_json(
            "POST",
            "/node-api/v1/enroll",
            json_body=snapshot.enrollment_payload(
                enrollment_token=enrollment_token, agent_version=self._agent_version
            ),
            expected_statuses={200},
        )
        return EnrollmentResponse.parse(payload)

    def heartbeat(
        self,
        node_token: SensitiveToken,
        snapshot: NodeSnapshot,
        *,
        current_command_id: str | None,
    ) -> None:
        started = self._clock()
        _, payload = self._request_json(
            "POST",
            "/node-api/v1/heartbeat",
            token=node_token,
            json_body=snapshot.heartbeat_payload(
                agent_version=self._agent_version,
                current_command_id=current_command_id,
                control_latency_ms=self.control_latency_ms(),
            ),
            expected_statuses={200},
        )
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ProtocolError("invalid_heartbeat_response", "heartbeat response is invalid")
        node_status = payload.get("node_status")
        if node_status not in {"ready", "degraded"}:
            raise ProtocolError("invalid_heartbeat_response", "heartbeat node status is invalid")
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        with self._latency_lock:
            self._control_latency_ms = round(min(MAX_CONTROL_LATENCY_MS, elapsed_ms), 3)

    def control_latency_ms(self) -> float | None:
        with self._latency_lock:
            return self._control_latency_ms

    def next_command(self, node_token: SensitiveToken, *, wait_seconds: int) -> NodeCommand | None:
        if not 0 <= wait_seconds <= 20:
            raise ValueError("command long-poll wait must be between 0 and 20 seconds")
        status, payload = self._request_json(
            "GET",
            "/node-api/v1/commands/next",
            token=node_token,
            query={"wait": str(wait_seconds)},
            expected_statuses={200, 204},
            read_timeout_seconds=wait_seconds + self._settings.request_timeout_seconds,
        )
        if status == 204:
            return None
        return NodeCommand.parse(payload)

    def ack_command(self, node_token: SensitiveToken, command_id: str) -> None:
        _, payload = self._request_json(
            "POST",
            f"/node-api/v1/commands/{command_id}/ack",
            token=node_token,
            json_body={},
            expected_statuses={200},
        )
        if not isinstance(payload, dict) or payload.get("status") != "acknowledged":
            raise ProtocolError("invalid_command_response", "command ack response is invalid")

    def complete_command(
        self,
        node_token: SensitiveToken,
        command_id: str,
        completion: CommandCompletion,
    ) -> None:
        _, payload = self._request_json(
            "POST",
            f"/node-api/v1/commands/{command_id}/complete",
            token=node_token,
            json_body=completion.to_payload(),
            expected_statuses={200},
        )
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ProtocolError(
                "invalid_command_response", "command completion response is invalid"
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: SensitiveToken | None = None,
        json_body: JsonObject | None = None,
        query: Mapping[str, str] | None = None,
        expected_statuses: set[int],
        read_timeout_seconds: float | None = None,
    ) -> tuple[int, object | None]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token.reveal()}"
        timeout = httpx.Timeout(
            connect=self._settings.connect_timeout_seconds,
            read=(
                read_timeout_seconds
                if read_timeout_seconds is not None
                else self._settings.request_timeout_seconds
            ),
            write=self._settings.request_timeout_seconds,
            pool=self._settings.connect_timeout_seconds,
        )
        try:
            with self._client.stream(
                method,
                path,
                headers=headers,
                json=json_body,
                params=query,
                timeout=timeout,
            ) as response:
                status = response.status_code
                if status in {401, 403} and token is not None:
                    raise CredentialsRejected(
                        "node_credential_rejected", "node credential was rejected"
                    )
                if status == 401 and path == "/node-api/v1/enroll":
                    raise EnrollmentRejected(
                        "enrollment_rejected", "node enrollment credential was rejected"
                    )
                if status == 204 and status in expected_statuses:
                    return status, None
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunk) > _MAX_RESPONSE_BYTES - len(body):
                        if status not in expected_statuses:
                            raise ControlAPIError(
                                "control_request_rejected", "control request was rejected"
                            )
                        raise ProtocolError(
                            "control_response_too_large", "control response is too large"
                        )
                    body.extend(chunk)
                if status not in expected_statuses:
                    if content_type == "application/json":
                        try:
                            rejected = json.loads(body)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            rejected = None
                        if (
                            isinstance(rejected, dict)
                            and isinstance(rejected.get("error"), dict)
                            and rejected["error"].get("code") == "unsupported_protocol"
                        ):
                            raise ProtocolRejected(
                                "unsupported_protocol", "node protocol version was rejected"
                            )
                    raise ControlAPIError(
                        "control_request_rejected", "control request was rejected"
                    )
                if content_type != "application/json":
                    raise ProtocolError("invalid_control_response", "control response is not JSON")
        except (
            CredentialsRejected,
            EnrollmentRejected,
            ControlAPIError,
            ProtocolError,
            ProtocolRejected,
        ):
            raise
        except httpx.TimeoutException:
            raise ControlAPIError("control_timeout", "control request timed out") from None
        except httpx.RequestError:
            raise ControlAPIError("control_unavailable", "control service is unavailable") from None
        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProtocolError("invalid_control_response", "control response is invalid") from None
        return status, cast(object, decoded)

    def __enter__(self) -> NodeAPIClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
