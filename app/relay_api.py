"""Outbound-only native relay protocol and protected administrator API."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from ipaddress import ip_address
from time import monotonic
from typing import Annotated, Any, NoReturn, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api import SESSION_COOKIE, require_csrf, require_session
from app.core.security import digest_opaque_token, verify_password
from app.node_api import (
    ConcurrentNodePollError,
    NodeCommandPollGate,
    NodePollRateLimitError,
)
from app.schemas import (
    RelayCommandAckRequest,
    RelayCommandCompleteRequest,
    RelayConfigureYouTubeRequest,
    RelayHeartbeatRequest,
    RelayStepUpRequest,
)
from app.services.relays import (
    RelayActiveError,
    RelayAuthenticationError,
    RelayCommandNotFoundError,
    RelayCommandPendingError,
    RelayCommandStateError,
    RelayHeartbeatRateLimitError,
    RelayIdempotencyConflictError,
    RelayNotConfiguredError,
    RelayNotFoundError,
    RelaySecretUnavailableError,
    RelayService,
    RelayUnavailableError,
    RelayUnsupportedProtocolError,
)
from app.step_up_limiter import StepUpRateLimiter

router = APIRouter()
_SRT_TOKEN = re.compile(r"srt://[^\s\"'<>]+", re.IGNORECASE)


def _relays(request: Request) -> RelayService:
    return cast(RelayService, request.app.state.relays)


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
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not token or " " in token:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    return token


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    settings = request.app.state.settings
    expected = {f"https://{settings.public_domain}"}
    if settings.environment != "production":
        expected.add(f"http://{settings.public_domain}")
        expected.add(str(request.base_url).rstrip("/"))
    if origin not in expected:
        _fail(status.HTTP_403_FORBIDDEN, "origin_failed", "Request origin is not allowed")


def _step_up(request: Request, password: str, node_id: str) -> None:
    settings = request.app.state.settings
    session_token = request.cookies.get(SESSION_COOKIE)
    session_digest = digest_opaque_token(session_token) if session_token else "missing"
    client_identity = request.client.host if request.client else "unknown"
    identity = f"{session_digest}:{client_identity}"
    limiter = cast(StepUpRateLimiter, request.app.state.relay_step_up_limiter)
    retry_after = limiter.retry_after(identity)
    if retry_after is not None:
        request.app.state.database.add_audit_event(
            "relay.step_up_rate_limited", f"node_id={node_id}"
        )
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "step_up_rate_limited",
            "Too many administrator password attempts",
            **{"Retry-After": str(retry_after)},
        )
    if not verify_password(password, settings.admin_password_hash):
        limiter.fail(identity)
        request.app.state.database.add_audit_event("relay.step_up_failed", f"node_id={node_id}")
        _fail(
            status.HTTP_401_UNAUTHORIZED,
            "step_up_failed",
            "Administrator password is invalid",
        )
    limiter.success(identity)


def _command_error(exc: Exception) -> NoReturn:
    if isinstance(exc, RelayNotFoundError):
        _fail(status.HTTP_404_NOT_FOUND, "relay_not_found", "Relay node not found")
    if isinstance(exc, RelayAuthenticationError):
        _fail(status.HTTP_409_CONFLICT, "relay_revoked", "Relay access is revoked")
    if isinstance(exc, RelayUnavailableError):
        _fail(status.HTTP_409_CONFLICT, "relay_unavailable", "Relay node is offline")
    if isinstance(exc, RelayUnsupportedProtocolError):
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    if isinstance(exc, RelayActiveError):
        _fail(
            status.HTTP_409_CONFLICT,
            "relay_active",
            "Stop the relay before changing YouTube configuration",
        )
    if isinstance(exc, RelayNotConfiguredError):
        _fail(
            status.HTTP_409_CONFLICT,
            "youtube_not_configured",
            "YouTube is not configured",
        )
    if isinstance(exc, RelayCommandPendingError):
        _fail(status.HTTP_409_CONFLICT, "relay_command_pending", "A relay command is pending")
    if isinstance(exc, RelayIdempotencyConflictError):
        _fail(
            status.HTTP_409_CONFLICT,
            "idempotency_key_conflict",
            "Idempotency-Key belongs to a different relay request",
        )
    raise exc


def _idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value) is None:
        _fail(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_idempotency_key",
            "Idempotency-Key is invalid",
        )
    return value


def _queue(
    request: Request,
    node_id: str,
    action: str,
    *,
    payload: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    try:
        command = _relays(request).create_command(
            node_id,
            cast(Any, action),
            payload=payload,
            idempotency_key=_idempotency_key(idempotency_key),
        )
    except (
        RelayNotFoundError,
        RelayAuthenticationError,
        RelayUnavailableError,
        RelayUnsupportedProtocolError,
        RelayActiveError,
        RelayNotConfiguredError,
        RelayCommandPendingError,
        RelayIdempotencyConflictError,
    ) as exc:
        _command_error(exc)
    return {"command_id": command["id"], "state": command["state"]}


@router.post("/relay-agent/v1/heartbeat", include_in_schema=False)
async def relay_heartbeat(
    payload: RelayHeartbeatRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, Any]:
    try:
        result = _relays(request).record_heartbeat(token, payload.model_dump(mode="json"))
    except RelayAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    except RelayUnsupportedProtocolError:
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    except RelayHeartbeatRateLimitError:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "heartbeat_rate_limited",
            "Heartbeat sent too frequently",
            **{"Retry-After": "1"},
        )
    return {"status": "ok", **result}


@router.get("/relay-agent/v1/commands/next", response_model=None, include_in_schema=False)
async def relay_next_command(
    request: Request,
    wait: Annotated[int, Query(ge=0, le=20)] = 20,
    token: str = Depends(_bearer_token),
) -> dict[str, Any] | Response:
    service = _relays(request)
    try:
        authenticated = service.authenticate(token, require_supported_protocol=True)
    except RelayAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    except RelayUnsupportedProtocolError:
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    gate = cast(NodeCommandPollGate, request.app.state.relay_command_polls)
    try:
        async with gate.hold(str(authenticated["node_id"])):
            deadline = monotonic() + wait
            while True:
                try:
                    command = service.lease_next_command(token)
                except RelayAuthenticationError:
                    _fail(
                        status.HTTP_401_UNAUTHORIZED,
                        "relay_authentication_failed",
                        "Authentication failed",
                    )
                except RelayUnsupportedProtocolError:
                    _fail(
                        status.HTTP_409_CONFLICT,
                        "unsupported_protocol",
                        "Relay protocol is unsupported",
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


@router.post("/relay-agent/v1/commands/{command_id}/ack", include_in_schema=False)
async def relay_acknowledge_command(
    command_id: str,
    _: RelayCommandAckRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, str]:
    try:
        result = _relays(request).acknowledge_command(token, command_id)
    except RelayAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    except RelayUnsupportedProtocolError:
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    except RelayCommandNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    except RelayCommandStateError:
        _fail(status.HTTP_409_CONFLICT, "invalid_command_state", "Command state changed")
    return {"status": result}


@router.post("/relay-agent/v1/commands/{command_id}/complete", include_in_schema=False)
async def relay_complete_command(
    command_id: str,
    payload: RelayCommandCompleteRequest,
    request: Request,
    token: str = Depends(_bearer_token),
) -> dict[str, str]:
    secret = payload.secret_result.get_secret_value() if payload.secret_result is not None else None
    try:
        result = _relays(request).complete_command(
            token,
            command_id,
            status=payload.status,
            completed_at=payload.completed_at.isoformat(),
            safe_result=payload.safe_result.model_dump(mode="json"),
            secret_result=secret,
        )
    except RelayAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    except RelayUnsupportedProtocolError:
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    except RelayCommandNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    except RelayCommandStateError:
        _fail(status.HTTP_409_CONFLICT, "invalid_command_state", "Command state changed")
    return {"status": result}


@router.get("/api/relay-nodes", dependencies=[Depends(require_session)])
async def list_relay_nodes(request: Request) -> dict[str, Any]:
    return {"items": _relays(request).list_nodes()}


@router.get("/api/nodes/{node_id}/relay", dependencies=[Depends(require_session)])
@router.get("/api/nodes/{node_id}/relay/status", dependencies=[Depends(require_session)])
async def get_relay_status(node_id: str, request: Request) -> dict[str, Any]:
    try:
        return _relays(request).get_status(node_id)
    except RelayNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "relay_not_found", "Relay node not found")


@router.get(
    "/api/nodes/{node_id}/relay/commands/{command_id}",
    dependencies=[Depends(require_session)],
)
async def get_relay_command(node_id: str, command_id: str, request: Request) -> dict[str, Any]:
    command = _relays(request).get_command(node_id, command_id)
    if command is None:
        _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
    return command


@router.post("/api/nodes/{node_id}/relay/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_relay(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return _queue(request, node_id, "STATUS", idempotency_key=idempotency_key)


@router.post("/api/nodes/{node_id}/relay/start", status_code=status.HTTP_202_ACCEPTED)
async def start_relay(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return _queue(request, node_id, "START", idempotency_key=idempotency_key)


@router.post("/api/nodes/{node_id}/relay/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_relay(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return _queue(request, node_id, "STOP", idempotency_key=idempotency_key)


@router.put("/api/nodes/{node_id}/relay/configure-youtube", status_code=status.HTTP_202_ACCEPTED)
async def configure_youtube(
    node_id: str,
    payload: RelayConfigureYouTubeRequest,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _step_up(request, payload.admin_password.get_secret_value(), node_id)
    return _queue(
        request,
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={
            "youtube_rtmps_url": payload.url.get_secret_value(),
            "youtube_stream_key": payload.stream_key.get_secret_value(),
        },
        idempotency_key=idempotency_key,
    )


@router.delete("/api/nodes/{node_id}/relay/youtube", status_code=status.HTTP_202_ACCEPTED)
async def clear_youtube(
    node_id: str,
    payload: RelayStepUpRequest,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _step_up(request, payload.admin_password.get_secret_value(), node_id)
    return _queue(request, node_id, "CLEAR_YOUTUBE", idempotency_key=idempotency_key)


def _parse_srt_result(secret: str) -> dict[str, str | None]:
    public_url: str | None = None
    vpn_url: str | None = None
    try:
        decoded = json.loads(secret)
    except (TypeError, ValueError):
        decoded = None
    candidates: list[tuple[str, str]] = []
    if isinstance(decoded, dict):
        for label in ("public_url", "vpn_url"):
            value = decoded.get(label)
            if isinstance(value, str):
                candidates.append((label, value.strip()))
    if not candidates:
        for line in secret.splitlines():
            for match in _SRT_TOKEN.findall(line):
                candidates.append((line.lower(), match))
    for label, candidate in candidates:
        if not candidate or len(candidate) > 4096 or any(char.isspace() for char in candidate):
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme.lower() != "srt" or parsed.hostname is None:
            continue
        normalized_label = label.strip().lower()
        label_prefix = normalized_label.split(":", 1)[0].strip().replace(" ", "_")
        explicitly_public = label_prefix in {"public", "public_url"}
        explicitly_vpn = label_prefix in {"vpn", "vpn_url"}
        is_vpn = explicitly_vpn
        if not explicitly_public and not explicitly_vpn:
            with suppress(ValueError):
                is_vpn = ip_address(parsed.hostname).is_private
        if is_vpn and vpn_url is None:
            vpn_url = candidate
        elif not is_vpn and public_url is None:
            public_url = candidate
    if public_url is None and vpn_url is None:
        raise RelayCommandStateError("relay returned no valid SRT URL")
    return {"public_url": public_url, "vpn_url": vpn_url}


@router.post("/api/nodes/{node_id}/relay/reveal-moblin-url")
async def reveal_moblin_url(
    node_id: str,
    payload: RelayStepUpRequest,
    request: Request,
    wait: Annotated[int, Query(ge=0, le=20)] = 20,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    _step_up(request, payload.admin_password.get_secret_value(), node_id)
    queued = _queue(
        request,
        node_id,
        "REVEAL_MOBLIN_URL",
        idempotency_key=idempotency_key,
    )
    command_id = str(queued["command_id"])
    deadline = monotonic() + wait
    while True:
        command = _relays(request).get_command(node_id, command_id)
        if command is None:  # pragma: no cover - command created in this request
            _fail(status.HTTP_404_NOT_FOUND, "command_not_found", "Command not found")
        if command["state"] == "completed":
            if command["completion_status"] != "ok":
                _fail(
                    status.HTTP_409_CONFLICT,
                    "relay_command_failed",
                    "Relay could not provide the Moblin URL",
                )
            try:
                secret = _relays(request).consume_secret_result(node_id, command_id)
                result = _parse_srt_result(secret)
            except RelaySecretUnavailableError:
                _fail(
                    status.HTTP_410_GONE,
                    "secret_already_consumed",
                    "Moblin URL was already revealed",
                )
            except RelayCommandStateError:
                _fail(
                    status.HTTP_502_BAD_GATEWAY,
                    "invalid_relay_result",
                    "Relay returned an invalid Moblin URL",
                )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        if command["state"] in {"failed", "cancelled"}:
            _fail(
                status.HTTP_409_CONFLICT,
                "relay_command_failed",
                "Relay could not provide the Moblin URL",
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            return JSONResponse(
                {"command_id": command_id, "state": command["state"]},
                status_code=status.HTTP_202_ACCEPTED,
                headers={"Cache-Control": "no-store"},
            )
        await asyncio.sleep(min(0.25, remaining))


__all__ = ["require_same_origin", "router"]
