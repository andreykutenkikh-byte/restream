"""Admin-only diagnostics; HUD/operator credentials cannot read stream history."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.api import CSRF_COOKIE, SESSION_COOKIE, require_session
from app.broadcast.api import store
from app.broadcast.diagnostics import RETENTION_DAYS, bounds, records, report

router = APIRouter(dependencies=[Depends(require_session)])
Hours = Annotated[int, Query(ge=1, le=168)]


@router.get("/api/broadcasts/outputs/{output_id}/diagnostics")
def diagnostics(
    request: Request, output_id: str, hours: Hours = 6, until: datetime | None = None
) -> dict[str, Any]:
    return report(store(request), output_id, hours, until)


@router.get("/broadcasts/outputs/{output_id}/diagnostics")
def page(
    request: Request, output_id: str, hours: Hours = 6, until: datetime | None = None
) -> Response:
    data = report(store(request), output_id, hours, until)
    csrf = request.app.state.sessions.ensure_csrf(
        request.cookies[SESSION_COOKIE], request.cookies.get(CSRF_COOKIE)
    )
    response: Response = request.app.state.templates.TemplateResponse(
        request=request,
        name="stream_diagnostics.html",
        context={"report": data, "hours": hours, "csrf_token": csrf},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, secure=request.app.state.settings.cookie_secure, samesite="lax", path="/"
    )
    return response


@router.get("/api/broadcasts/outputs/{output_id}/diagnostics/export")
def export(
    request: Request, output_id: str, hours: Hours = 6, until: datetime | None = None
) -> StreamingResponse:
    source = store(request)
    with source.database.connect() as db:
        source.row(db, "SELECT id FROM broadcast_outputs WHERE id=?", (output_id,))
    since, end = bounds(hours, until)

    def stream() -> Iterator[str]:
        yield (
            json.dumps(
                {
                    "kind": "metadata",
                    "version": 1,
                    "output_id": output_id,
                    "since": since,
                    "until": end,
                    "retention_days": RETENTION_DAYS,
                    "note": "Records are grouped by kind; sort by at. "
                    "Gaps are missing observations, not proof of healthy delivery.",
                }
            )
            + "\n"
        )
        for item in records(source, output_id, since, end):
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="stream-diagnostics.jsonl"',
        },
    )
