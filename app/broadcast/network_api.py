from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from starlette.types import ASGIApp

from app.broadcast.api import mutation
from app.broadcast.media_api import control
from app.broadcast.network_models import ObsMeasurement
from app.moblin_hud_api import HudBodyLimitMiddleware
from app.relay_api import _bearer_token

router = APIRouter()


class ObsBodyLimitMiddleware(HudBodyLimitMiddleware):
    path_prefixes = ("/obs-monitor/",)

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app, max_body_bytes=4096)


@router.post("/api/broadcasts/routes/{route_id}/network-probe", dependencies=[Depends(mutation)])
def probe(
    request: Request, route_id: str, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {"job_id": control(request).network.start_probe(route_id, idempotency_key)}


@router.post("/api/broadcasts/sources/{source_id}/obs-monitor", dependencies=[Depends(mutation)])
def pair(request: Request, source_id: str) -> dict[str, Any]:
    return {"token": control(request).network.pair_obs(source_id), "source_id": source_id}


@router.post(
    "/api/broadcasts/sources/{source_id}/obs-monitor/revoke", dependencies=[Depends(mutation)]
)
def revoke(request: Request, source_id: str) -> dict[str, bool]:
    control(request).network.revoke_obs(source_id)
    return {"revoked": True}


@router.post("/obs-monitor/v1/sample")
def sample(
    request: Request, data: ObsMeasurement, token: Annotated[str, Depends(_bearer_token)]
) -> dict[str, bool]:
    control(request).network.record_obs(token, data)
    return {"accepted": True}
