"""Small UI adapters over existing broadcast intent, admission and secret storage.

No agent/media protocol, installer, lease timing or switch state machine changes.
"""

from __future__ import annotations

import json
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlencode

from pydantic import Field, SecretStr, field_validator

from app.broadcast.media_control import MediaControl
from app.broadcast.models import CAPABILITIES, BroadcastError, Input, OutputCreate, SessionCreate
from app.broadcast.read_model import snapshot
from app.broadcast.store import BroadcastStore
from app.broadcast.switching import SwitchController
from app.db import utc_now


class Prepare(Input):
    name: str = Field(default="Моя трансляция", min_length=1, max_length=120)
    ingress_node_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class Connection(Input):
    target_route_id: str | None = Field(default=None, min_length=1, max_length=128)


class YouTubeSettings(Input):
    # Reuse the canonical endpoint/key validators without duplicating policy.
    primary_url: str | None = Field(default=None, max_length=256)
    backup_url: str | None = Field(default=None, max_length=256)
    stream_key: SecretStr | None = Field(default=None, repr=False)

    @field_validator("primary_url", "backup_url")
    @classmethod
    def endpoint(cls, value: str | None) -> str | None:
        return OutputCreate.endpoint(value)

    @field_validator("stream_key")
    @classmethod
    def key(cls, value: SecretStr | None) -> SecretStr | None:
        return OutputCreate.key(value)


