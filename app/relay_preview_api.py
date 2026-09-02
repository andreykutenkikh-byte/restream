"""Authenticated browser playback and node-authenticated preview ingestion."""

from __future__ import annotations

import re
from typing import Annotated, Any, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.api import require_csrf, require_session
from app.relay_api import require_same_origin
from app.services.relay_preview import (
    MAX_SEGMENT_BYTES,
    RelayPreviewCapacityExceeded,
    RelayPreviewInvalidSegment,
    RelayPreviewRateLimited,
    RelayPreviewStore,
    RelayPreviewUnavailable,
)
from app.services.relays import (
    RelayAuthenticationError,
    RelayNotFoundError,
    RelayService,
    RelayUnsupportedProtocolError,
)

router = APIRouter()
_SEQUENCE = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}


def _fail(http_status: int, code: str, message: str, **headers: str) -> NoReturn:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers=headers or None,
    )


def _store(request: Request) -> RelayPreviewStore:
    return cast(RelayPreviewStore, request.app.state.relay_preview)


def _relays(request: Request) -> RelayService:
    return cast(RelayService, request.app.state.relays)


def _bearer_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    if authorization is None:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not token or " " in token:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    return token


def _live_status(request: Request, node_id: str) -> dict[str, Any]:
    try:
        current = _relays(request).get_status(node_id)
    except RelayNotFoundError:
        _fail(status.HTTP_404_NOT_FOUND, "relay_not_found", "Relay node not found")
    relay = current.get("status", {})
    if not (
        current.get("available") is True
        and isinstance(relay, dict)
        and relay.get("service") == "active"
        and relay.get("main_process") == "running"
        and relay.get("srt_listener") == "listening"
        and relay.get("source") == "LIVE"
    ):
        _store(request).purge_node(node_id)
        _fail(status.HTTP_409_CONFLICT, "preview_not_available", "Live preview is unavailable")
    return current


def _generation(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError:
        _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_preview_segment", "Invalid segment")
    if parsed.version != 4 or str(parsed) != value:
        _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_preview_segment", "Invalid segment")
    return value


def _sequence(value: str) -> int:
    if _SEQUENCE.fullmatch(value) is None:
        _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_preview_segment", "Invalid segment")
    parsed = int(value)
    if parsed > 2**63 - 1:
        _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_preview_segment", "Invalid segment")
    return parsed


def _single_raw_header(request: Request, name: bytes) -> bytes | None:
    values = [value for key, value in request.scope.get("headers", []) if key.lower() == name]
    return values[0] if len(values) == 1 else None


def _has_raw_header(request: Request, name: bytes) -> bool:
    return any(key.lower() == name for key, _ in request.scope.get("headers", []))


@router.post("/api/nodes/{node_id}/relay/preview/lease", status_code=status.HTTP_204_NO_CONTENT)
async def renew_preview_lease(
    node_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
    __: None = Depends(require_same_origin),
) -> Response:
    _live_status(request, node_id)
    _store(request).renew(node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)


@router.get(
    "/api/nodes/{node_id}/relay/preview/index.m3u8",
    dependencies=[Depends(require_session)],
)
async def get_preview_playlist(node_id: str, request: Request) -> Response:
    _live_status(request, node_id)
    try:
        body = _store(request).playlist(node_id)
    except RelayPreviewUnavailable:
        _fail(status.HTTP_404_NOT_FOUND, "preview_not_ready", "Live preview is not ready")
    return Response(body, media_type="application/vnd.apple.mpegurl", headers=_NO_STORE)


@router.get(
    "/api/nodes/{node_id}/relay/preview/segment/{generation}/{sequence}.ts",
    dependencies=[Depends(require_session)],
)
async def get_preview_segment(
    node_id: str,
    generation: str,
    sequence: str,
    request: Request,
) -> Response:
    _live_status(request, node_id)
    generation = _generation(generation)
    sequence_number = _sequence(sequence)
    try:
        body = _store(request).segment(node_id, generation, sequence_number)
    except RelayPreviewUnavailable:
        _fail(status.HTTP_404_NOT_FOUND, "preview_segment_not_found", "Preview segment not found")
    return Response(body, media_type="video/mp2t", headers=_NO_STORE)


@router.put(
    "/relay-media/v1/preview/segments/{generation}/{sequence}",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def upload_preview_segment(
    generation: str,
    sequence: str,
    request: Request,
    token: str = Depends(_bearer_token),
) -> Response:
    generation = _generation(generation)
    sequence_number = _sequence(sequence)
    try:
        authenticated = _relays(request).authenticate(token, require_supported_protocol=True)
    except RelayAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "relay_authentication_failed", "Authentication failed")
    except RelayUnsupportedProtocolError:
        _fail(status.HTTP_409_CONFLICT, "unsupported_protocol", "Relay protocol is unsupported")
    node_id = str(authenticated["node_id"])
    _live_status(request, node_id)

    if _has_raw_header(request, b"transfer-encoding"):
        _fail(status.HTTP_400_BAD_REQUEST, "invalid_preview_segment", "Invalid segment")
    raw_length = _single_raw_header(request, b"content-length")
    raw_type = _single_raw_header(request, b"content-type")
    if raw_length is None or raw_type != b"video/mp2t":
        _fail(status.HTTP_400_BAD_REQUEST, "invalid_preview_segment", "Invalid segment")
    try:
        length_text = raw_length.decode("ascii")
    except UnicodeDecodeError:
        _fail(status.HTTP_400_BAD_REQUEST, "invalid_preview_segment", "Invalid segment")
    if re.fullmatch(r"[1-9][0-9]{0,7}", length_text) is None:
        _fail(status.HTTP_400_BAD_REQUEST, "invalid_preview_segment", "Invalid segment")
    declared_length = int(length_text)
    if declared_length > MAX_SEGMENT_BYTES:
        _fail(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "preview_segment_too_large",
            "Segment is too large",
        )

    try:
        with _store(request).reserve_upload(node_id, declared_length):
            payload = bytearray()
            async for chunk in request.stream():
                if len(chunk) > MAX_SEGMENT_BYTES - len(payload):
                    _fail(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "preview_segment_too_large",
                        "Segment is too large",
                    )
                payload.extend(chunk)
            if len(payload) != declared_length:
                _fail(status.HTTP_400_BAD_REQUEST, "invalid_preview_segment", "Invalid segment")
            _store(request).put(node_id, generation, sequence_number, bytes(payload))
    except RelayPreviewUnavailable:
        _fail(status.HTTP_409_CONFLICT, "preview_not_requested", "Preview is not requested")
    except RelayPreviewInvalidSegment:
        _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_preview_segment", "Invalid segment")
    except RelayPreviewRateLimited:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "preview_rate_limited",
            "Preview upload rate exceeded",
            **{"Retry-After": "2"},
        )
    except RelayPreviewCapacityExceeded:
        _fail(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "preview_capacity_exceeded",
            "Preview capacity is unavailable",
            **{"Retry-After": "2"},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


__all__ = ["router"]
