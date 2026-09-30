"""No media enters this service: only bounded intent, encrypted leases and measurements."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from pydantic import Field

from app.broadcast.envelope import decode, seal
from app.broadcast.models import CAPABILITIES, BroadcastError, Input, MediaProfile, ResourceLimits
from app.broadcast.store import BroadcastStore
from app.db import utc_now

LEASE_SECONDS = 120
FRESH_SECONDS = 20


class MediaNodeEnable(Input):
    public_key: str = Field(min_length=44, max_length=44)
    srt_host: str = Field(min_length=1, max_length=45)
    srt_port: int = Field(ge=1024, le=65535)
    limits: ResourceLimits
    profile: MediaProfile = Field(default_factory=MediaProfile)


class Observation(Input):
    route_id: str = Field(min_length=1, max_length=64)
    source_kind: Literal["direct", "forwarded", "unknown"]
    source_identity: str | None = Field(default=None, max_length=128)
    video_pts: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    audio_pts: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    video_frames: int = Field(default=0, ge=0, le=10**12)
    audio_packets: int = Field(default=0, ge=0, le=10**12)
    bitrate_bps: int | None = Field(default=None, ge=0, le=1_000_000_000)
    publisher_frames: int = Field(default=0, ge=0, le=10**12)
    publisher_time_us: int = Field(default=0, ge=0, le=10**18)
    publisher_connected: bool = False
    safe_error_code: Literal["publisher_failed", "source_lost", "retry_exhausted"] | None = None


class MediaHeartbeat(Input):
    protocol_version: Literal[2] = 2
    boot_id: str = Field(min_length=16, max_length=64)
    public_key: str = Field(min_length=44, max_length=44)
    capabilities: list[str] = Field(max_length=8)
    sequence: int = Field(ge=0, le=10**15)
    plan_generation: int = Field(ge=0, le=10**15)
    observations: list[Observation] = Field(default_factory=list, max_length=32)


class MediaControl:
    def __init__(self, store: BroadcastStore, *, test_loopback: bool = False) -> None:
        self.store = store
        self.test_loopback = test_loopback

    def admit(self, db: Any, output_id: str, target_route: str | None = None) -> None:
        route = self.store.row(
            db,
            "SELECT r.*,src.profile_json,src.ingress_node_id "
            "FROM broadcast_routes r JOIN broadcast_outputs o ON o.id=r.output_id "
            "JOIN broadcast_sessions s ON s.id=o.session_id "
            "JOIN broadcast_sources src ON src.id=s.source_id WHERE r.output_id=? "
            "AND ((? IS NULL AND r.role='current') OR r.id=?)",
            (output_id, target_route, target_route),
        )
        node = self.ready_node(db, route["node_id"])
        profile = MediaProfile.model_validate_json(route["profile_json"])
        expected = MediaProfile.model_validate_json(node["profile_json"])
        if profile.model_dump(exclude={"expected_bitrate_bps"}) != expected.model_dump(
            exclude={"expected_bitrate_bps"}
        ):
            raise BroadcastError("media_profile_incompatible")
        limits = ResourceLimits.model_validate_json(node["limits_json"])
        active = db.execute(
            "SELECT src.profile_json FROM broadcast_routes r "
            "JOIN broadcast_outputs o ON o.id=r.output_id "
            "JOIN broadcast_sessions s ON s.id=o.session_id "
            "JOIN broadcast_sources src ON src.id=s.source_id "
            "WHERE r.node_id=? AND r.desired_enabled=1 "
            "AND r.id!=?",
            (route["node_id"], route["id"]),
        ).fetchall()
        if len(active) + 1 > limits.max_publishers_per_node:
            raise BroadcastError("publisher_limit")
        bitrate = profile.expected_bitrate_bps + sum(
            json.loads(r["profile_json"])["expected_bitrate_bps"] for r in active
        )
        if bitrate > limits.max_expected_egress_bps:
            raise BroadcastError("egress_limit")
        if route["ingress_node_id"] != route["node_id"]:
            source = self.ready_node(db, route["ingress_node_id"], capability="inter_relay_srt_v1")
            source_limits = ResourceLimits.model_validate_json(source["limits_json"])
            count = db.execute(
                "SELECT COUNT(*) FROM broadcast_forwarding WHERE source_node_id=? "
                "AND enabled=1 AND route_id!=?",
                (route["ingress_node_id"], route["id"]),
            ).fetchone()[0]
            if count + 1 > source_limits.max_forwarded_routes:
                raise BroadcastError("forward_limit")

    def enable(self, node_id: str, data: MediaNodeEnable) -> None:
        try:
            X25519PublicKey.from_public_bytes(decode(data.public_key))
            address = ip_address(data.srt_host)
        except ValueError:
            raise BroadcastError("invalid_media_node", 422) from None
        if not address.is_global and not (self.test_loopback and address.is_loopback):
            raise BroadcastError("public_media_address_required", 422)
        with self.store.transaction() as db:
            self.store.node(db, node_id)
            if db.execute(
                "SELECT 1 FROM broadcast_media_nodes WHERE node_id=?", (node_id,)
            ).fetchone():
                raise BroadcastError("media_node_already_enabled")
            db.execute(
                "INSERT INTO broadcast_media_nodes(node_id,public_key,srt_host,srt_port,"
                "capabilities_json,limits_json,profile_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    node_id,
                    data.public_key,
                    data.srt_host,
                    data.srt_port,
                    "[]",
                    data.limits.model_dump_json(),
                    data.profile.model_dump_json(),
                    utc_now(),
                ),
            )
        self.store.database.add_audit_event("broadcast.media_enabled", f"node_id={node_id}")

    def ready_node(self, db: Any, node_id: str, *, capability: str = "multi_output_v1") -> Any:
        node = db.execute(
            "SELECT m.*,n.status,n.revoked_at FROM broadcast_media_nodes m "
            "JOIN restream_nodes n ON n.id=m.node_id WHERE m.node_id=?",
            (node_id,),
        ).fetchone()
        if not node or not node["enabled"] or node["revoked_at"] or node["status"] == "revoked":
            raise BroadcastError("media_node_not_enabled")
        if capability not in json.loads(node["capabilities_json"]):
            raise BroadcastError("media_capability_missing")
        if (
            not node["last_seen_at"]
            or (datetime.now(UTC) - datetime.fromisoformat(node["last_seen_at"])).total_seconds()
            > FRESH_SECONDS
        ):
            raise BroadcastError("media_heartbeat_stale")
        return node

    def _source_secret(self, db: Any, source_id: str, node_id: str) -> str:
        row = db.execute(
            "SELECT encrypted FROM broadcast_source_secrets WHERE source_id=?", (source_id,)
        ).fetchone()
        if row:
            return self.store.fingerprint(
                [self.store.unseal(row["encrypted"])["passphrase"], node_id]
            )
        value = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO broadcast_source_secrets VALUES (?,?)",
            (source_id, self.store.seal({"passphrase": value})),
        )
        return self.store.fingerprint([value, node_id])

    def ensure_forward(self, db: Any, route: Any, source_node: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM broadcast_forwarding WHERE route_id=?", (route["id"],)
        ).fetchone()
        if row and row["source_node_id"] == source_node and row["enabled"]:
            return dict(row)
        source = self.ready_node(db, source_node, capability="inter_relay_srt_v1")
        limits = ResourceLimits.model_validate_json(source["limits_json"])
        count = db.execute(
            "SELECT COUNT(*) FROM broadcast_forwarding WHERE source_node_id=? AND enabled=1",
            (source_node,),
        ).fetchone()[0]
        if count >= limits.max_forwarded_routes:
            raise BroadcastError("forward_limit")
        generation = row["generation"] + 1 if row else 1
        credentials = self.store.seal(
            {"passphrase": secrets.token_urlsafe(32), "token": secrets.token_urlsafe(32)}
        )
        db.execute(
            "INSERT INTO broadcast_forwarding VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(route_id) DO UPDATE SET source_node_id=excluded.source_node_id,"
            "generation=excluded.generation,encrypted=excluded.encrypted,enabled=1",
            (route["id"], source_node, route["node_id"], generation, credentials, utc_now()),
        )
        return dict(
            self.store.row(
                db, "SELECT * FROM broadcast_forwarding WHERE route_id=?", (route["id"],)
            )
        )

    def heartbeat(self, node_id: str, data: MediaHeartbeat) -> dict[str, Any]:
        with self.store.transaction() as db:
            node = self.store.row(
                db, "SELECT * FROM broadcast_media_nodes WHERE node_id=?", (node_id,)
            )
            if not node["enabled"] or data.public_key != node["public_key"]:
                raise BroadcastError("media_identity_mismatch", 403)
            if not CAPABILITIES.issubset(data.capabilities):
                raise BroadcastError("media_capability_missing")
            if node["boot_id"] == data.boot_id and data.sequence <= node["last_sequence"]:
                raise BroadcastError("stale_heartbeat")
            if node["boot_id"] != data.boot_id:
                db.execute("DELETE FROM broadcast_media_observations WHERE node_id=?", (node_id,))
            if data.observations and data.plan_generation != node["generation"]:
                raise BroadcastError("stale_plan_generation")
            now = utc_now()
            db.execute(
                "UPDATE broadcast_media_nodes SET last_seen_at=?,last_sequence=?,boot_id=?,"
                "capabilities_json=? WHERE node_id=?",
                (now, data.sequence, data.boot_id, json.dumps(sorted(CAPABILITIES)), node_id),
            )
            for obs in data.observations:
                route = self.store.row(
                    db,
                    "SELECT * FROM broadcast_routes WHERE id=? AND node_id=?",
                    (obs.route_id, node_id),
                )
                old = db.execute(
                    "SELECT * FROM broadcast_media_observations WHERE route_id=?", (obs.route_id,)
                ).fetchone()
                moving = bool(
                    old
                    and obs.source_identity
                    and obs.source_identity == old["source_identity"]
                    and obs.source_kind == old["source_kind"]
                    and obs.video_pts is not None
                    and obs.audio_pts is not None
                    and old["video_pts"] is not None
                    and old["audio_pts"] is not None
                    and obs.video_pts > old["video_pts"]
                    and obs.audio_pts > old["audio_pts"]
                    and obs.video_frames > 0
                    and obs.audio_packets > 0
                    and obs.bitrate_bps
                    and obs.bitrate_bps > 0
                )
                samples = min(100, old["valid_samples"] + 1) if moving else 0
                direct = samples if obs.source_kind == "direct" else 0
                db.execute(
                    "INSERT OR REPLACE INTO broadcast_media_observations VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        obs.route_id,
                        node_id,
                        data.plan_generation,
                        data.sequence,
                        obs.source_kind,
                        obs.source_identity,
                        obs.video_pts,
                        obs.audio_pts,
                        obs.video_frames,
                        obs.audio_packets,
                        obs.bitrate_bps,
                        obs.publisher_frames,
                        obs.publisher_time_us,
                        obs.publisher_connected,
                        samples,
                        direct,
                        obs.safe_error_code,
                        now,
                    ),
                )
                db.execute(
                    "UPDATE broadcast_routes SET source_kind=? WHERE id=?",
                    (obs.source_kind, obs.route_id),
                )
                if route["role"] == "current":
                    state = (
                        "PUBLISHING"
                        if obs.publisher_connected and samples >= 2
                        else (
                            "SOURCE_LOST"
                            if obs.safe_error_code == "source_lost"
                            else "WAITING_FOR_MEDIA"
                        )
                    )
                    if not route["desired_enabled"]:
                        state = "STOPPED" if not obs.publisher_connected else "STOP_REQUESTED"
                    db.execute(
                        "UPDATE broadcast_outputs SET state=? WHERE id=?",
                        (state, route["output_id"]),
                    )
        return self.desired(node_id)

    def desired(self, node_id: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            node = self.ready_node(db, node_id)
            routes = db.execute(
                "SELECT r.*,s.source_id,src.ingress_node_id,src.profile_json,"
                "o.session_id,o.desired_enabled AS output_enabled,b.credentials_encrypted "
                "FROM broadcast_routes r JOIN broadcast_outputs o ON o.id=r.output_id "
                "JOIN broadcast_sessions s ON s.id=o.session_id "
                "JOIN broadcast_sources src ON src.id=s.source_id "
                "JOIN youtube_bindings b ON b.output_id=o.id WHERE r.node_id=?",
                (node_id,),
            ).fetchall()
            limits = ResourceLimits.model_validate_json(node["limits_json"])
            node_profile = MediaProfile.model_validate_json(node["profile_json"])
            publishing = [r for r in routes if r["desired_enabled"]]
            for route in publishing:
                profile = MediaProfile.model_validate_json(route["profile_json"])
                if profile.model_dump(exclude={"expected_bitrate_bps"}) != node_profile.model_dump(
                    exclude={"expected_bitrate_bps"}
                ):
                    raise BroadcastError("media_profile_incompatible")
            db.execute(
                "UPDATE broadcast_forwarding SET enabled=0 WHERE route_id IN "
                "(SELECT id FROM broadcast_routes WHERE desired_enabled=0)"
            )
            if len(publishing) > limits.max_publishers_per_node:
                raise BroadcastError("publisher_limit")
            bitrate = sum(json.loads(r["profile_json"])["expected_bitrate_bps"] for r in publishing)
            forwards = db.execute(
                "SELECT f.*,s.source_id,src.profile_json,r.output_id "
                "FROM broadcast_forwarding f JOIN broadcast_routes r ON r.id=f.route_id "
                "JOIN broadcast_outputs o ON o.id=r.output_id "
                "JOIN broadcast_sessions s ON s.id=o.session_id "
                "JOIN broadcast_sources src ON src.id=s.source_id "
                "WHERE f.source_node_id=? AND f.enabled=1",
                (node_id,),
            ).fetchall()
            bitrate += sum(json.loads(f["profile_json"])["expected_bitrate_bps"] for f in forwards)
            if bitrate > limits.max_expected_egress_bps:
                raise BroadcastError("egress_limit")
            plan: dict[str, Any] = {
                "protocol_version": 2,
                "node_id": node_id,
                "routes": [],
                "exports": [],
                "sources": {},
                "limits": limits.model_dump(),
            }
            for route in routes:
                source_id = route["source_id"]
                plan["sources"][source_id] = self._source_secret(db, source_id, node_id)
                item = {
                    "id": route["id"],
                    "output_id": route["output_id"],
                    "session_id": route["session_id"],
                    "source_id": source_id,
                    "profile": json.loads(route["profile_json"]),
                    "enabled": bool(route["desired_enabled"]),
                    "generation": route["generation"],
                    "forward": None,
                    "destination": None,
                }
                if route["desired_enabled"]:
                    if route["ingress_node_id"] != node_id:
                        forward = self.ensure_forward(db, route, route["ingress_node_id"])
                        source = self.ready_node(db, forward["source_node_id"])
                        item["forward"] = {
                            "source_node_id": forward["source_node_id"],
                            "host": source["srt_host"],
                            "port": source["srt_port"],
                            "path": f"relay/{source_id}/{route['id']}/{forward['generation']}",
                            **self.store.unseal(forward["encrypted"]),
                        }
                    if not route["credentials_encrypted"]:
                        raise BroadcastError("output_not_provisioned")
                    credentials = self.store.unseal(route["credentials_encrypted"])
                    endpoint = (
                        credentials["backup"]
                        if route["youtube_slot"] == "BACKUP"
                        else credentials["primary"]
                    )
                    if not endpoint:
                        raise BroadcastError("youtube_backup_unavailable")
                    item["destination"] = {
                        "endpoint": endpoint,
                        "stream_key": credentials["stream_key"],
                    }
                plan["routes"].append(item)
            for forward in forwards:
                plan["sources"][forward["source_id"]] = self._source_secret(
                    db, forward["source_id"], node_id
                )
                plan["exports"].append(
                    {
                        "route_id": forward["route_id"],
                        "source_id": forward["source_id"],
                        "output_id": forward["output_id"],
                        "target_node_id": forward["target_node_id"],
                        "path": (
                            f"relay/{forward['source_id']}/{forward['route_id']}/"
                            f"{forward['generation']}"
                        ),
                        **self.store.unseal(forward["encrypted"]),
                    }
                )
            fingerprint = self.store.fingerprint(plan)
            generation = node["generation"] + (fingerprint != node["plan_fingerprint"])
            db.execute(
                "UPDATE broadcast_media_nodes SET generation=?,plan_fingerprint=? WHERE node_id=?",
                (generation, fingerprint, node_id),
            )
            context = {
                "purpose": "broadcast-desired-v2",
                "node_id": node_id,
                "generation": generation,
                "issued_at": utc_now(),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
            }
            return seal(node["public_key"], plan, context)
