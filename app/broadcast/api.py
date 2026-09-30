"""Administrator broadcast APIs. Read-only HUD credentials are never accepted here."""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import Field

from app.api import CSRF_COOKIE, SESSION_COOKIE, require_csrf, require_session
from app.broadcast.models import (
    BroadcastError,
    Input,
    OutputCreate,
    OutputIntent,
    RouteCreate,
    SessionCreate,
)
from app.broadcast.oauth import YouTubeOAuth
from app.broadcast.store import BroadcastStore
from app.broadcast.youtube import YouTube, YouTubeHTTP, YouTubeProvisioner
from app.db import utc_now
from app.relay_api import require_same_origin

router = APIRouter()


def store(request: Request) -> BroadcastStore:
    return cast(BroadcastStore, request.app.state.broadcasts)


def oauth(request: Request) -> YouTubeOAuth:
    return cast(YouTubeOAuth, request.app.state.youtube_oauth)


def mutation(request: Request, _: Annotated[dict[str, str], Depends(require_csrf)]) -> None:
    require_same_origin(request)
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise BroadcastError("cross_site_forbidden", 403)


def provider(request: Request, output_id: str) -> YouTube:
    with store(request).database.connect() as db:
        binding = store(request).row(
            db, "SELECT channel_id FROM youtube_bindings WHERE output_id=?", (output_id,)
        )
    if not binding["channel_id"]:
        raise BroadcastError("manual_output_has_no_api")
    factory = getattr(request.app.state, "youtube_provider_factory", None)
    if factory:
        return cast(YouTube, factory(binding["channel_id"]))
    return YouTubeHTTP(lambda: oauth(request).access_token(binding["channel_id"]))


@router.get("/broadcasts")
def page(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or request.app.state.sessions.get(token) is None:
        return RedirectResponse("/login", status_code=303)
    csrf = request.app.state.sessions.ensure_csrf(token, request.cookies.get(CSRF_COOKIE))
    response: Response = request.app.state.templates.TemplateResponse(
        request=request, name="broadcasts.html", context={"csrf_token": csrf}
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, secure=request.app.state.settings.cookie_secure, samesite="lax", path="/"
    )
    return response


@router.get("/api/broadcasts", dependencies=[Depends(require_session)])
def snapshot(request: Request) -> dict[str, Any]:
    return store(request).snapshot()


@router.post("/api/broadcasts/sessions", dependencies=[Depends(mutation)], status_code=201)
def create_session(
    request: Request, data: SessionCreate, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {"id": store(request).create_session(data, idempotency_key)}


@router.post(
    "/api/broadcasts/sessions/{session_id}/outputs",
    dependencies=[Depends(mutation)],
    status_code=201,
)
def create_output(
    request: Request, session_id: str, data: OutputCreate, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {"id": store(request).create_output(session_id, data, idempotency_key)}


@router.post(
    "/api/broadcasts/outputs/{output_id}/routes", dependencies=[Depends(mutation)], status_code=201
)
def add_route(
    request: Request, output_id: str, data: RouteCreate, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {"id": store(request).add_route(output_id, data.node_id, idempotency_key)}


@router.post("/api/broadcasts/outputs/{output_id}/intent", dependencies=[Depends(mutation)])
def intent(
    request: Request, output_id: str, data: OutputIntent, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {"id": store(request).intent(output_id, data.enabled, idempotency_key)}


class BatchIntent(Input):
    output_ids: list[str] = Field(min_length=1, max_length=32)
    enabled: bool


@router.post("/api/broadcasts/outputs/intent", dependencies=[Depends(mutation)])
def batch_intent(
    request: Request, data: BatchIntent, idempotency_key: Annotated[str, Header()]
) -> dict[str, Any]:
    results = []
    for output_id in dict.fromkeys(data.output_ids):
        try:
            store(request).intent(output_id, data.enabled, idempotency_key)
            results.append({"id": output_id, "accepted": True})
        except BroadcastError as exc:
            results.append({"id": output_id, "accepted": False, "code": exc.code})
    return {"results": results}


@router.post("/api/broadcasts/outputs/{output_id}/provision", dependencies=[Depends(mutation)])
def provision(request: Request, output_id: str) -> dict[str, str]:
    YouTubeProvisioner(store(request)).provision(output_id, provider(request, output_id))
    return {"status": "READY"}


@router.post("/api/broadcasts/outputs/{output_id}/youtube-status", dependencies=[Depends(mutation)])
def youtube_status(request: Request, output_id: str) -> dict[str, str]:
    with store(request).database.connect() as db:
        binding = store(request).row(
            db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,)
        )
    if not binding["broadcast_id"] or not binding["stream_id"]:
        raise BroadcastError("output_not_provisioned")
    result = provider(request, output_id).status(binding["broadcast_id"], binding["stream_id"])
    with store(request).transaction() as db:
        db.execute(
            "UPDATE youtube_bindings SET lifecycle_status=?,stream_status=?,health_status=?,"
            "updated_at=? WHERE output_id=?",
            (
                result["lifecycle_status"],
                result["stream_status"],
                result["health_status"],
                utc_now(),
                output_id,
            ),
        )
    return result


class Transition(Input):
    state: Literal["testing", "live", "complete"]


@router.post("/api/broadcasts/outputs/{output_id}/transition", dependencies=[Depends(mutation)])
def transition(request: Request, output_id: str, data: Transition) -> dict[str, str]:
    with store(request).database.connect() as db:
        binding = store(request).row(
            db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,)
        )
        if db.execute(
            "SELECT 1 FROM broadcast_switches WHERE output_id=? AND active=1", (output_id,)
        ).fetchone():
            raise BroadcastError("switch_in_progress")
        output = store(request).row(
            db, "SELECT session_id FROM broadcast_outputs WHERE id=?", (output_id,)
        )
    if not binding["broadcast_id"]:
        raise BroadcastError("output_not_provisioned")
    remote = provider(request, output_id)
    current = remote.status(binding["broadcast_id"], binding["stream_id"])
    if current["lifecycle_status"] != data.state:
        remote.transition(binding["broadcast_id"], data.state)
    with store(request).transaction() as db:
        store(request).event(
            db,
            output["session_id"],
            "youtube.transition_requested",
            output_id=output_id,
            detail={"state": data.state},
        )
    return {"status": "REQUESTED"}


@router.post("/api/broadcasts/youtube/connect", dependencies=[Depends(mutation)])
def connect(request: Request) -> dict[str, str]:
    return {"url": oauth(request).begin(request.cookies[SESSION_COOKIE])}


@router.get("/api/broadcasts/youtube/callback", dependencies=[Depends(require_session)])
def callback(request: Request, state: str, code: str) -> RedirectResponse:
    oauth(request).callback(request.cookies[SESSION_COOKIE], state, code)
    return RedirectResponse("/broadcasts", status_code=303)


@router.post("/api/broadcasts/youtube/{channel_id}/revoke", dependencies=[Depends(mutation)])
def revoke(request: Request, channel_id: str) -> dict[str, str]:
    oauth(request).revoke(channel_id)
    return {"status": "disconnected"}