class BroadcastPresentation:
    def __init__(self, store: BroadcastStore, media: MediaControl, switches: SwitchController):
        self.store, self.media, self.switches = store, media, switches

    def public_node(self, db: Any, node_id: str, *, require_fresh: bool = True) -> Any:
        if require_fresh:
            node = self.media.ready_node(db, node_id)
        else:
            # Revealing an existing address is not media admission: a missed
            # heartbeat does not revoke its credential. Revocation still does.
            node = db.execute(
                "SELECT m.*,n.status,n.revoked_at FROM broadcast_media_nodes m "
                "JOIN restream_nodes n ON n.id=m.node_id WHERE m.node_id=?",
                (node_id,),
            ).fetchone()
            if not node or not node["enabled"] or node["revoked_at"] or node["status"] == "revoked":
                raise BroadcastError("media_node_not_enabled")
        if not CAPABILITIES.issubset(json.loads(node["capabilities_json"])):
            raise BroadcastError("media_capability_missing")
        if not ip_address(node["srt_host"]).is_global:
            raise BroadcastError("public_media_address_required")
        return node

    def prepare(self, data: Prepare, key: str) -> dict[str, str]:
        value = data.model_dump()
        with self.store.transaction() as db:
            prior = self.store.replay(db, "ui-prepare", key, value)
            if prior:
                output = self.store.row(
                    db, "SELECT session_id FROM broadcast_outputs WHERE id=?", (prior,)
                )
                return {"session_id": output["session_id"], "output_id": prior}
            self.public_node(db, data.ingress_node_id)
            if data.session_id:
                session = self.store.row(
                    db,
                    "SELECT s.id,src.ingress_node_id FROM broadcast_sessions s "
                    "JOIN broadcast_sources src ON src.id=s.source_id WHERE s.id=?",
                    (data.session_id,),
                )
                if session["ingress_node_id"] != data.ingress_node_id:
                    raise BroadcastError("ingress_change_requires_handoff")
                sid = data.session_id
            else:
                sid = self.store.create_session(
                    SessionCreate(name=data.name, ingress_node_id=data.ingress_node_id), key, _db=db
                )
            oid = self.store.create_output(
                sid,
                OutputCreate(name=data.name, node_id=data.ingress_node_id),
                key,
                draft=True,
                _db=db,
            )
            # Existing admission checks resource/profile compatibility even for a draft.
            self.media.admit(db, oid)
            source = self.store.row(
                db, "SELECT source_id FROM broadcast_sessions WHERE id=?", (sid,)
            )
            self.media._source_secret(db, source["source_id"], data.ingress_node_id)
            self.store.remember(db, "ui-prepare", key, value, oid)
            return {"session_id": sid, "output_id": oid}

    def connection(self, session_id: str, target_route_id: str | None) -> dict[str, Any]:
        # Explicit reveal only. Never issue desired intent or rotate a source secret here.
        with self.store.database.connect() as db:
            session = self.store.row(
                db,
                "SELECT s.source_id,src.ingress_node_id,src.profile_json FROM broadcast_sessions s "
                "JOIN broadcast_sources src ON src.id=s.source_id WHERE s.id=?",
                (session_id,),
            )
            node_id = session["ingress_node_id"]
            if target_route_id:
                route = self.store.row(
                    db,
                    "SELECT r.node_id FROM broadcast_routes r "
                    "JOIN broadcast_outputs o ON o.id=r.output_id WHERE r.id=? AND o.session_id=?",
                    (target_route_id, session_id),
                )
                node_id = route["node_id"]
            node = self.public_node(db, node_id, require_fresh=False)
            row = db.execute(
                "SELECT encrypted FROM broadcast_source_secrets WHERE source_id=?",
                (session["source_id"],),
            ).fetchone()
            if not row:
                raise BroadcastError("connection_not_prepared")
            secret = self.store.fingerprint(
                [self.store.unseal(row["encrypted"])["passphrase"], node_id]
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
            return {
                "protocol": "srt",
                "node_id": node_id,
                "url": f"srt://{host}:{node['srt_port']}?{query}",
                "listener_confirmed": False,
                "profile": json.loads(session["profile_json"]),
            }

    def save_youtube(self, output_id: str, data: YouTubeSettings, key: str) -> None:
        value = data.model_dump(mode="json")
        value["stream_key"] = data.stream_key.get_secret_value() if data.stream_key else None
        with self.store.transaction() as db:
            scope = f"ui-youtube:{output_id}"
            if self.store.replay(db, scope, key, value):
                return
            output = self.store.row(db, "SELECT * FROM broadcast_outputs WHERE id=?", (output_id,))
            if output["mode"] != "manual":
                raise BroadcastError("use_youtube_api_settings")
            binding = self.store.row(
                db, "SELECT * FROM youtube_bindings WHERE output_id=?", (output_id,)
            )
            if db.execute(
                "SELECT 1 FROM broadcast_switches WHERE output_id=? AND active=1", (output_id,)
            ).fetchone():
                raise BroadcastError("switch_in_progress")
            old = (
                self.store.unseal(binding["credentials_encrypted"])
                if binding["credentials_encrypted"]
                else {}
            )
            credentials = {
                "primary": data.primary_url or old.get("primary"),
                "backup": data.backup_url,
                "stream_key": value["stream_key"] or old.get("stream_key"),
            }
            if not credentials["primary"] or not credentials["stream_key"]:
                raise BroadcastError("manual_credentials_required", 422)
            if credentials["primary"] == credentials["backup"]:
                raise BroadcastError("distinct_backup_endpoint_required", 422)
            fingerprint = self.store.fingerprint(credentials["stream_key"])
            if db.execute(
                "SELECT 1 FROM youtube_bindings WHERE credential_fingerprint=? AND output_id!=?",
                (fingerprint, output_id),
            ).fetchone():
                raise BroadcastError("independent_output_requires_unique_stream")
            if credentials == old:
                self.store.remember(db, scope, key, value, output_id)
                return
            # Adding the missing backup changes no current endpoint or key/lease.
            add_backup = bool(
                old
                and not old.get("backup")
                and credentials["backup"]
                and old["primary"] == credentials["primary"]
                and old["stream_key"] == credentials["stream_key"]
            )
            routes = db.execute(
                "SELECT id,youtube_slot FROM broadcast_routes WHERE output_id=?", (output_id,)
            ).fetchall()
            if add_backup and any(r["youtube_slot"] == "BACKUP" for r in routes):
                raise BroadcastError("youtube_slot_not_free")
            if not add_backup:
                if output["desired_enabled"]:
                    raise BroadcastError("stop_before_changing_youtube")
                for route in routes:
                    ever_leased = db.execute(
                        "SELECT 1 FROM broadcast_egress_leases WHERE route_id=?", (route["id"],)
                    ).fetchone()
                    if ever_leased and not self.switches.stopped(
                        db, route["id"], output["updated_at"]
                    ):
                        raise BroadcastError("waiting_for_publisher_stop")
            db.execute(
                "UPDATE youtube_bindings SET credentials_encrypted=?,credential_fingerprint=?,"
                "has_backup=?,updated_at=? WHERE output_id=?",
                (
                    self.store.seal(credentials),
                    fingerprint,
                    bool(credentials["backup"]),
                    utc_now(),
                    output_id,
                ),
            )
            if not output["desired_enabled"]:
                db.execute(
                    "UPDATE broadcast_outputs SET state='READY',updated_at=? WHERE id=?",
                    (utc_now(), output_id),
                )
            self.store.event(
                db, output["session_id"], "output.connection_saved", output_id=output_id
            )
            self.store.remember(db, scope, key, value, output_id)

    def state(self) -> dict[str, Any]:
        result = snapshot(self.store)
        with self.store.database.connect() as db:
            for node in result["nodes"]:
                try:
                    self.public_node(db, node["id"])
                    node["setup_error"] = None
                except BroadcastError as exc:
                    node["setup_error"] = exc.code
            for session in result["sessions"]:
                for output in session["outputs"]:
                    binding = self.store.row(
                        db,
                        "SELECT credentials_encrypted FROM youtube_bindings WHERE output_id=?",
                        (output["id"],),
                    )
                    credentials = (
                        self.store.unseal(binding["credentials_encrypted"])
                        if binding["credentials_encrypted"]
                        else {}
                    )
                    output["connection"] = {
                        "primary_url": credentials.get("primary"),
                        "backup_url": credentials.get("backup"),
                    }
                    # Desired stop is not proof that the remote publisher has stopped.
                    output["stop_confirmed"] = not output["desired_enabled"] and all(
                        not db.execute(
                            "SELECT 1 FROM broadcast_egress_leases WHERE route_id=?",
                            (route["id"],),
                        ).fetchone()
                        or self.switches.stopped(db, route["id"], output["updated_at"])
                        for route in output["routes"]
                    )
                    for route in output["routes"]:
                        try:
                            self.public_node(db, route["node_id"])
                            self.media.admit(db, output["id"], route["id"])
                            route["admission_error"] = None
                        except BroadcastError as exc:
                            route["admission_error"] = exc.code
                        # Ignore telemetry from a superseded desired plan in this UI.
                        observation = db.execute(
                            "SELECT o.plan_generation,m.generation "
                            "FROM broadcast_media_observations o JOIN broadcast_media_nodes m "
                            "ON m.node_id=o.node_id WHERE o.route_id=?",
                            (route["id"],),
                        ).fetchone()
                        if (
                            observation
                            and observation["plan_generation"] != observation["generation"]
                        ):
                            route["media_state"] = "UNKNOWN"
                            for link in ("phone_link", "interrelay_link", "egress_link"):
                                route[link] = {
                                    k: "UNKNOWN" if k in ("state", "viewer_playback") else None
                                    for k in route[link]
                                }
                    current = next(r for r in output["routes"] if r["role"] == "current")
                    slot = "BACKUP" if current["youtube_slot"] == "PRIMARY" else "PRIMARY"
                    for route in output["routes"]:
                        error = route["admission_error"]
                        if output["switch"] and output["switch"]["active"]:
                            error = "switch_in_progress"
                        elif not output["desired_enabled"]:
                            error = "output_not_running"
                        elif not output["youtube"]["has_backup"]:
                            error = "youtube_dual_ingest_required"
                        elif not current["server_ready"]:
                            error = "current_server_unavailable"
                        elif any(r["youtube_slot"] == slot for r in output["routes"]):
                            error = "youtube_slot_not_free"
                        route["switch_error"] = error
                        route["handoff_error"] = error or (
                            "source_handoff_in_progress"
                            if any(
                                o["switch"] and o["switch"]["active"] for o in session["outputs"]
                            )
                            else None
                        )
                    # Managed v2 has no source-scoped preview in this candidate.
                    # Reusing the legacy node preview could show an unrelated stream.
                    output["preview"] = {
                        "available": False,
                        "reason": "managed_preview_unavailable",
                    }
        return result
