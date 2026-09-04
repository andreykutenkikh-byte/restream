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
from app.bootstrap_api import BootstrapRateLimiter
from app.bootstrap_api import router as bootstrap_router
from app.core.config import Settings
from app.core.validation import destination_validator
from app.db import Database
from app.logging_config import configure_logging
from app.login_limiter import LoginRateLimiter
from app.moblin_hud_api import HudBodyLimitMiddleware, HudRateLimiter
from app.moblin_hud_api import router as moblin_hud_router
from app.node_api import NodeBodyLimitMiddleware, NodeCommandPollGate, NodeEnrollmentGate
from app.node_api import router as node_router
from app.relay_api import router as relay_router
from app.relay_preview_api import router as relay_preview_router
from app.runtime import ApplicationRuntime, URLValidator
from app.services.bootstrap import (
    BootstrapClient,
    BootstrapCoordinator,
    UnavailableBootstrapCoordinator,
)
from app.services.mediamtx import MediaMTXClient
from app.services.moblin_hud import MoblinHudService
from app.services.nodes import NodeService
from app.services.preview import PreviewService
from app.services.relay_preview import RelayPreviewStore
from app.services.relay_quality import RelayQualityTracker
from app.services.relays import RelayService
from app.session import SessionManager
from app.step_up_limiter import StepUpRateLimiter

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(
    settings: Settings | None = None,
    *,
    mediamtx: MediaMTXClient | None = None,
    preview: PreviewService | None = None,
    url_validator: URLValidator | None = None,
    worker_launcher: Any | None = None,
    bootstrap: Any | None = None,
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
    preview_service = preview or PreviewService(
        settings.mediamtx_hls_url,
        username=settings.worker_auth_user,
        password=settings.worker_auth_password,
    )
    relays = RelayService(database, settings.master_encryption_key)
    moblin_hud = MoblinHudService(database)
    relay_preview = RelayPreviewStore()
    nodes = NodeService(
        database,
        relay_payload_tombstone=relays.encrypted_empty_payload(),
    )
    if bootstrap is not None:
        bootstrap_service = bootstrap
    elif settings.bootstrap_worker_secret:
        bootstrap_service = BootstrapCoordinator(
            database,
            nodes,
            BootstrapClient(
                settings.bootstrap_socket_path,
                settings.bootstrap_worker_secret,
            ),
            control_url=settings.public_control_url,
            node_agent_image=settings.node_agent_image,
            node_agent_environment=settings.environment,
        )
    else:
        bootstrap_service = UnavailableBootstrapCoordinator()
    background_tasks: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def maintain_node_state() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    nodes.prune_retention()
                    relays.prune_retention()
                    moblin_hud.prune_expired_pairings()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Node maintenance failed")

        database.migrate()
        recover_interrupted = getattr(bootstrap_service, "recover_interrupted_jobs", None)
        if recover_interrupted is not None:
            await recover_interrupted()
        nodes.prune_retention()
        relays.prune_retention()
        await runtime.startup()
        try:
            monitor_active = getattr(bootstrap_service, "monitor_active_jobs", None)
            if monitor_active is not None:
                monitor_task = asyncio.create_task(monitor_active())
                background_tasks.add(monitor_task)
                monitor_task.add_done_callback(background_tasks.discard)
            maintenance_task = asyncio.create_task(maintain_node_state())
            background_tasks.add(maintenance_task)
            maintenance_task.add_done_callback(background_tasks.discard)
            yield
        finally:
            relay_preview.clear()
            try:
                await preview_service.close()
            finally:
                try:
                    pending_background = tuple(background_tasks)
                    for task in pending_background:
                        task.cancel()
                    if pending_background:
                        await asyncio.gather(*pending_background, return_exceptions=True)
                    await bootstrap_service.close()
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
    app.state.preview = preview_service
    app.state.sessions = SessionManager(
        database, settings.session_secret, settings.session_ttl_seconds
    )
    app.state.login_limiter = LoginRateLimiter()
    app.state.relay_step_up_limiter = StepUpRateLimiter()
    app.state.bootstrap_limiter = BootstrapRateLimiter()
    app.state.moblin_hud = moblin_hud
    app.state.relay_quality = RelayQualityTracker()
    app.state.moblin_hud_pair_limiter = HudRateLimiter(attempts=8, window_seconds=60)
    app.state.moblin_hud_admin_limiter = HudRateLimiter(attempts=6, window_seconds=60)
    app.state.moblin_hud_status_lock = asyncio.Lock()
    app.state.moblin_hud_status_cached_at = None
    app.state.moblin_hud_status_cache = None
    app.state.moblin_hud_status_observation = None
    app.state.moblin_hud_active_route_id = None
    app.state.moblin_hud_stream_session_sequence = 0
    app.state.nodes = nodes
    app.state.relays = relays
    app.state.relay_preview = relay_preview
    app.state.bootstrap = bootstrap_service
    app.state.background_tasks = background_tasks
    app.state.node_enrollments = NodeEnrollmentGate()
    app.state.node_command_polls = NodeCommandPollGate()
    app.state.relay_command_polls = NodeCommandPollGate()
    app.state.destination_lock = asyncio.Lock()
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    # MediaMTX reaches the private auth callback through the Compose service
    # DNS name. Keep that single internal host explicit so TrustedHost remains
    # effective for every public request.
    allowed_hosts = sorted(
        {settings.public_domain, "backend", "localhost", "127.0.0.1", "testserver"}
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(NodeBodyLimitMiddleware)
    app.add_middleware(HudBodyLimitMiddleware)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(node_router)
    app.include_router(relay_router)
    app.include_router(relay_preview_router)
    app.include_router(bootstrap_router)
    app.include_router(moblin_hud_router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        if (
            request.method == "POST"
            and request.url.path == "/api/auth/logout"
            and response.status_code < 400
        ):
            relay_preview.clear()
        response.headers["X-Content-Type-Options"] = "nosniff"
        hud_path = request.url.path.startswith(("/moblin-hud", "/api/moblin-hud"))
        response.headers["Referrer-Policy"] = "no-referrer" if hud_path else "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith(
            ("/api/", "/node-api/", "/relay-agent/", "/relay-media/", "/moblin-hud")
        ) or request.url.path in {
            "/",
            "/login",
            "/servers",
        }:
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
