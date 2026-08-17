"""HTTP pages and JSON API for the single-administrator Stage 1 service."""

from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.core.redaction import redact_text
from app.core.security import encrypt_destination_key, verify_password
from app.core.validation import URLValidationError
from app.db import Database
from app.login_limiter import LoginRateLimiter
from app.runtime import ApplicationRuntime
from app.schemas import DestinationCreate, DestinationUpdate, LoginRequest, MediaMTXAuthRequest
from app.services.preview import (
    PreviewInvalidRequest,
    PreviewService,
    PreviewUnavailable,
    PreviewUpstreamError,
)
from app.session import SessionManager

SESSION_COOKIE = "adojapan_session"
CSRF_COOKIE = "adojapan_csrf"

router = APIRouter()


def _runtime(request: Request) -> ApplicationRuntime:
    return cast(ApplicationRuntime, request.app.state.runtime)


def _database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def _sessions(request: Request) -> SessionManager:
    return cast(SessionManager, request.app.state.sessions)


def _preview(request: Request) -> PreviewService:
    return cast(PreviewService, request.app.state.preview)


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _bootstrap(request: Request) -> Any:
    return request.app.state.bootstrap


def _fail(http_status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=http_status, detail={"code": code, "message": message})


def require_session(request: Request) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    session = _sessions(request).get(token)
    if session is None:
        _fail(status.HTTP_401_UNAUTHORIZED, "authentication_required", "Требуется вход")
    return session


async def require_csrf(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    csrf_token = x_csrf_token
    if not csrf_token and request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        csrf_token = str(form.get("csrf_token", ""))
    if not _sessions(request).validate_csrf(token, csrf_token):
        if _sessions(request).get(token) is None:
            _fail(status.HTTP_401_UNAUTHORIZED, "authentication_required", "Требуется вход")
        _fail(status.HTTP_403_FORBIDDEN, "csrf_failed", "Защитный токен недействителен")
    session = _sessions(request).get(token)
    if session is None:  # pragma: no cover - race with logout
        _fail(status.HTTP_401_UNAUTHORIZED, "authentication_required", "Требуется вход")
    return session


def _set_session_cookies(response: Response, request: Request, token: str, csrf: str) -> None:
    settings = _runtime(request).settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    # The CSRF token is intentionally readable by the small same-origin frontend;
    # the bearer session remains HttpOnly and is also required by the server.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response, request: Request) -> None:
    secure = _runtime(request).settings.cookie_secure
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="lax")


async def _parse_login_request(request: Request) -> LoginRequest:
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = await request.json()
        else:
            form = await request.form()
            payload = {"login": form.get("login", ""), "password": form.get("password", "")}
        return LoginRequest.model_validate(payload)
    except (ValidationError, ValueError):
        _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "Проверьте логин и пароль")


def _client_identity(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    database_ready = _database(request).ready()
    ingest = await _runtime(request).ingest_status()
    media_ready = ingest.state.value != "error"
    bootstrap_ready = await _bootstrap(request).healthy()
    payload = {
        "status": "ready" if database_ready and media_ready else "not_ready",
        "database": "ready" if database_ready else "unavailable",
        "media": "ready" if media_ready else "unavailable",
        "bootstrap": "ready" if bootstrap_ready else "unavailable",
    }
    return JSONResponse(payload, status_code=200 if database_ready and media_ready else 503)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if _sessions(request).get(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _templates(request).TemplateResponse(request=request, name="login.html", context={})


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    session_token = request.cookies.get(SESSION_COOKIE)
    if _sessions(request).get(session_token) is None or not session_token:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    csrf_token = _sessions(request).ensure_csrf(session_token, request.cookies.get(CSRF_COOKIE))
    runtime = _runtime(request)
    ingest = {
        "rtmp_server_url": runtime.settings.public_rtmp_url,
        "stream_key": runtime.ingest_key(),
        **(await runtime.ingest_view()),
    }
    response = _templates(request).TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "csrf_token": csrf_token,
            "current_user": {"login": runtime.settings.admin_login},
            "ingest": ingest,
            "destinations": runtime.list_destination_views(),
        },
    )
    if request.cookies.get(CSRF_COOKIE) != csrf_token:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=runtime.settings.session_ttl_seconds,
            httponly=False,
            secure=runtime.settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    return response


