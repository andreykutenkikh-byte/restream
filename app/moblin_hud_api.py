"""Read-only Moblin HUD pages, pairing, and normalized stream status API."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from threading import Lock
from time import monotonic
from typing import Annotated, Any, Final, NoReturn, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import require_csrf, require_session
from app.relay_api import require_same_origin
from app.runtime import ApplicationRuntime
from app.services.moblin_hud import (
    HUD_SESSION_TTL_SECONDS,
    DeviceLimitError,
    ExpiredPairingTokenError,
    HudDeviceNotFoundError,
    HudSessionAuthenticationError,
    InvalidPairingTokenError,
    MoblinHudService,
    PairingLimitError,
    UsedPairingTokenError,
)
from app.services.relay_quality import RelayQualityTracker, StreamRouteSnapshot
from app.services.relays import RelayService

HUD_SESSION_COOKIE: Final = "__Host-adojapan_hud_session"
MAX_HUD_BODY_BYTES: Final = 4 * 1024
MIN_QUALITY_SAMPLE_INTERVAL_SECONDS: Final = 1.5
PAIR_TOKEN_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
SAFE_ERROR_CODE_PATTERN: Final = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
IPV4_ADDRESS_PATTERN: Final = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
IPV6_ADDRESS_PATTERN: Final = re.compile(r"(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}", re.I)
LOCATOR_PATTERN: Final = re.compile(r"\b(?:https?|rtmps?|srt|ssh)\s*[:/]", re.I)

router = APIRouter()


class HudBodyLimitMiddleware:
    """Bound HUD mutation bodies before JSON or form parsing."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_HUD_BODY_BYTES) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if (
            scope["type"] != "http"
            or not path.startswith(("/moblin-hud/api/", "/api/moblin-hud/"))
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
                await self._reject(scope, receive, send, 400, "invalid_content_length")
                return
            if declared_length < 0:
                await self._reject(scope, receive, send, 400, "invalid_content_length")
                return
            if declared_length > self.max_body_bytes:
                await self._reject(scope, receive, send, 413, "payload_too_large")
                return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send, 413, "payload_too_large")
                return
            more_body = bool(message.get("more_body", False))

        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
    ) -> None:
        response = JSONResponse(
            {"error": {"code": code, "message": "Invalid request"}},
            status_code=status_code,
        )
        await response(scope, receive, send)


