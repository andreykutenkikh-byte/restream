"""Administrative API for one-click node bootstrap jobs."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from time import monotonic
from typing import Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.api import require_csrf, require_session
from app.services.bootstrap import (
    BootstrapCoordinator,
    BootstrapJobConflict,
    BootstrapJobNotFound,
    BootstrapRejected,
    BootstrapUnavailable,
    BootstrapWorkerRestarted,
)

router = APIRouter()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BootstrapRequest(StrictModel):
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)
    expected_host_fingerprint: str | None = Field(default=None, max_length=128)

    @field_validator("address")
    @classmethod
    def validate_address_shape(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("Адрес содержит недопустимые символы")
        if any(fragment in value for fragment in ("://", "@", "/", "?", "#", "\\")):
            raise ValueError("Введите hostname или IP-адрес без URL")
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", value):
            raise ValueError("SSH-логин содержит недопустимые символы")
        return value

    @field_validator("expected_host_fingerprint")
    @classmethod
    def normalize_fingerprint(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", normalized):
            raise ValueError("SSH fingerprint должен иметь формат SHA256")
        return normalized.rstrip("=")


class SudoPasswordRequest(StrictModel):
    sudo_password: SecretStr = Field(min_length=1, max_length=1024)


class BootstrapRateLimiter:
    """Small per-client in-memory limiter for the single-admin deployment."""

    def __init__(self, *, attempts: int = 5, window_seconds: float = 600.0) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, identity: str) -> bool:
        now = monotonic()
        entries = self._attempts[identity]
        while entries and entries[0] <= now - self.window_seconds:
            entries.popleft()
        if len(entries) >= self.attempts:
            return False
        entries.append(now)
        return True


def _bootstrap(request: Request) -> BootstrapCoordinator:
    return cast(BootstrapCoordinator, request.app.state.bootstrap)


def _limiter(request: Request) -> BootstrapRateLimiter:
    return cast(BootstrapRateLimiter, request.app.state.bootstrap_limiter)


def _fail(http_status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _client_identity(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _handle_error(exc: Exception) -> NoReturn:
    if isinstance(exc, BootstrapJobNotFound):
        _fail(status.HTTP_404_NOT_FOUND, exc.code, "Задача подключения не найдена")
    if isinstance(exc, BootstrapJobConflict):
        _fail(
            status.HTTP_409_CONFLICT,
            exc.code,
            "Другая задача подключения уже выполняется",
        )
    if isinstance(exc, BootstrapWorkerRestarted):
        _fail(
            status.HTTP_409_CONFLICT,
            exc.code,
            "Сервис установки был перезапущен. Введите пароль повторно.",
        )
    if isinstance(exc, BootstrapRejected):
        _fail(status.HTTP_409_CONFLICT, exc.code, str(exc))
    if isinstance(exc, BootstrapUnavailable):
        _fail(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            exc.code,
            "Установка серверов временно недоступна",
        )
    raise exc


@router.post("/api/nodes/bootstrap", status_code=status.HTTP_202_ACCEPTED)
async def create_bootstrap_job(
    payload: BootstrapRequest,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    if not _limiter(request).allow(_client_identity(request)):
        _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "bootstrap_rate_limited",
            "Слишком много попыток подключения. Повторите позднее.",
        )
    try:
        return await _bootstrap(request).create_job(
            address=payload.address,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            expected_host_fingerprint=payload.expected_host_fingerprint,
        )
    except (
        BootstrapJobConflict,
        BootstrapRejected,
        BootstrapUnavailable,
        BootstrapWorkerRestarted,
    ) as exc:
        _handle_error(exc)
    finally:
        payload.password = SecretStr("")


@router.get(
    "/api/nodes/bootstrap/active",
    dependencies=[Depends(require_session)],
)
async def get_active_bootstrap_job(request: Request) -> dict[str, Any] | None:
    try:
        return await _bootstrap(request).get_active_job()
    except (
        BootstrapRejected,
        BootstrapUnavailable,
        BootstrapWorkerRestarted,
    ) as exc:
        _handle_error(exc)


@router.get(
    "/api/nodes/bootstrap/{job_id}",
    dependencies=[Depends(require_session)],
)
async def get_bootstrap_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _bootstrap(request).get_job(job_id)
    except (
        BootstrapJobNotFound,
        BootstrapRejected,
        BootstrapUnavailable,
        BootstrapWorkerRestarted,
    ) as exc:
        _handle_error(exc)


@router.post("/api/nodes/bootstrap/{job_id}/sudo-password")
async def provide_sudo_password(
    job_id: str,
    payload: SudoPasswordRequest,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await _bootstrap(request).provide_sudo_password(job_id, payload.sudo_password)
    except (
        BootstrapJobNotFound,
        BootstrapRejected,
        BootstrapUnavailable,
        BootstrapWorkerRestarted,
    ) as exc:
        _handle_error(exc)
    finally:
        payload.sudo_password = SecretStr("")


@router.post("/api/nodes/bootstrap/{job_id}/cancel")
async def cancel_bootstrap_job(
    job_id: str,
    request: Request,
    _: dict[str, str] = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await _bootstrap(request).cancel_job(job_id)
    except (
        BootstrapJobNotFound,
        BootstrapRejected,
        BootstrapUnavailable,
        BootstrapWorkerRestarted,
    ) as exc:
        _handle_error(exc)


__all__ = ["BootstrapRateLimiter", "BootstrapRequest", "SudoPasswordRequest", "router"]
