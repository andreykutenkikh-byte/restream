"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api import router
from app.core.config import Settings
from app.core.validation import destination_validator
from app.db import Database
from app.logging_config import configure_logging
from app.login_limiter import LoginRateLimiter
from app.runtime import ApplicationRuntime, URLValidator
from app.services.mediamtx import MediaMTXClient
from app.session import SessionManager

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(
    settings: Settings | None = None,
    *,
    mediamtx: MediaMTXClient | None = None,
    url_validator: URLValidator | None = None,
    worker_launcher: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    database = Database(settings.database_path)
    runtime_kwargs: dict[str, Any] = {
        "mediamtx": mediamtx,
        "worker_launcher": worker_launcher,
    }
    runtime_kwargs["url_validator"] = url_validator or destination_validator(
        environment=settings.environment,
        test_allowlist=settings.test_destination_allowlist,
    )
    runtime = ApplicationRuntime(settings, database, **runtime_kwargs)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.migrate()
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(
        title="AdoJapan Restream",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.runtime = runtime
    app.state.sessions = SessionManager(
        database, settings.session_secret, settings.session_ttl_seconds
    )
    app.state.login_limiter = LoginRateLimiter()
    app.state.destination_lock = asyncio.Lock()
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    # MediaMTX reaches the private auth callback through the Compose service
    # DNS name. Keep that single internal host explicit so TrustedHost remains
    # effective for every public request.
    allowed_hosts = sorted(
        {settings.public_domain, "backend", "localhost", "127.0.0.1", "testserver"}
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/") or request.url.path in {"/", "/login"}:
            response.headers["Cache-Control"] = "no-store"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        detail = cast(Any, exc.detail)
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            error = detail
        else:
            error = {"code": "http_error", "message": str(detail)}
        return JSONResponse({"error": error}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(item) for item in first.get("loc", ()) if item != "body")
        message = "Проверьте введённые данные"
        if location:
            message = f"Некорректное поле: {location}"
        return JSONResponse(
            {"error": {"code": "validation_error", "message": message}},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled application error", exc_info=exc)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Не удалось выполнить операцию. Попробуйте ещё раз.",
                }
            },
            status_code=500,
        )

    return app
