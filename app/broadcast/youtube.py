"""YouTube adapter and durable provisioning. No automatic duplicate inserts."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app.broadcast.models import BroadcastError, youtube_endpoint
from app.broadcast.store import BroadcastStore
from app.db import utc_now

SAFE_REASONS = frozenset(
    {
        "concurrentBroadcastsExceedLimit",
        "sharedIngestionBroadcastsExceedLimit",
        "quotaExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "insufficientPermissions",
        "forbidden",
        "liveStreamingNotEnabled",
        "livePermissionBlocked",
        "invalidTransition",
        "redundantTransition",
        "errorStreamInactive",
        "invalid_grant",
        "notFound",
        "unauthorized",
    }
)


class YouTubeError(BroadcastError):
    pass


class YouTube(Protocol):
    def insert_stream(self, output: dict[str, Any], marker: str) -> dict[str, Any]: ...
    def insert_broadcast(self, output: dict[str, Any], marker: str) -> dict[str, Any]: ...
    def find(self, resource: str, marker: str) -> dict[str, Any] | None: ...
    def bind(self, broadcast_id: str, stream_id: str) -> None: ...
    def status(self, broadcast_id: str, stream_id: str) -> dict[str, str]: ...
    def transition(self, broadcast_id: str, state: str) -> None: ...


class YouTubeHTTP:
    def __init__(
        self, access_token: Callable[[], str], *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.access_token = access_token
        self.transport = transport

    def request(
        self, method: str, resource: str, params: dict[str, str], body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # The host and resource are code-owned. No redirect or caller-supplied URL.
        with httpx.Client(timeout=15, follow_redirects=False, transport=self.transport) as client:
            try:
                response = client.request(
                    method,
                    f"https://www.googleapis.com/youtube/v3/{resource}",
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {self.access_token()}"},
                )
            except httpx.HTTPError:
                raise YouTubeError("youtube_response_uncertain", 503) from None
        try:
            result: dict[str, Any] = response.json()
            if response.is_success:
                return result
            error = result.get("error", {})
            reasons: list[dict[str, Any]] = error.get("errors", [])
            reason = (
                reasons[0].get("reason", "youtube_api_error") if reasons else "youtube_api_error"
            )
        except (ValueError, AttributeError, TypeError):
            reason = "youtube_api_error"
        raise YouTubeError(reason if reason in SAFE_REASONS else "youtube_api_error", 502)

    def insert_stream(self, output: dict[str, Any], marker: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "liveStreams",
            {"part": "snippet,cdn,contentDetails"},
            {
                "snippet": {"title": output["name"], "description": marker},
                "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
                # Reusable permits mine=true reconciliation, but our DB binds it to one output only.
                "contentDetails": {"isReusable": True},
            },
        )

    def insert_broadcast(self, output: dict[str, Any], marker: str) -> dict[str, Any]:
        start = output["scheduled_start"] or (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        return self.request(
            "POST",
            "liveBroadcasts",
            {"part": "snippet,status,contentDetails"},
            {
                "snippet": {
                    "title": output["name"],
                    "description": marker,
                    "scheduledStartTime": start,
                },
                "status": {"privacyStatus": output["visibility"], "selfDeclaredMadeForKids": False},
                "contentDetails": {
                    "enableAutoStart": False,
                    "enableAutoStop": False,
                    "monitorStream": {"enableMonitorStream": False},
                },
            },
        )

    def find(self, resource: str, marker: str) -> dict[str, Any] | None:
        if resource not in {"liveStreams", "liveBroadcasts"}:
            raise YouTubeError("invalid_resource", 422)
        params = {
            "part": "id,snippet,cdn" if resource == "liveStreams" else "id,snippet",
            "mine": "true",
            "maxResults": "50",
        }
        found: list[dict[str, Any]] = []
        for _ in range(20):
            result = self.request("GET", resource, params)
            found.extend(
                item
                for item in result.get("items", [])
                if item.get("snippet", {}).get("description") == marker
            )
            if not result.get("nextPageToken"):
                if len(found) > 1:
                    raise YouTubeError("youtube_duplicate_marker")
                return found[0] if found else None
            params["pageToken"] = result["nextPageToken"]
        raise YouTubeError("youtube_reconciliation_limit")

    def bind(self, broadcast_id: str, stream_id: str) -> None:
        self.request(
            "POST",
            "liveBroadcasts/bind",
            {"part": "id,contentDetails", "id": broadcast_id, "streamId": stream_id},
        )

    def status(self, broadcast_id: str, stream_id: str) -> dict[str, str]:
        broadcasts = self.request("GET", "liveBroadcasts", {"part": "status", "id": broadcast_id})
        streams = self.request("GET", "liveStreams", {"part": "status", "id": stream_id})
        if not broadcasts.get("items") or not streams.get("items"):
            raise YouTubeError("notFound", 404)
        return {
            "lifecycle_status": str(broadcasts["items"][0]["status"]["lifeCycleStatus"]),
            "stream_status": str(streams["items"][0]["status"]["streamStatus"]),
            "health_status": str(streams["items"][0]["status"]["healthStatus"]["status"]),
        }

    def transition(self, broadcast_id: str, state: str) -> None:
        if state not in {"testing", "live", "complete"}:
            raise YouTubeError("invalidTransition", 422)
        self.request(
            "POST",
            "liveBroadcasts/transition",
            {"part": "status", "id": broadcast_id, "broadcastStatus": state},
        )


class YouTubeProvisioner:
    def __init__(self, store: BroadcastStore) -> None:
        self.store = store

    def provision(self, output_id: str, provider: YouTube) -> None:
        owner = secrets.token_hex(16)
        with self.store.transaction() as db:
            output = dict(
                self.store.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
            )
            operation = dict(
                self.store.row(
                    db, "SELECT * FROM youtube_operations WHERE output_id=?", (output_id,)
                )
            )
            if operation["phase"] == "READY":
                return
            if operation["lease_until"] and operation["lease_until"] > utc_now():
                raise BroadcastError("youtube_operation_busy")
            until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            db.execute(
                "UPDATE youtube_operations SET lease_owner=?,lease_until=?,"
                "generation=generation+1 WHERE output_id=?",
                (owner, until, output_id),
            )
        try:
            phase = operation["phase"]
            marker = operation["marker"]
            stream: dict[str, Any] | None
            broadcast: dict[str, Any] | None
            if phase in {"NEW", "STREAM_PENDING"}:
                if phase == "NEW":
                    self._phase(output_id, owner, "STREAM_PENDING")
                    stream = provider.insert_stream(output, marker)
                else:
                    stream = provider.find("liveStreams", marker)
                    if stream is None:
                        raise YouTubeError("youtube_reconciliation_required")
                info = stream["cdn"]["ingestionInfo"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{6,256}", info["streamName"]):
                    raise YouTubeError("youtube_invalid_credentials", 502)
                credentials = {
                    "primary": youtube_endpoint(info["rtmpsIngestionAddress"]),
                    "backup": youtube_endpoint(info["rtmpsBackupIngestionAddress"])
                    if info.get("rtmpsBackupIngestionAddress")
                    else None,
                    "stream_key": info["streamName"],
                }
                with self.store.transaction() as db:
                    self._owned(db, output_id, owner)
                    db.execute(
                        "UPDATE youtube_bindings SET stream_id=?,credentials_encrypted=?,"
                        "credential_fingerprint=?,has_backup=?,updated_at=? WHERE output_id=?",
                        (
                            stream["id"],
                            self.store.seal(credentials),
                            self.store.fingerprint(info["streamName"]),
                            bool(credentials["backup"]),
                            utc_now(),
                            output_id,
                        ),
                    )
                    db.execute(
                        "UPDATE youtube_operations SET stream_id=?,phase='STREAM_CREATED' "
                        "WHERE output_id=?",
                        (stream["id"], output_id),
                    )
                operation["stream_id"], phase = stream["id"], "STREAM_CREATED"
            if phase in {"STREAM_CREATED", "BROADCAST_PENDING"}:
                if phase == "STREAM_CREATED":
                    self._phase(output_id, owner, "BROADCAST_PENDING")
                    broadcast = provider.insert_broadcast(output, marker)
                else:
                    broadcast = provider.find("liveBroadcasts", marker)
                    if broadcast is None:
                        raise YouTubeError("youtube_reconciliation_required")
                with self.store.transaction() as db:
                    self._owned(db, output_id, owner)
                    db.execute(
                        "UPDATE youtube_bindings SET broadcast_id=? WHERE output_id=?",
                        (broadcast["id"], output_id),
                    )
                    db.execute(
                        "UPDATE youtube_operations SET broadcast_id=?,phase='BIND_PENDING' "
                        "WHERE output_id=?",
                        (broadcast["id"], output_id),
                    )
                operation["broadcast_id"], phase = broadcast["id"], "BIND_PENDING"
            if phase == "BIND_PENDING":
                provider.bind(operation["broadcast_id"], operation["stream_id"])
                self._phase(output_id, owner, "READY")
                with self.store.transaction() as db:
                    self._owned(db, output_id, owner)
                    db.execute(
                        "UPDATE broadcast_outputs SET state='READY',safe_error_code=NULL "
                        "WHERE id=?",
                        (output_id,),
                    )
                    self.store.event(
                        db, output["session_id"], "youtube.provisioned", output_id=output_id
                    )
        except (KeyError, TypeError, ValueError):
            self._error(output_id, owner, "youtube_invalid_response")
            raise YouTubeError("youtube_invalid_response", 502) from None
        except BroadcastError as exc:
            self._error(output_id, owner, exc.code)
            raise
        finally:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE youtube_operations SET lease_owner=NULL,lease_until=NULL "
                    "WHERE output_id=? AND lease_owner=?",
                    (output_id, owner),
                )

    def _owned(self, db: Any, output_id: str, owner: str) -> None:
        row = self.store.row(
            db,
            "SELECT lease_owner,lease_until FROM youtube_operations WHERE output_id=?",
            (output_id,),
        )
        if row["lease_owner"] != owner or row["lease_until"] < utc_now():
            raise BroadcastError("youtube_lease_lost")

    def _phase(self, output_id: str, owner: str, phase: str) -> None:
        with self.store.transaction() as db:
            self._owned(db, output_id, owner)
            db.execute(
                "UPDATE youtube_operations SET phase=?,attempts=attempts+1,updated_at=? "
                "WHERE output_id=?",
                (phase, utc_now(), output_id),
            )

    def _error(self, output_id: str, owner: str, code: str) -> None:
        with self.store.transaction() as db:
            self._owned(db, output_id, owner)
            db.execute(
                "UPDATE youtube_operations SET safe_error_code=? WHERE output_id=?",
                (code, output_id),
            )
            db.execute(
                "UPDATE broadcast_outputs SET safe_error_code=? WHERE id=?", (code, output_id)
            )