class HudRateLimiter:
    """Small bounded sliding-window limiter for HUD pairing operations."""

    def __init__(
        self,
        *,
        attempts: int,
        window_seconds: float,
        max_identities: int = 1024,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if attempts < 1 or window_seconds <= 0 or max_identities < 1:
            raise ValueError("rate limit parameters must be positive")
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_identities = max_identities
        self._clock = clock
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def hit(self, identity: str) -> int | None:
        with self._lock:
            return self._hit_unlocked(identity)

    def _hit_unlocked(self, identity: str) -> int | None:
        now = self._clock()
        values = self._attempts.pop(identity, deque())
        cutoff = now - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()
        if len(values) >= self.attempts:
            self._attempts[identity] = values
            return max(1, math.ceil(values[0] + self.window_seconds - now))
        values.append(now)
        self._attempts[identity] = values
        while len(self._attempts) > self.max_identities:
            self._attempts.popitem(last=False)
        return None


class PairingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str = Field(default="Moblin iPhone", min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValueError("display_name must be printable")
        return normalized


def _fail(http_status: int, code: str, message: str, **headers: str) -> NoReturn:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers=headers or None,
    )


def _hud(request: Request) -> MoblinHudService:
    return cast(MoblinHudService, request.app.state.moblin_hud)


def _quality(request: Request) -> RelayQualityTracker:
    return cast(RelayQualityTracker, request.app.state.relay_quality)


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _relays(request: Request) -> RelayService:
    return cast(RelayService, request.app.state.relays)


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _identity(request: Request, purpose: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{purpose}:{host}"


def _limit(request: Request, state_name: str, purpose: str) -> None:
    limiter = cast(HudRateLimiter, getattr(request.app.state, state_name))
    retry_after = limiter.hit(_identity(request, purpose))
    if retry_after is not None:
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many requests",
            **{"Retry-After": str(retry_after)},
        )


def require_hud_session(request: Request) -> dict[str, Any]:
    token = request.cookies.get(HUD_SESSION_COOKIE)
    try:
        return cast(dict[str, Any], _hud(request).authenticate_session(token))
    except HudSessionAuthenticationError:
        _fail(status.HTTP_401_UNAUTHORIZED, "hud_authentication_required", "HUD access required")


async def _parse_pairing_token(request: Request) -> str:
    try:
        payload = await request.json()
    except (UnicodeDecodeError, ValueError):
        _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_pairing_request", "Invalid request")
    if not isinstance(payload, dict) or set(payload) != {"token"}:
        _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_pairing_request", "Invalid request")
    pairing_token = payload.get("token")
    if not isinstance(pairing_token, str) or not PAIR_TOKEN_PATTERN.fullmatch(pairing_token):
        _fail(
            status.HTTP_401_UNAUTHORIZED, "pairing_rejected", "Pairing link is invalid or expired"
        )
    return pairing_token


def _safe_name(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split()).strip()[:128]
    if (
        not normalized
        or IPV4_ADDRESS_PATTERN.search(normalized)
        or IPV6_ADDRESS_PATTERN.search(normalized)
        or LOCATOR_PATTERN.search(normalized)
        or "/" in normalized
        or "\\" in normalized
        or "@" in normalized
    ):
        return fallback
    for part in re.split(r"[\s\[\](){},/]+", normalized):
        candidate = part.strip(".;")
        if not candidate:
            continue
        try:
            ip_address(candidate)
        except ValueError:
            continue
        return fallback
    return normalized


def _safe_error_code(value: Any) -> str | None:
    if value is None:
        return None
    candidate = str(value)
    return candidate if SAFE_ERROR_CODE_PATTERN.fullmatch(candidate) else "relay_error"


def _bounded_number(value: Any, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def _bounded_integer(value: Any, *, maximum: int) -> int | None:
    number = _bounded_number(value, maximum=float(maximum))
    return round(number) if number is not None else None


def _heartbeat_age(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _youtube_forward_state(destinations: list[dict[str, Any]]) -> str:
    enabled = [item for item in destinations if bool(item.get("enabled"))]
    states = {str(item.get("state", "unknown")).lower() for item in enabled}
    if states & {"failed", "error"}:
        return "failed"
    if states & {"running", "live", "active"}:
        return "active"
    if states & {"starting", "connecting", "reconnecting", "waiting", "waiting_for_input"}:
        return "connecting"
    return "inactive" if enabled or destinations else "unknown"


async def _main_snapshot(request: Request) -> tuple[StreamRouteSnapshot, tuple[Any, ...]]:
    runtime = _runtime(request)
    try:
        ingest = await runtime.ingest_view()
    except Exception:
        snapshot = StreamRouteSnapshot(
            route_id="main",
            display_name="Основной сервер",
            kind="main",
            available=False,
            live=False,
            ready=False,
            source="UNKNOWN",
            input_bitrate_bps=None,
            youtube_forward_state="unknown",
            overall_state="unavailable",
            heartbeat_age_seconds=None,
            error_code="main_status_unavailable",
            recommendation_eligible=False,
        )
        return snapshot, ("main", "unavailable")
    state = str(ingest.get("state", "offline")).lower()
    live = state in {"live", "unstable"}
    destinations = runtime.list_destination_views()
    youtube = _youtube_forward_state(destinations)
    metadata_value = ingest.get("metadata")
    metadata: dict[str, Any] = (
        cast(dict[str, Any], metadata_value) if isinstance(metadata_value, dict) else {}
    )
    width = _bounded_integer(metadata.get("width"), maximum=16384)
    height = _bounded_integer(metadata.get("height"), maximum=16384)
    portrait_valid = width is None or height is None or height > width
    overall = "healthy" if live and state == "live" and youtube != "failed" else "ok"
    if state == "unstable" or youtube == "failed":
        overall = "degraded"
    if state == "error":
        overall = "failed"
    snapshot = StreamRouteSnapshot(
        route_id="main",
        display_name="Основной сервер",
        kind="main",
        available=state != "error",
        live=live,
        ready=state != "error",
        source="UNKNOWN" if state == "error" else "LIVE" if live else "NONE",
        input_bitrate_bps=_bounded_integer(ingest.get("bitrate_bps"), maximum=1_000_000_000),
        youtube_forward_state=youtube,
        overall_state=overall,
        # This is a successful direct ingest read, not a relay heartbeat. Its
        # freshness advances independently from whether media bytes changed.
        heartbeat_age_seconds=None if state == "error" else 0.0,
        error_code=("youtube_forward_failed" if youtube == "failed" else None),
        recommendation_eligible=False,
        portrait_profile_valid=portrait_valid,
        youtube_configured=bool(destinations),
        # The ingest API does not report independent service/process/listener
        # lifecycle. An offline input (or API error) cannot prove manual stop.
    )
    observation = (
        "main",
        state,
        ingest.get("bytes_received"),
        snapshot.input_bitrate_bps,
        youtube,
        overall,
        portrait_valid,
    )
    return snapshot, observation


def _relay_snapshots(
    request: Request,
) -> tuple[list[StreamRouteSnapshot], tuple[tuple[Any, ...], ...]]:
    node_views = {
        str(item.get("id")): item
        for item in request.app.state.nodes.list_nodes()
        if isinstance(item, dict) and item.get("id") is not None
    }
    snapshots: list[StreamRouteSnapshot] = []
    observations: list[tuple[Any, ...]] = []
    for index, relay in enumerate(_relays(request).list_nodes(), start=1):
        node_id = str(relay.get("node_id", ""))
        if not node_id:
            continue
        node = node_views.get(node_id, {})
        relay_status_value = relay.get("status")
        relay_status: dict[str, Any] = (
            cast(dict[str, Any], relay_status_value) if isinstance(relay_status_value, dict) else {}
        )
        node_status = str(node.get("status", "offline")).lower()
        source = str(relay_status.get("source", "UNKNOWN")).upper()
        available = bool(relay.get("available")) and node_status not in {"revoked", "failed"}
        memory_total = _bounded_integer(node.get("memory_total_bytes"), maximum=2**63 - 1)
        memory_available = _bounded_integer(node.get("memory_available_bytes"), maximum=2**63 - 1)
        if memory_total is not None and memory_available is not None:
            memory_available = min(memory_available, memory_total)
        snapshot = StreamRouteSnapshot(
            route_id=node_id[:128],
            display_name=_safe_name(relay.get("display_name"), f"Relay {index}"),
            kind="relay",
            available=available,
            live=source == "LIVE",
            ready=available and node_status == "ready",
            source=source,
            input_bitrate_bps=_bounded_integer(
                relay_status.get("input_bitrate_bps"), maximum=1_000_000_000
            ),
            youtube_forward_state=str(relay_status.get("youtube_forward", "unknown")).lower()[:32],
            overall_state=(
                "revoked"
                if node_status == "revoked"
                else str(relay_status.get("overall", node_status)).lower()[:32]
            ),
            heartbeat_age_seconds=_heartbeat_age(relay.get("last_seen_at")),
            host_cpu_percent=_bounded_number(node.get("cpu_percent"), maximum=100.0),
            host_memory_available_bytes=memory_available,
            host_memory_total_bytes=memory_total,
            error_code=_safe_error_code(relay_status.get("error_code")),
            pending_command=("pending" if node.get("current_command_id") else None),
            recommendation_eligible=node_status == "ready",
            portrait_profile_valid=bool(relay_status.get("portrait_profile")),
            youtube_configured=bool(relay_status.get("youtube_url_configured"))
            and bool(relay_status.get("youtube_key_configured")),
            service_state=str(relay_status.get("service", "unknown")).lower()[:32],
            main_process_state=str(relay_status.get("main_process", "unknown")).lower()[:32],
            srt_listener_state=str(relay_status.get("srt_listener", "unknown")).lower()[:32],
        )
        snapshots.append(snapshot)
        observations.append(
            (
                node_id,
                relay.get("last_seen_at"),
            )
        )
    return snapshots, tuple(observations)


async def _status_payload(request: Request) -> dict[str, Any]:
    async with request.app.state.moblin_hud_status_lock:
        observed_at = monotonic()
        cached_at = request.app.state.moblin_hud_status_cached_at
        cached_payload = request.app.state.moblin_hud_status_cache
        if (
            isinstance(cached_at, (int, float))
            and isinstance(cached_payload, dict)
            and 0 <= observed_at - cached_at < MIN_QUALITY_SAMPLE_INTERVAL_SECONDS
        ):
            return cast(dict[str, Any], cached_payload)
        main_result, relay_result = await asyncio.gather(
            _main_snapshot(request),
            asyncio.to_thread(_relay_snapshots, request),
        )
        main, main_observation = main_result
        relays, relay_observations = relay_result
        snapshots = [main, *relays]
        previous = request.app.state.moblin_hud_status_observations
        observations = {str(item[0]): item for item in (main_observation, *relay_observations)}
        new_sample_ids = {
            route_id
            for route_id, identity in observations.items()
            if previous.get(route_id) != identity
        }
        evaluation = _quality(request).evaluate(
            snapshots,
            now=observed_at,
            # Route/session lifecycle belongs to the tracker. A lost LIVE flag
            # must never manufacture a new session or clear its recovery clock.
            stream_session_id=None,
            new_sample_route_ids=new_sample_ids,
        )
        payload = {
            "scope": "stream_monitor",
            "generated_at": datetime.now(UTC).isoformat(),
            **evaluation.as_payload(),
        }
        request.app.state.moblin_hud_status_cached_at = observed_at
        request.app.state.moblin_hud_status_cache = payload
        request.app.state.moblin_hud_status_observations = {
            route_id: observations[route_id]
            for route_id in _quality(request).tracked_route_ids
            if route_id in observations
        }
        return payload


@router.get("/moblin-hud", response_class=HTMLResponse)
async def hud_page(request: Request) -> Response:
    paired = False
    try:
        _hud(request).authenticate_session(request.cookies.get(HUD_SESSION_COOKIE))
        paired = True
    except HudSessionAuthenticationError:
        pass
    return _templates(request).TemplateResponse(
        request=request,
        name="moblin_hud.html",
        context={"hud_paired": paired},
    )


@router.post(
    "/api/moblin-hud/pairings",
    dependencies=[
        Depends(require_session),
        Depends(require_csrf),
        Depends(require_same_origin),
    ],
)
async def create_pairing(request: Request, body: PairingCreateRequest) -> dict[str, str]:
    _limit(request, "moblin_hud_admin_limiter", "admin-pairing")
    try:
        grant = _hud(request).create_pairing(body.display_name)
    except PairingLimitError:
        _fail(409, "pairing_limit_reached", "Revoke or wait for an existing pairing")
    except DeviceLimitError:
        _fail(409, "device_limit_reached", "Revoke an existing HUD device")
    base_url = request.app.state.settings.public_control_url.rstrip("/")
    pairing_url = f"{base_url}/moblin-hud#pair={grant.pairing_token}"
    settings_json = json.dumps(
        {"webBrowser": {"home": pairing_url}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "device_id": grant.device_id,
        "pairing_url": pairing_url,
        "moblin_url": f"moblin://?{quote(settings_json, safe='')}",
        "expires_at": grant.expires_at,
    }


@router.get(
    "/api/moblin-hud/devices",
    dependencies=[Depends(require_session)],
)
async def list_hud_devices(request: Request) -> dict[str, Any]:
    return {"items": _hud(request).list_devices()}


@router.post(
    "/api/moblin-hud/devices/{device_id}/revoke",
    dependencies=[
        Depends(require_session),
        Depends(require_csrf),
        Depends(require_same_origin),
    ],
)
async def revoke_hud_device(request: Request, device_id: str) -> dict[str, Any]:
    try:
        device = _hud(request).revoke_device(device_id)
    except HudDeviceNotFoundError:
        _fail(404, "hud_device_not_found", "HUD device not found")
    return {"device": device}


@router.post(
    "/moblin-hud/api/pair",
    dependencies=[Depends(require_same_origin)],
)
async def pair_hud(request: Request, response: Response) -> dict[str, Any]:
    _limit(request, "moblin_hud_pair_limiter", "device-pairing")
    pairing_token = await _parse_pairing_token(request)
    try:
        grant = _hud(request).consume_pairing(pairing_token)
    except (InvalidPairingTokenError, ExpiredPairingTokenError, UsedPairingTokenError):
        _fail(401, "pairing_rejected", "Pairing link is invalid or expired")
    except DeviceLimitError:
        _fail(409, "device_limit_reached", "HUD device limit reached")
    response.set_cookie(
        HUD_SESSION_COOKIE,
        grant.session_token,
        max_age=HUD_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"paired": True, "scope": grant.scope, "device_id": grant.device_id}


@router.get(
    "/moblin-hud/api/status",
    dependencies=[Depends(require_hud_session)],
)
async def hud_status(request: Request) -> dict[str, Any]:
    return await _status_payload(request)


@router.post(
    "/moblin-hud/api/logout",
    dependencies=[Depends(require_same_origin)],
)
async def hud_logout(
    request: Request,
    response: Response,
    device: Annotated[dict[str, Any], Depends(require_hud_session)],
) -> dict[str, bool]:
    _hud(request).revoke_device(str(device["id"]))
    response.delete_cookie(
        HUD_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {"logged_out": True}


__all__ = [
    "HUD_SESSION_COOKIE",
    "MAX_HUD_BODY_BYTES",
    "MIN_QUALITY_SAMPLE_INTERVAL_SECONDS",
    "HudBodyLimitMiddleware",
    "HudRateLimiter",
    "require_hud_session",
    "router",
]