@router.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request) -> Response:
    session_token = request.cookies.get(SESSION_COOKIE)
    if _sessions(request).get(session_token) is None or not session_token:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    csrf_token = _sessions(request).ensure_csrf(session_token, request.cookies.get(CSRF_COOKIE))
    runtime = _runtime(request)
    response = _templates(request).TemplateResponse(
        request=request,
        name="servers.html",
        context={
            "csrf_token": csrf_token,
            "current_user": {"login": runtime.settings.admin_login},
            "bootstrap_available": await _bootstrap(request).healthy(),
        },
    )
    if request.cookies.get(CSRF_COOKIE) != csrf_token:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=runtime.settings.session_ttl_seconds,
            httponly=False,
            secure=runtime.settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    return response


@router.post("/api/auth/login")
async def login(request: Request) -> Response:
    limiter = cast(LoginRateLimiter, request.app.state.login_limiter)
    identity = _client_identity(request)
    if not limiter.allowed(identity):
        _fail(status.HTTP_429_TOO_MANY_REQUESTS, "login_rate_limited", "Слишком много попыток")
    credentials = await _parse_login_request(request)
    settings = _runtime(request).settings
    login_matches = hmac.compare_digest(credentials.login, settings.admin_login)
    password_matches = verify_password(credentials.password, settings.admin_password_hash)
    if not login_matches or not password_matches:
        limiter.fail(identity)
        _database(request).add_audit_event("auth.failed", "Invalid credentials")
        _fail(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Неверный логин или пароль")
    limiter.success(identity)
    new_session = _sessions(request).create()
    _database(request).add_audit_event("auth.login")
    accepts_html = "text/html" in request.headers.get("accept", "") and not request.headers.get(
        "content-type", ""
    ).startswith("application/json")
    response: Response
    if accepts_html:
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    else:
        response = JSONResponse(
            {"authenticated": True, "csrf_token": new_session.csrf_token, "redirect": "/"}
        )
    _set_session_cookies(response, request, new_session.token, new_session.csrf_token)
    return response


@router.get("/api/auth/session", dependencies=[Depends(require_session)])
async def current_session(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:  # pragma: no cover - dependency invariant
        _fail(status.HTTP_401_UNAUTHORIZED, "authentication_required", "Требуется вход")
    csrf_token = _sessions(request).ensure_csrf(token, request.cookies.get(CSRF_COOKIE))
    response = JSONResponse(
        {
            "authenticated": True,
            "login": _runtime(request).settings.admin_login,
            "csrf_token": csrf_token,
        }
    )
    if request.cookies.get(CSRF_COOKIE) != csrf_token:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=_runtime(request).settings.session_ttl_seconds,
            httponly=False,
            secure=_runtime(request).settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    return response


@router.post("/api/auth/logout")
async def logout(request: Request, _: dict[str, str] = Depends(require_csrf)) -> Response:
    _sessions(request).delete(request.cookies.get(SESSION_COOKIE))
    _database(request).add_audit_event("auth.logout")
    accepts_html = "text/html" in request.headers.get("accept", "")
    response: Response = (
        RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if accepts_html
        else JSONResponse({"authenticated": False, "redirect": "/login"})
    )
    _clear_session_cookies(response, request)
    return response


@router.post("/internal/mediamtx/auth", include_in_schema=False)
async def mediamtx_auth(request: MediaMTXAuthRequest, raw_request: Request) -> Response:
    forwarded = any(
        header in raw_request.headers
        for header in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
    )
    client_host = raw_request.client.host if raw_request.client else ""
    try:
        client_address = ip_address(client_host)
        internal_client = client_address.is_private or client_address.is_loopback
    except ValueError:
        internal_client = (
            _runtime(raw_request).settings.environment == "test" and client_host == "testclient"
        )
    if forwarded or not internal_client:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    allowed = _runtime(raw_request).authorize_mediamtx(
        action=request.action,
        protocol=request.protocol,
        path=request.path,
        user=request.user,
        password=request.password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT if allowed else 401)


@router.get("/api/ingest", dependencies=[Depends(require_session)])
async def ingest_configuration(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    key = runtime.ingest_key()
    return {
        "rtmp_server_url": runtime.settings.public_rtmp_url,
        "stream_key": key,
        "stream_key_masked": "••••••••••••",
    }


@router.get("/api/ingest/status", dependencies=[Depends(require_session)])
async def ingest_status(request: Request) -> dict[str, Any]:
    return await _runtime(request).ingest_view()


@router.get("/api/ingest/preview/{asset}", dependencies=[Depends(require_session)])
async def ingest_preview(
    asset: str,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    if asset == "index.m3u8" and not (await _runtime(request).ingest_status()).is_available:
        await _preview(request).reset()
        _fail(
            status.HTTP_404_NOT_FOUND,
            "preview_not_available",
            "Предпросмотр пока недоступен",
        )
    try:
        upstream = await _preview(request).open(
            _runtime(request).ingest_path(),
            asset,
            query_items=request.query_params.multi_items(),
            range_header=range_header,
        )
    except PreviewInvalidRequest:
        _fail(
            status.HTTP_400_BAD_REQUEST,
            "invalid_preview_asset",
            "Запрошен недопустимый ресурс предпросмотра",
        )
    except PreviewUnavailable:
        _fail(
            status.HTTP_404_NOT_FOUND,
            "preview_not_available",
            "Предпросмотр пока недоступен",
        )
    except PreviewUpstreamError:
        _fail(
            status.HTTP_502_BAD_GATEWAY,
            "preview_unavailable",
            "Не удалось открыть предпросмотр",
        )
    return StreamingResponse(
        upstream.body,
        status_code=upstream.status_code,
        headers=upstream.headers,
    )


@router.post("/api/ingest/rotate")
async def rotate_ingest(
    request: Request, _: dict[str, str] = Depends(require_csrf)
) -> dict[str, Any]:
    runtime = _runtime(request)
    key = await runtime.rotate_ingest_key()
    await _preview(request).reset()
    return {
        "rtmp_server_url": runtime.settings.public_rtmp_url,
        "stream_key": key,
        "stream_key_masked": "••••••••••••",
    }


@router.get("/api/destinations", dependencies=[Depends(require_session)])
async def list_destinations(request: Request) -> dict[str, Any]:
    items = _runtime(request).list_destination_views()
    return {"items": items, "count": len(items)}


@router.post("/api/destinations", status_code=status.HTTP_201_CREATED)
async def create_destination(
    payload: DestinationCreate,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        server_url = await asyncio.to_thread(
            runtime.validate_destination_server, payload.server_url
        )
    except URLValidationError as exc:
        _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_destination_url", str(exc))
    lock = cast(asyncio.Lock, request.app.state.destination_lock)
    async with lock:
        if _database(request).count_destinations() >= runtime.settings.max_destinations:
            _fail(
                status.HTTP_409_CONFLICT,
                "destination_limit_reached",
                f"Можно добавить не более {runtime.settings.max_destinations} площадок",
            )
        destination = _database(request).create_destination(
            name=payload.name,
            server_url=server_url,
            encrypted_key=encrypt_destination_key(
                payload.stream_key, runtime.settings.master_encryption_key
            ),
            enabled=payload.enabled,
        )
    _database(request).add_audit_event("destination.created", f"id={destination['id']}")
    if payload.enabled:
        await runtime.workers.start(int(destination["id"]))
        await asyncio.sleep(0)
        destination = _database(request).get_destination(int(destination["id"])) or destination
    return runtime.destination_view(destination)


@router.put("/api/destinations/{destination_id}")
async def update_destination(
    destination_id: int,
    payload: DestinationUpdate,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    runtime = _runtime(request)
    existing = _database(request).get_destination(destination_id)
    if existing is None:
        _fail(status.HTTP_404_NOT_FOUND, "destination_not_found", "Площадка не найдена")
    updates: dict[str, Any] = payload.model_dump(exclude_none=True)
    if "server_url" in updates:
        try:
            updates["server_url"] = await asyncio.to_thread(
                runtime.validate_destination_server, str(updates["server_url"])
            )
        except URLValidationError as exc:
            _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_destination_url", str(exc))
    if "stream_key" in updates:
        updates["stream_key_encrypted"] = encrypt_destination_key(
            str(updates.pop("stream_key")), runtime.settings.master_encryption_key
        )
    configuration_changed = "server_url" in updates or "stream_key_encrypted" in updates
    desired_enabled = bool(updates.get("enabled", existing["enabled"]))
    if configuration_changed or not desired_enabled:
        await runtime.workers.stop(destination_id)
    destination = _database(request).update_destination(destination_id, **updates)
    if destination is None:  # pragma: no cover - deletion race
        _fail(status.HTTP_404_NOT_FOUND, "destination_not_found", "Площадка не найдена")
    if desired_enabled and configuration_changed or "enabled" in updates and desired_enabled:
        await runtime.workers.start(destination_id)
    _database(request).add_audit_event("destination.updated", f"id={destination_id}")
    return runtime.destination_view(destination)


@router.delete("/api/destinations/{destination_id}")
async def delete_destination(
    destination_id: int,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> Response:
    if _database(request).get_destination(destination_id) is None:
        _fail(status.HTTP_404_NOT_FOUND, "destination_not_found", "Площадка не найдена")
    await _runtime(request).workers.remove(destination_id)
    _database(request).delete_destination(destination_id)
    _database(request).add_audit_event("destination.deleted", f"id={destination_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/destinations/{destination_id}/start")
async def start_destination(
    destination_id: int,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    runtime = _runtime(request)
    destination = _database(request).get_destination(destination_id)
    if destination is None:
        _fail(status.HTTP_404_NOT_FOUND, "destination_not_found", "Площадка не найдена")
    try:
        await asyncio.to_thread(runtime.validate_destination_server, str(destination["server_url"]))
    except URLValidationError as exc:
        _fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_destination_url", str(exc))
    destination = _database(request).update_destination(destination_id, enabled=True) or destination
    await runtime.workers.start(destination_id)
    await asyncio.sleep(0)
    return runtime.destination_view(destination)


@router.post("/api/destinations/{destination_id}/stop")
async def stop_destination(
    destination_id: int,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    runtime = _runtime(request)
    destination = _database(request).get_destination(destination_id)
    if destination is None:
        _fail(status.HTTP_404_NOT_FOUND, "destination_not_found", "Площадка не найдена")
    await runtime.workers.stop(destination_id)
    destination = (
        _database(request).update_destination(destination_id, enabled=False) or destination
    )
    return runtime.destination_view(destination)


@router.get("/api/system/diagnostics", dependencies=[Depends(require_session)])
async def diagnostics(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    ingest = await runtime.ingest_status()
    workers = runtime.workers.all_statuses()
    return {
        "environment": runtime.settings.environment,
        "database_ready": _database(request).ready(),
        "ingest_state": ingest.state.value,
        "worker_counts": {
            state: sum(1 for worker in workers if worker.state.value == state)
            for state in (
                "stopped",
                "waiting_for_input",
                "connecting",
                "live",
                "reconnecting",
                "failed",
            )
        },
        "recent_events": [
            {
                **event,
                "detail": redact_text(event["detail"]) if event.get("detail") else None,
            }
            for event in _database(request).list_audit_events()
        ],
        "checked_at": datetime.now(UTC).isoformat(),
    }
