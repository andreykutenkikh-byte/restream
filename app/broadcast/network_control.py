"""Scoped measurements and idle-only, short, authenticated capacity tests."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.broadcast.models import BroadcastError
from app.broadcast.network_models import (
    NETWORK_CAPABILITY,
    PROBE_CAP_BPS,
    PROBE_CAPABILITY,
    PROBE_MAX_BYTES,
    PROBE_SECONDS,
    ObsMeasurement,
)
from app.db import utc_now

if TYPE_CHECKING:
    from app.broadcast.media_control import MediaControl, MediaHeartbeat
    from app.broadcast.store import BroadcastStore


def age(stamp: str) -> float:
    return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds())


def measured(row: sqlite3.Row | None, ttl: int) -> dict[str, Any]:
    if row is None:
        return {"state": "UNKNOWN"}
    elapsed = age(row["observed_at"])
    return {
        "state": "FRESH" if elapsed <= ttl else "STALE",
        "age_seconds": round(elapsed, 1),
        "observed_at": row["observed_at"],
        **json.loads(row["payload_json"]),
    }


class NetworkControl:
    def __init__(self, store: BroadcastStore, media: MediaControl) -> None:
        self.store, self.media = store, media

    def record(self, db: sqlite3.Connection, node_id: str, data: MediaHeartbeat) -> None:
        if data.network_version != 1 or NETWORK_CAPABILITY not in data.capabilities:
            return
        if data.probe_port is not None and PROBE_CAPABILITY not in data.capabilities:
            raise BroadcastError("network_capability_missing", 422)
        for item in data.link_measurements:
            self.store.row(
                db,
                "SELECT r.id FROM broadcast_routes r JOIN broadcast_outputs o ON o.id=r.output_id "
                "JOIN broadcast_sessions s ON s.id=o.session_id "
                "JOIN broadcast_sources src ON src.id=s.source_id "
                "WHERE r.id=? AND src.ingress_node_id=?",
                (item.route_id, node_id),
            )
            elapsed = datetime.now(UTC).timestamp() - item.sampled_at
            if not -5 <= elapsed <= 45:
                continue
            db.execute(
                "INSERT INTO broadcast_network_links VALUES(?,?,?,?,?) "
                "ON CONFLICT(route_id,kind) DO UPDATE SET "
                "reporter_node_id=excluded.reporter_node_id,observed_at=excluded.observed_at,"
                "payload_json=excluded.payload_json "
                "WHERE excluded.observed_at>broadcast_network_links.observed_at",
                (
                    item.route_id,
                    item.kind,
                    node_id,
                    datetime.fromtimestamp(item.sampled_at, UTC).isoformat(),
                    item.model_dump_json(),
                ),
            )
        for incoming in data.ingress_measurements:
            self.store.row(
                db,
                "SELECT id FROM broadcast_sources WHERE id=? AND ingress_node_id=?",
                (incoming.source_id, node_id),
            )
            elapsed = datetime.now(UTC).timestamp() - incoming.sampled_at
            if not -5 <= elapsed <= 10:
                continue
            # RTMP/TCP does not expose inbound packet loss through this API.
            if incoming.protocol == "rtmp":
                incoming = incoming.model_copy(
                    update=dict.fromkeys(
                        (
                            "rtt_ms",
                            "received_packets",
                            "missing_packets",
                            "retransmitted_packets",
                            "dropped_packets",
                        )
                    )
                )
            db.execute(
                "INSERT INTO broadcast_ingress_metrics VALUES(?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "reporter_node_id=excluded.reporter_node_id,observed_at=excluded.observed_at,"
                "payload_json=excluded.payload_json "
                "WHERE excluded.observed_at>broadcast_ingress_metrics.observed_at",
                (
                    incoming.source_id,
                    node_id,
                    datetime.fromtimestamp(incoming.sampled_at, UTC).isoformat(),
                    incoming.model_dump_json(),
                ),
            )
        self.expire(db)
        for progress in data.probe_progress:
            row = db.execute(
                "SELECT * FROM broadcast_network_probe_jobs WHERE id=?", (progress.job_id,)
            ).fetchone()
            if row is None or row["state"] in ("COMPLETED", "FAILED"):
                continue
            expected = row["source_node_id" if progress.role == "sender" else "target_node_id"]
            if node_id != expected:
                raise BroadcastError("network_probe_scope", 403)
            if progress.state == "FAILED":
                db.execute(
                    "UPDATE broadcast_network_probe_jobs SET state='FAILED',safe_error=?,"
                    "finished_at=?,encrypted='' WHERE id=?",
                    (progress.safe_error or "peer_unavailable", utc_now(), row["id"]),
                )
            elif progress.role == "receiver" and progress.state == "READY":
                db.execute(
                    "UPDATE broadcast_network_probe_jobs SET target_ready=1,state='READY' "
                    "WHERE id=? AND state='WAITING'",
                    (row["id"],),
                )
            elif progress.role == "sender" and row["target_ready"]:
                if progress.state == "RUNNING":
                    db.execute(
                        "UPDATE broadcast_network_probe_jobs SET state='RUNNING' WHERE id=?",
                        (row["id"],),
                    )
                elif progress.state == "COMPLETED":
                    if (
                        progress.bytes_received is None
                        or progress.elapsed_ms is None
                        or progress.throughput_bps is None
                    ):
                        raise BroadcastError("network_probe_result_required", 422)
                    computed = int(progress.bytes_received * 8000 / progress.elapsed_ms)
                    if abs(computed - progress.throughput_bps) > max(1, computed // 100):
                        raise BroadcastError("network_probe_result_invalid", 422)
                    db.execute(
                        "UPDATE broadcast_network_probe_jobs SET state='COMPLETED',"
                        "throughput_bps=?,"
                        "bytes_received=?,elapsed_ms=?,finished_at=?,encrypted='' WHERE id=?",
                        (
                            computed,
                            progress.bytes_received,
                            progress.elapsed_ms,
                            utc_now(),
                            row["id"],
                        ),
                    )

    def plan(self, db: sqlite3.Connection, node_id: str) -> dict[str, Any]:
        targets = []
        for row in db.execute(
            "SELECT r.id,r.node_id,m.srt_host,m.rtmp_port,m.probe_port,m.last_seen_at,"
            "m.enabled,n.revoked_at,n.ssh_port FROM broadcast_routes r "
            "JOIN broadcast_outputs o ON o.id=r.output_id "
            "JOIN broadcast_sessions s ON s.id=o.session_id "
            "JOIN broadcast_sources src ON src.id=s.source_id "
            "JOIN broadcast_media_nodes m ON m.node_id=r.node_id "
            "JOIN restream_nodes n ON n.id=r.node_id WHERE src.ingress_node_id=? "
            "AND r.node_id!=? ORDER BY r.id LIMIT 32",
            (node_id, node_id),
        ):
            if not row["enabled"] or row["revoked_at"]:
                continue
            # Native SSH is reachable even when rootless media uses UDP forwarding.
            port = row["probe_port"] or row["rtmp_port"] or row["ssh_port"]
            targets.append({"route_id": row["id"], "host": row["srt_host"], "port": port})
        self.expire(db)
        jobs = []
        for row in db.execute(
            "SELECT p.*,m.srt_host,m.probe_port FROM broadcast_network_probe_jobs p "
            "JOIN broadcast_media_nodes m ON m.node_id=p.target_node_id "
            "WHERE (p.source_node_id=? OR p.target_node_id=?) "
            "AND p.state IN ('WAITING','READY','RUNNING')",
            (node_id, node_id),
        ):
            jobs.append(
                {
                    "id": row["id"],
                    "role": "sender" if node_id == row["source_node_id"] else "receiver",
                    "host": row["srt_host"],
                    "port": row["probe_port"],
                    "peer_ready": bool(row["target_ready"]),
                    "expires_at": row["expires_at"],
                    **self.store.unseal(row["encrypted"]),
                }
            )
        return {"monitor_targets": targets, "network_probes": jobs}

    @staticmethod
    def expire(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE broadcast_network_probe_jobs SET state='FAILED',safe_error='expired',"
            "finished_at=?,encrypted='' WHERE state IN ('WAITING','READY','RUNNING') "
            "AND expires_at<=?",
            (utc_now(), utc_now()),
        )

    def idle(self, db: sqlite3.Connection, source_node: str, target_node: str) -> bool:
        nodes = (source_node, target_node)
        if db.execute(
            "SELECT 1 FROM broadcast_routes r JOIN broadcast_outputs o ON o.id=r.output_id "
            "JOIN broadcast_sessions s ON s.id=o.session_id "
            "JOIN broadcast_sources src ON src.id=s.source_id "
            "WHERE (r.node_id IN (?,?) OR src.ingress_node_id IN (?,?)) "
            "AND (o.desired_enabled=1 OR r.media_warm=1) LIMIT 1",
            (*nodes, *nodes),
        ).fetchone():
            return False
        for node in nodes:
            fresh = db.execute(
                "SELECT enabled,last_seen_at FROM broadcast_media_nodes WHERE node_id=?",
                (node,),
            ).fetchone()
            if not fresh or not fresh["enabled"] or not fresh["last_seen_at"]:
                return False
            if age(fresh["last_seen_at"]) > 10:
                return False
        for row in db.execute(
            "SELECT source_kind,valid_samples,observed_at,publisher_running FROM "
            "broadcast_media_observations WHERE node_id IN (?,?)",
            nodes,
        ):
            if age(row["observed_at"]) <= 10 and (
                row["source_kind"] != "unknown" or row["publisher_running"]
            ):
                return False
        for row in db.execute(
            "SELECT observed_at,payload_json FROM broadcast_ingress_metrics "
            "WHERE reporter_node_id IN (?,?)",
            nodes,
        ):
            if age(row["observed_at"]) <= 10:
                return False
        return not db.execute("SELECT 1 FROM broadcast_switches WHERE active=1 LIMIT 1").fetchone()

    def start_probe(self, route_id: str, key: str) -> str:
        with self.store.transaction() as db:
            prior = self.store.replay(db, "network-probe", key, route_id)
            if prior:
                return prior
            row = self.store.row(
                db,
                "SELECT r.node_id,src.ingress_node_id FROM broadcast_routes r "
                "JOIN broadcast_outputs o ON o.id=r.output_id "
                "JOIN broadcast_sessions s ON s.id=o.session_id "
                "JOIN broadcast_sources src ON src.id=s.source_id WHERE r.id=?",
                (route_id,),
            )
            source, target = row["ingress_node_id"], row["node_id"]
            if source == target:
                raise BroadcastError("network_local_route")
            for node in (source, target):
                ready = self.media.ready_node(db, node, capability=PROBE_CAPABILITY)
                if not ready["probe_port"]:
                    raise BroadcastError("network_probe_unavailable")
            self.expire(db)
            if not self.idle(db, source, target):
                raise BroadcastError("network_probe_media_active")
            if db.execute(
                "SELECT 1 FROM broadcast_network_probe_jobs "
                "WHERE state IN "
                "('WAITING','READY','RUNNING')"
            ).fetchone():
                raise BroadcastError("network_probe_busy")
            job = secrets.token_hex(16)
            db.execute(
                "INSERT INTO broadcast_network_probe_jobs(id,route_id,source_node_id,"
                "target_node_id,"
                "state,encrypted,created_at,expires_at) VALUES(?,?,?,?,'WAITING',?,?,?)",
                (
                    job,
                    route_id,
                    source,
                    target,
                    self.store.seal({"token": secrets.token_urlsafe(32)}),
                    utc_now(),
                    (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
                ),
            )
            self.store.remember(db, "network-probe", key, route_id, job)
            return job

    def pair_obs(self, source_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            self.store.row(db, "SELECT id FROM broadcast_sources WHERE id=?", (source_id,))
            db.execute(
                "INSERT OR REPLACE INTO broadcast_obs_monitors(source_id,token_digest,"
                "created_at) VALUES(?,?,?)",
                (source_id, hashlib.sha256(token.encode()).hexdigest(), utc_now()),
            )
            db.execute("DELETE FROM broadcast_obs_samples WHERE source_id=?", (source_id,))
        return token

    def revoke_obs(self, source_id: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE broadcast_obs_monitors SET revoked_at=? WHERE source_id=?",
                (utc_now(), source_id),
            )
            db.execute("DELETE FROM broadcast_obs_samples WHERE source_id=?", (source_id,))

    def record_obs(self, token: str, data: ObsMeasurement) -> None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM broadcast_obs_monitors WHERE token_digest=? AND revoked_at IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if row is None:
                raise BroadcastError("obs_monitor_authentication_failed", 401)
            if row["boot_id"] == data.boot_id and data.sequence <= row["last_sequence"]:
                raise BroadcastError("obs_monitor_stale_sample")
            previous = db.execute(
                "SELECT * FROM broadcast_obs_samples WHERE source_id=?", (row["source_id"],)
            ).fetchone()
            elapsed = age(previous["observed_at"]) if previous else 0.0
            if previous and elapsed < 1:
                raise BroadcastError("obs_monitor_rate_limit", 429)
            payload = data.model_dump()
            payload.update(
                bitrate_bps=None,
                dropped_frames_delta=None,
                dropped_frames_percent=None,
                reconnects_delta=None,
                window_seconds=None,
            )
            prior = json.loads(previous["payload_json"]) if previous else {}
            same = (
                prior.get("boot_id") == data.boot_id
                and 1 <= elapsed <= 30
                and data.duration_ms >= prior.get("duration_ms", 0)
                and data.total_frames >= prior.get("total_frames", 0)
                and data.dropped_frames >= prior.get("dropped_frames", 0)
                and data.bytes_sent >= prior.get("bytes_sent", 0)
            )
            if same and data.active and prior.get("active"):
                frames = data.total_frames - prior["total_frames"]
                dropped = data.dropped_frames - prior["dropped_frames"]
                payload.update(
                    bitrate_bps=int((data.bytes_sent - prior["bytes_sent"]) * 8 / elapsed),
                    dropped_frames_delta=dropped,
                    dropped_frames_percent=round(dropped * 100 / frames, 2) if frames else None,
                    reconnects_delta=int(data.reconnecting and not prior["reconnecting"]),
                    window_seconds=round(elapsed, 2),
                )
            db.execute(
                "UPDATE broadcast_obs_monitors SET boot_id=?,last_sequence=? WHERE source_id=?",
                (data.boot_id, data.sequence, row["source_id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO broadcast_obs_samples VALUES(?,?,?)",
                (row["source_id"], utc_now(), json.dumps(payload)),
            )

    @staticmethod
    def source_view(db: sqlite3.Connection, source_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM broadcast_ingress_metrics WHERE source_id=?", (source_id,)
        ).fetchone()
        obs = db.execute(
            "SELECT * FROM broadcast_obs_samples WHERE source_id=?", (source_id,)
        ).fetchone()
        paired = db.execute(
            "SELECT 1 FROM broadcast_obs_monitors WHERE source_id=? AND revoked_at IS NULL",
            (source_id,),
        ).fetchone()
        obs_view = measured(obs, 10)
        for key in ("boot_id", "sequence"):
            obs_view.pop(key, None)
        return {"ingress": measured(row, 10), "obs": {**obs_view, "paired": bool(paired)}}

    @staticmethod
    def route_view(db: sqlite3.Connection, route_id: str, local: bool) -> dict[str, Any]:
        if local:
            return {"local": True, "tcp": {"state": "LOCAL"}, "srt": {"state": "LOCAL"}}
        source = db.execute(
            "SELECT src.ingress_node_id FROM broadcast_routes r "
            "JOIN broadcast_outputs o ON o.id=r.output_id "
            "JOIN broadcast_sessions s ON s.id=o.session_id "
            "JOIN broadcast_sources src ON src.id=s.source_id WHERE r.id=?",
            (route_id,),
        ).fetchone()
        links = {
            r["kind"]: r
            for r in db.execute(
                "SELECT * FROM broadcast_network_links WHERE route_id=? AND reporter_node_id=?",
                (route_id, source[0] if source else None),
            )
        }
        job = db.execute(
            "SELECT * FROM broadcast_network_probe_jobs WHERE route_id=? AND source_node_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (route_id, source[0] if source else None),
        ).fetchone()
        probe = (
            {
                k: job[k]
                for k in (
                    "id",
                    "state",
                    "created_at",
                    "finished_at",
                    "throughput_bps",
                    "bytes_received",
                    "elapsed_ms",
                    "safe_error",
                )
            }
            if job
            else None
        )
        if probe:
            probe.update(cap_bps=PROBE_CAP_BPS, seconds=PROBE_SECONDS, max_bytes=PROBE_MAX_BYTES)
        return {
            "local": False,
            "tcp": measured(links.get("tcp"), 45),
            "srt": measured(links.get("srt"), 10),
            "probe": probe,
        }
