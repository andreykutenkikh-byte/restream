"""Authenticated administrative and outbound-only Node Agent API."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import require_csrf, require_session
from app.schemas import (
    NodeCommandAckRequest,
    NodeCommandCompleteRequest,
    NodeEnrollmentRequest,
    NodeHeartbeatRequest,
    NodeRenameRequest,
)
from app.services.nodes import (
    CommandNotFoundError,
    CommandStateError,
    EnrollmentTokenError,
    HeartbeatRateLimitError,
    NodeAuthenticationError,
    NodeNotFoundError,
    NodeService,
    NodeUnavailableError,
    UnsupportedProtocolError,
)

MAX_NODE_BODY_BYTES = 16 * 1024
ENROLLMENT_RETRY_AFTER_SECONDS = 60
LOGGER = logging.getLogger(__name__)

router = APIRouter()


class NodeBodyLimitMiddleware:
    """Reject oversized Node API bodies while they are still being streamed."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_NODE_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not str(scope.get("path", "")).startswith(("/node-api/", "/relay-agent/"))
            or str(scope.get("method", "")).upper() not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return

        raw_headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        lengths = [value for name, value in raw_headers if name.lower() == b"content-length"]
        if lengths:
            try:
                declared_length = int(lengths[-1].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                response = _error_response(
                    status.HTTP_400_BAD_REQUEST, "invalid_content_length", "Invalid request"
                )
                await response(scope, receive, send)
                return
            if declared_length < 0:
                response = _error_response(
                    status.HTTP_400_BAD_REQUEST, "invalid_content_length", "Invalid request"
                )
                await response(scope, receive, send)
                return
            if declared_length > self.max_body_bytes:
                response = _error_response(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "payload_too_large",
                    "Payload is too large",
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail={"code": "payload_too_large", "message": "Payload is too large"},
                    )
            return message

        await self.app(scope, limited_receive, send)


class ConcurrentNodePollError(RuntimeError):
    """Raised when one node opens more than one command long poll."""


class NodePollRateLimitError(RuntimeError):
    """Raised when one node starts command polls too frequently."""


class NodeCommandPollGate:
    """Allows at most one outstanding command poll per authenticated node."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float = 1.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._lock = asyncio.Lock()
        self._active: set[str] = set()
        self._last_started: dict[str, float] = {}
        self._minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock

    @asynccontextmanager
    async def hold(self, node_id: str) -> AsyncIterator[None]:
        async with self._lock:
            if node_id in self._active:
                raise ConcurrentNodePollError
            now = self._clock()
            last_started = self._last_started.get(node_id)
            if last_started is not None and now - last_started < self._minimum_interval_seconds:
                raise NodePollRateLimitError
            self._active.add(node_id)
            self._last_started[node_id] = now
        try:
            yield
        finally:
            async with self._lock:
                self._active.discard(node_id)


class ConcurrentNodeEnrollmentError(RuntimeError):
    """Raised when one peer opens more than one enrollment request."""


class NodeEnrollmentRateLimitError(RuntimeError):
    """Raised when one peer exceeds the bounded enrollment attempt budget."""


class NodeEnrollmentCapacityError(RuntimeError):
    """Raised when the global enrollment concurrency budget is exhausted."""


class NodeEnrollmentGate:
    """Bound per-peer enrollment rate, concurrency, and in-memory identity state."""

    def __init__(
        self,
        *,
        attempts: int = 5,
        window_seconds: float = 60.0,
        max_identities: int = 4096,
        max_concurrent: int = 32,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if attempts <= 0 or window_seconds <= 0 or max_concurrent <= 0:
            raise ValueError("enrollment gate limits must be positive")
        if max_identities < max_concurrent:
            raise ValueError("enrollment identity capacity must cover concurrency")
        self._attempt_limit = attempts
        self._window_seconds = window_seconds
        self._max_identities = max_identities
        self._max_concurrent = max_concurrent
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active: set[str] = set()
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def _entries(self, identity: str, now: float) -> deque[float]:
        entries = self._attempts.get(identity)
        if entries is None:
            while len(self._attempts) >= self._max_identities:
                evictable = next(
                    (candidate for candidate in self._attempts if candidate not in self._active),
                    None,
                )
                if evictable is None:
                    raise NodeEnrollmentCapacityError
                self._attempts.pop(evictable)
            entries = deque()
            self._attempts[identity] = entries
        else:
            self._attempts.move_to_end(identity)
        cutoff = now - self._window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        return entries

    @asynccontextmanager
    async def hold(self, identity: str) -> AsyncIterator[None]:
        async with self._lock:
            if identity in self._active:
                raise ConcurrentNodeEnrollmentError
            if len(self._active) >= self._max_concurrent:
                raise NodeEnrollmentCapacityError
            now = self._clock()
            entries = self._entries(identity, now)
            if len(entries) >= self._attempt_limit:
                raise NodeEnrollmentRateLimitError
            entries.append(now)
            self._active.add(identity)
        try:
            yield
        finally:
            async with self._lock:
                self._active.discard(identity)


def _error_response(http_status: int, code: str, message: str) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=http_status,
    )


def _nodes(request: Request) -> NodeService:
    return cast(NodeService, request.app.state.nodes)


def _fail(http_status: int, code: str, message: str, **headers: str) -> NoReturn:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers=headers or None,
    )


def _bearer_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    if authorization is None:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not token or " " in token:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    return token


def _handle_node_authentication(
    service: NodeService,
    token: str,
    *,
    require_supported_protocol: bool = False,
) -> dict[str, Any]:
    try:
        return service.authenticate(
            token,
            require_supported_protocol=require_supported_protocol,
        )
    except NodeAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )


async def _notify_bootstrap_enrollment(request: Request, node_id: str) -> None:
    try:
        await request.app.state.bootstrap.notify_enrollment_completed(node_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.warning("Bootstrap enrollment notification failed")


@router.post(
    "/node-api/v1/enroll",
    include_in_schema=False,
)
async def enroll_node(payload: NodeEnrollmentRequest, request: Request) -> dict[str, Any]:
    public_ip = request.client.host if request.client is not None else "unknown"
    gate = cast(NodeEnrollmentGate, request.app.state.node_enrollments)
    try:
        async with gate.hold(public_ip):
            enrollment_token = payload.enrollment_token.get_secret_value()
            if not 32 <= len(enrollment_token) <= 512:
                raise EnrollmentTokenError("enrollment token is invalid or expired")
            values = payload.model_dump(mode="json", exclude={"enrollment_token"})
            grant = await asyncio.to_thread(
                _nodes(request).enroll,
                enrollment_token,
                public_ip=public_ip,
                profile=values,
            )
    except (
        ConcurrentNodeEnrollmentError,
        NodeEnrollmentCapacityError,
        NodeEnrollmentRateLimitError,
    ):
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "enrollment_rate_limited",
            "Enrollment requests are temporarily limited",
            **{"Retry-After": str(ENROLLMENT_RETRY_AFTER_SECONDS)},
        )
    except EnrollmentTokenError:
        _fail(
            status.HTTP_401_UNAUTHORIZED,
            "enrollment_failed",
            "Enrollment token is invalid or expired",
        )
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )
    return {
        "node_id": grant.node_id,
        "node_token": grant.node_token,
        "heartbeat_interval_seconds": grant.heartbeat_interval_seconds,
        "command_poll_interval_seconds": grant.command_poll_interval_seconds,
    }


@router.post(
    "/node-api/v1/heartbeat",
    include_in_schema=False,
)
async def heartbeat(
    payload: NodeHeartbeatRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, Any]:
    try:
        result = _nodes(request).record_heartbeat(token, payload.model_dump(mode="json"))
    except NodeAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )
    except HeartbeatRateLimitError:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "heartbeat_rate_limited",
            "Heartbeat sent too frequently",
            **{"Retry-After": "1"},
        )
    background_tasks = cast(set[asyncio.Task[None]], request.app.state.background_tasks)
    notification = asyncio.create_task(_notify_bootstrap_enrollment(request, result["node_id"]))
    background_tasks.add(notification)
    notification.add_done_callback(background_tasks.discard)
    return {"status": "ok", **result}


@router.get(
    "/node-api/v1/commands/next",
    response_model=None,
    include_in_schema=False,
)
async def next_command(
    request: Request,
    wait: Annotated[int, Query(ge=0, le=20)] = 20,
    token: str = Depends(_bearer_token),
) -> dict[str, Any] | Response:
    service = _nodes(request)
    authenticated = _handle_node_authentication(
        service,
        token,
        require_supported_protocol=True,
    )
    poll_gate = cast(NodeCommandPollGate, request.app.state.node_command_polls)
    try:
        async with poll_gate.hold(str(authenticated["node_id"])):
            deadline = monotonic() + wait
            while True:
                try:
                    command = service.lease_next_command(token)
                except NodeAuthenticationError:
                    _fail(
                        status.HTTP_401_UNAUTHORIZED,
                        "node_authentication_failed",
                        "Authentication failed",
                    )
                except UnsupportedProtocolError:
                    _fail(
                        status.HTTP_409_CONFLICT,
                        "unsupported_protocol",
                        "Node protocol version is not supported",
                    )
                if command is not None:
                    return command
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return Response(status_code=status.HTTP_204_NO_CONTENT)
                await asyncio.sleep(min(1.0, remaining))
    except ConcurrentNodePollError:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "command_poll_in_progress",
            "A command poll is already in progress",
            **{"Retry-After": "1"},
        )
    except NodePollRateLimitError:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "command_poll_rate_limited",
            "Command polls are sent too frequently",
            **{"Retry-After": "1"},
        )


@router.post(
    "/node-api/v1/commands/{command_id}/ack",
    include_in_schema=False,
)
async def acknowledge_command(
    command_id: str,
    _: NodeCommandAckRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, str]:
    try:
        result = _nodes(request).acknowledge_command(token, command_id)
    except NodeAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )
    except CommandNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    except CommandStateError:
        _fail(status.HTTP_409_CONFLICT, "invalid_command_state", "Command state changed")
    return {"status": result}


@router.post(
    "/node-api/v1/commands/{command_id}/complete",
    include_in_schema=False,
)
async def complete_command(
    command_id: str,
    payload: NodeCommandCompleteRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, str]:
    service = _nodes(request)
    authenticated = _handle_node_authentication(
        service,
        token,
        require_supported_protocol=True,
    )
    command = service.get_command(str(authenticated["node_id"]), command_id)
    if command is None:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    if command["command_type"] == "PING":
        if payload.received_at is None or payload.checks is not None:
            _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_result", "Invalid PING result")
        if payload.completed_at < payload.received_at:
            _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_result", "Invalid PING result")
    elif payload.checks is None or payload.received_at is not None:
        _fail(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_result",
            "Invalid SELF_TEST result",
        )
    try:
        result = service.complete_command(token, command_id, payload.model_dump(mode="json"))
    except NodeAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "node_authentication_failed", "Authentication failed")
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )
    except CommandNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    except CommandStateError:
        _fail(status.HTTP_409_CONFLICT, "invalid_command_state", "Command state changed")
    return {"status": result}


@router.get("/api/nodes", dependencies=[Depends(require_session)])
async def list_nodes(request: Request) -> dict[str, Any]:
    return {"items": _nodes(request).list_nodes()}


@router.get("/api/nodes/{node_id}", dependencies=[Depends(require_session)])
async def get_node(node_id: str, request: Request) -> dict[str, Any]:
    node = _nodes(request).get_node(node_id)
    if node is None:
        _fail(status.HTTP_404_NOT_FOUND, "node_not_found", "Node not found")
    return node


@router.patch("/api/nodes/{node_id}")
async def rename_node(
    node_id: str,
    payload: NodeRenameRequest,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return _nodes(request).rename_node(node_id, payload.display_name)
    except NodeNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "node_not_found", "Node not found")


def _queue_admin_command(request: Request, node_id: str, command_type: str) -> dict[str, Any]:
    try:
        if command_type == "PING":
            command = _nodes(request).create_command(node_id, "PING")
        else:
            command = _nodes(request).create_command(node_id, "SELF_TEST")
    except NodeNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "node_not_found", "Node not found")
    except NodeAuthenticationError:
        _fail(status.HTTP_409_CONFLICT, "node_revoked", "Node access is revoked")
    except NodeUnavailableError:
        _fail(status.HTTP_409_CONFLICT, "node_unavailable", "Node is not ready for commands")
    except UnsupportedProtocolError:
        _fail(
            status.HTTP_409_CONFLICT,
            "unsupported_protocol",
            "Node protocol version is not supported",
        )
    return command


@router.post("/api/nodes/{node_id}/ping", status_code=status.HTTP_202_ACCEPTED)
async def queue_ping(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    return _queue_admin_command(request, node_id, "PING")


@router.post("/api/nodes/{node_id}/self-test", status_code=status.HTTP_202_ACCEPTED)
async def queue_self_test(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    return _queue_admin_command(request, node_id, "SELF_TEST")


@router.get(
    "/api/nodes/{node_id}/commands/{command_id}",
    dependencies=[Depends(require_session)],
)
async def get_admin_command(node_id: str, command_id: str, request: Request) -> dict[str, Any]:
    command = _nodes(request).get_command(node_id, command_id)
    if command is None:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    return command


@router.post("/api/nodes/{node_id}/revoke")
async def revoke_node(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        revoked = _nodes(request).revoke_node(node_id)
    except NodeNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "node_not_found", "Node not found")
    except NodeUnavailableError:
        _fail(
            status.HTTP_409_CONFLICT,
            "node_bootstrap_active",
            "Cancel the active bootstrap job before revoking node access",
        )
    return revoked


__all__ = [
    "NodeBodyLimitMiddleware",
    "NodeCommandPollGate",
    "NodeEnrollmentGate",
    "router",
]
