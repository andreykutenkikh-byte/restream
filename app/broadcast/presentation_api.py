from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request

from app.api import require_session
from app.broadcast.api import mutation, store
from app.broadcast.presentation import (
    BroadcastPresentation,
    Connection,
    Prepare,
    ServerSelection,
    YouTubeSettings,
)

router = APIRouter()


def presentation(request: Request) -> BroadcastPresentation:
    return BroadcastPresentation(
        store(request), request.app.state.broadcast_media, request.app.state.broadcast_switches
    )


@router.get("/api/broadcasts/ui-state", dependencies=[Depends(require_session)])
def state(request: Request) -> dict[str, Any]:
    return presentation(request).state()


@router.post("/api/broadcasts/prepare", dependencies=[Depends(mutation)])
def prepare(
    request: Request, data: Prepare, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return presentation(request).prepare(data, idempotency_key)


@router.post("/api/broadcasts/sessions/{session_id}/connection", dependencies=[Depends(mutation)])
def connection(request: Request, session_id: str, data: Connection) -> dict[str, Any]:
    return presentation(request).connection(session_id, data.target_route_id, data.protocol)


@router.post("/api/broadcasts/outputs/{output_id}/connection", dependencies=[Depends(mutation)])
def youtube(
    request: Request,
    output_id: str,
    data: YouTubeSettings,
    idempotency_key: Annotated[str, Header()],
) -> dict[str, bool]:
    presentation(request).save_youtube(output_id, data, idempotency_key)
    return {"saved": True}


@router.post("/api/broadcasts/outputs/{output_id}/server", dependencies=[Depends(mutation)])
def select_server(
    request: Request,
    output_id: str,
    data: ServerSelection,
    idempotency_key: Annotated[str, Header()],
) -> dict[str, bool]:
    presentation(request).select_server(output_id, data.target_route_id, idempotency_key)
    return {"selected": True}
