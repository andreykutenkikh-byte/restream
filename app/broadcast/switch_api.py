from __future__ import annotations

import hmac
import json
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from pydantic import Field

from app.api import require_session
from app.broadcast.api import mutation, store
from app.broadcast.models import BroadcastError, Input
from app.broadcast.operator import OperatorService
from app.broadcast.read_model import limited_snapshot
from app.broadcast.switching import SwitchController
from app.moblin_hud_api import HudBodyLimitMiddleware, require_hud_session
from app.relay_api import require_same_origin

router = APIRouter()
COOKIE = "__Host-adojapan_stream_operator"


class OperatorBodyLimit(HudBodyLimitMiddleware):
    path_prefixes = ("/stream-operator/api/",)


class SwitchRequest(Input):
    target_route_id: str = Field(min_length=1, max_length=64)
    handoff_ingress: bool = True


class PairingRequest(Input):
    label: str = Field(min_length=1, max_length=80)
    allow_switching: Literal[True]
    ttl_minutes: int = Field(default=60, ge=5, le=240)


class PairToken(Input):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def service(request: Request) -> OperatorService:
    return OperatorService(store(request))


def controller(request: Request) -> SwitchController:
    return cast(SwitchController, request.app.state.broadcast_switches)


def origin(request: Request) -> None:
    require_same_origin(request)
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise BroadcastError("cross_site_forbidden", 403)


def operator(request: Request) -> dict[str, Any]:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise BroadcastError("cross_site_forbidden", 403)
    return service(request).authenticate(request.cookies.get(COOKIE))


def operator_mutation(
    request: Request, grant: Annotated[dict[str, Any], Depends(operator)]
) -> dict[str, Any]:
    origin(request)
    expected = store(request).fingerprint(["operator-csrf", request.cookies.get(COOKIE)])
    if not hmac.compare_digest(expected, request.headers.get("x-csrf-token", "")):
        raise BroadcastError("operator_csrf_rejected", 403)
    if request.app.state.operator_limiter.hit(grant["id"]) is not None:
        raise BroadcastError("operator_rate_limited", 429)
    return grant


def scoped_output(request: Request, grant: dict[str, Any], output_id: str) -> None:
    with store(request).database.connect() as db:
        if not db.execute(
            "SELECT 1 FROM broadcast_outputs WHERE id=? AND session_id=?",
            (output_id, grant["session_id"]),
        ).fetchone():
            raise BroadcastError("operator_scope_forbidden", 403)


@router.post("/api/broadcasts/outputs/{output_id}/switch", dependencies=[Depends(mutation)])
def admin_switch(
    request: Request, output_id: str, data: SwitchRequest, idempotency_key: Annotated[str, Header()]
) -> dict[str, str]:
    return {
        "id": controller(request).request(
            output_id, data.target_route_id, idempotency_key, handoff_ingress=data.handoff_ingress
        )
    }


@router.post("/api/broadcasts/switches/{switch_id}/cancel", dependencies=[Depends(mutation)])
def admin_cancel(request: Request, switch_id: str) -> dict[str, bool]:
    controller(request).cancel(switch_id)
    return {"accepted": True}


@router.post("/api/broadcasts/sessions/{session_id}/operators", dependencies=[Depends(mutation)])
def create_pairing(request: Request, session_id: str, data: PairingRequest) -> dict[str, str]:
    return service(request).create(session_id, data.label, data.ttl_minutes)


@router.get("/api/broadcasts/operators", dependencies=[Depends(require_session)])
def list_operators(request: Request) -> dict[str, Any]:
    with store(request).database.connect() as db:
        return {
            "operators": [
                dict(r)
                for r in db.execute(
                    "SELECT id,session_id,label,expires_at,revoked_at FROM "
                    "broadcast_operators ORDER BY created_at DESC LIMIT 200"
                )
            ]
        }


@router.post("/api/broadcasts/operators/{operator_id}/revoke", dependencies=[Depends(mutation)])
def revoke(request: Request, operator_id: str) -> dict[str, bool]:
    service(request).revoke(operator_id)
    return {"revoked": True}


@router.get("/stream-operator")
def page(request: Request) -> Response:
    return cast(
        Response,
        request.app.state.templates.TemplateResponse(
            request=request, name="stream_operator.html", context={}
        ),
    )


