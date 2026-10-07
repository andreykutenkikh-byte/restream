from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from starlette.types import ASGIApp

from app.broadcast.api import mutation
from app.broadcast.media_control import MediaControl, MediaHeartbeat, MediaNodeEnable
from app.broadcast.models import BroadcastError
from app.moblin_hud_api import HudBodyLimitMiddleware
from app.relay_api import _bearer_token
from app.services.nodes import NodeAuthenticationError
from app.services.relays import RelayAuthenticationError

router = APIRouter()


class MediaBodyLimitMiddleware(HudBodyLimitMiddleware):
    path_prefixes = ("/broadcast-agent/",)

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app, max_body_bytes=65536)


def control(request: Request) -> MediaControl:
    return cast(MediaControl, request.app.state.broadcast_media)


@router.post("/api/broadcasts/nodes/{node_id}/enable", dependencies=[Depends(mutation)])
def enable(request: Request, node_id: str, data: MediaNodeEnable) -> dict[str, str]:
    control(request).enable(node_id, data)
    return {"status": "enabled_waiting_for_v2_heartbeat"}


@router.post("/broadcast-agent/v2/heartbeat")
def heartbeat(
    request: Request, data: MediaHeartbeat, token: Annotated[str, Depends(_bearer_token)]
) -> dict[str, Any]:
    try:
        node = request.app.state.relays.authenticate(token)
    except RelayAuthenticationError:
        # The explicit v2 opt-in and pinned key below authorize media capability.
        # Keep both legacy authentication domains unchanged at their own APIs.
        try:
            node = request.app.state.nodes.authenticate(token)
        except NodeAuthenticationError:
            raise BroadcastError("media_authentication_failed", 401) from None
    return control(request).heartbeat(str(node["node_id"]), data)