@router.post("/stream-operator/api/pair")
def pair(request: Request, data: PairToken) -> Response:
    origin(request)
    identity = request.client.host if request.client else "unknown"
    if request.app.state.operator_pair_limiter.hit(identity) is not None:
        raise BroadcastError("operator_pair_rate_limited", 429)
    token = service(request).pair(data.token)
    response = Response(status_code=204)
    response.set_cookie(
        COOKIE, token, secure=True, httponly=True, samesite="strict", path="/", max_age=14400
    )
    return response


@router.get("/stream-operator/api/state")
def operator_state(
    request: Request, grant: Annotated[dict[str, Any], Depends(operator)]
) -> dict[str, Any]:
    return {
        **limited_snapshot(store(request), grant["session_id"]),
        "scope": "stream_operator",
        "csrf_token": store(request).fingerprint(["operator-csrf", request.cookies.get(COOKIE)]),
        "expires_at": grant["expires_at"],
    }


@router.post("/stream-operator/api/outputs/{output_id}/switch")
def operator_switch(
    request: Request,
    output_id: str,
    data: SwitchRequest,
    grant: Annotated[dict[str, Any], Depends(operator_mutation)],
    idempotency_key: Annotated[str, Header()],
) -> dict[str, str]:
    scoped_output(request, grant, output_id)
    identifier = controller(request).request(
        output_id, data.target_route_id, idempotency_key, handoff_ingress=data.handoff_ingress
    )
    with store(request).transaction() as db:
        store(request).event(
            db,
            grant["session_id"],
            "operator.switch_requested",
            output_id=output_id,
            switch_id=identifier,
            detail={"operator_id": grant["id"]},
        )
    return {"id": identifier}


@router.post("/stream-operator/api/switches/{switch_id}/cancel")
def operator_cancel(
    request: Request, switch_id: str, grant: Annotated[dict[str, Any], Depends(operator_mutation)]
) -> dict[str, bool]:
    with store(request).database.connect() as db:
        row = store(request).row(
            db, "SELECT output_id FROM broadcast_switches WHERE id=?", (switch_id,)
        )
    scoped_output(request, grant, row["output_id"])
    controller(request).cancel(switch_id)
    return {"accepted": True}


@router.post("/stream-operator/api/logout")
def logout(
    request: Request, grant: Annotated[dict[str, Any], Depends(operator_mutation)]
) -> Response:
    service(request).revoke(grant["id"])
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
    return response


@router.get("/moblin-hud/api/broadcasts", dependencies=[Depends(require_hud_session)])
def monitor(request: Request) -> dict[str, Any]:
    return {**limited_snapshot(store(request), None), "scope": "stream_monitor"}


@router.post(
    "/api/broadcasts/sessions/{session_id}/moblin-profiles", dependencies=[Depends(mutation)]
)
def profiles(request: Request, session_id: str) -> dict[str, str]:
    data = store(request)
    with data.transaction() as db:
        session = data.row(db, "SELECT source_id FROM broadcast_sessions WHERE id=?", (session_id,))
        nodes = db.execute(
            "SELECT DISTINCT n.id,n.display_name,m.srt_host,m.srt_port FROM restream_nodes n "
            "JOIN broadcast_media_nodes m ON m.node_id=n.id JOIN "
            "broadcast_routes r ON r.node_id=n.id "
            "JOIN broadcast_outputs o ON o.id=r.output_id WHERE o.session_id=? AND m.enabled=1 "
            "AND n.revoked_at IS NULL",
            (session_id,),
        ).fetchall()
        streams = []
        for node in nodes:
            secret = request.app.state.broadcast_media._source_secret(
                db, session["source_id"], node["id"]
            )
            host = f"[{node['srt_host']}]" if ":" in node["srt_host"] else node["srt_host"]
            query = urlencode(
                {
                    "streamid": f"publish:source/{session['source_id']}/direct:phone:{secret}",
                    "passphrase": secret,
                    "pbkeylen": 32,
                    "latency": 200000,
                },
                safe=":/",
            )
            streams.append(
                {
                    "name": "AdoJapan — " + node["display_name"],
                    "url": f"srt://{host}:{node['srt_port']}?{query}",
                    "selected": False,
                }
            )
        data.event(db, session_id, "moblin.profiles_requested")
    return {"moblin_url": "moblin://?" + quote(json.dumps({"streams": streams}), safe="")}
