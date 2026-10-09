"""Persistent quality samples and allowlisted process diagnostics for administrators."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.broadcast.models import BroadcastError
from app.broadcast.network_control import NetworkControl
from app.broadcast.server_quality import context

if TYPE_CHECKING:
    from app.broadcast.media_control import MediaHeartbeat, Observation
    from app.broadcast.store import BroadcastStore

RETENTION_DAYS = 7
MAX_SAMPLES = 250_000
MAX_EVENTS = 50_000


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def record_sample(
    db: sqlite3.Connection,
    node_id: str,
    heartbeat: MediaHeartbeat,
    route: sqlite3.Row,
    obs: Observation,
    now: str,
) -> None:
    data = obs.model_dump(exclude={"source_identity", "egress_lease_id", "runtime_secret_present"})
    data.update(
        boot=digest(heartbeat.boot_id),
        source_epoch=digest(obs.source_identity or ""),
        role=route["role"],
        desired_enabled=bool(route["desired_enabled"]),
        plan_generation=heartbeat.plan_generation,
        sequence=heartbeat.sequence,
        diagnostics_version=heartbeat.diagnostics_version,
        assessment_context=context(db, route["id"]),
        assessment_transition=bool(
            db.execute(
                "SELECT 1 FROM broadcast_switches WHERE output_id=? "
                "AND (active=1 OR updated_at>=?) LIMIT 1",
                (
                    route["output_id"],
                    (datetime.fromisoformat(now) - timedelta(seconds=5)).isoformat(),
                ),
            ).fetchone()
        ),
    )
    source = db.execute(
        "SELECT s.source_id,src.ingress_node_id FROM broadcast_outputs o "
        "JOIN broadcast_sessions s ON s.id=o.session_id "
        "JOIN broadcast_sources src ON src.id=s.source_id WHERE o.id=?",
        (route["output_id"],),
    ).fetchone()
    data["network"] = NetworkControl.route_view(
        db, route["id"], node_id == source["ingress_node_id"]
    )
    source_view = NetworkControl.source_view(db, source["source_id"])
    # No pairing token, OBS output name, peer address, path, query or credentials enter history.
    data["ingress_network"] = source_view["ingress"]
    data["obs_network"] = {
        k: v for k, v in source_view["obs"].items() if k not in ("boot_id", "sequence")
    }
    signature = digest(
        json.dumps(
            [
                data[k]
                for k in (
                    "boot",
                    "source_epoch",
                    "input_epoch",
                    "publisher_epoch",
                    "role",
                    "desired_enabled",
                    "plan_generation",
                    "source_kind",
                    "publisher_running",
                    "publisher_connected",
                    "safe_error_code",
                    "assessment_context",
                    "assessment_transition",
                )
            ]
        )
    )
    previous = db.execute(
        "SELECT * FROM broadcast_quality_history WHERE route_id=? ORDER BY id DESC LIMIT 1",
        (obs.route_id,),
    ).fetchone()
    interval = (
        5 if route["desired_enabled"] or route["media_warm"] or obs.source_kind != "unknown" else 15
    )
    if previous:
        elapsed = (
            datetime.fromisoformat(now) - datetime.fromisoformat(previous["observed_at"])
        ).total_seconds()
        if previous["signature"] == signature and elapsed < interval:
            return
        prior = json.loads(previous["payload_json"])
        same_input = (
            data["boot"] == prior["boot"]
            and data["source_epoch"] == prior["source_epoch"]
            and data["input_epoch"] == prior.get("input_epoch")
        )
        same_output = (
            data["boot"] == prior["boot"]
            and data["egress_generation"] == prior["egress_generation"]
            and data["publisher_epoch"] == prior.get("publisher_epoch")
        )
        for counter, name, scale, compatible in (
            ("input_bytes", "input_bitrate_bps", 8, same_input),
            ("video_frames", "input_fps", 1, same_input),
            ("publisher_bytes", "output_bitrate_bps", 8, same_output),
            ("publisher_frames", "output_fps", 1, same_output),
        ):
            current, before = data.get(counter), prior.get(counter)
            data[name] = (
                round((current - before) * scale / elapsed, 2)
                if (
                    compatible
                    and elapsed > 0
                    and current is not None
                    and before is not None
                    and current >= before
                )
                else None
            )
    db.execute(
        "INSERT INTO broadcast_quality_history(output_id,route_id,node_id,observed_at,"
        "signature,payload_json) VALUES(?,?,?,?,?,?)",
        (route["output_id"], obs.route_id, node_id, now, signature, json.dumps(data)),
    )


def record_events(
    db: sqlite3.Connection, node_id: str, heartbeat: MediaHeartbeat, now: str
) -> None:
    for event in heartbeat.diagnostic_events:
        if (
            event.route_id
            and not db.execute(
                "SELECT 1 FROM broadcast_routes WHERE id=? AND node_id=?", (event.route_id, node_id)
            ).fetchone()
        ):
            raise BroadcastError("diagnostic_route_scope", 403)
        db.execute(
            "INSERT OR IGNORE INTO broadcast_diagnostic_events(node_id,route_id,boot_hash,"
            "sequence,occurred_at,received_at,component,code,value) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                event.route_id,
                digest(heartbeat.boot_id),
                event.sequence,
                datetime.fromtimestamp(event.at, UTC).isoformat(),
                now,
                event.component,
                event.code,
                event.value,
            ),
        )


def prune(store: BroadcastStore) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat()
    with store.transaction() as db:
        NetworkControl.expire(db)
        db.execute(
            "DELETE FROM broadcast_network_probe_jobs WHERE id IN "
            "(SELECT id FROM broadcast_network_probe_jobs WHERE finished_at<? "
            "ORDER BY created_at LIMIT 1000)",
            (cutoff,),
        )
        for table, stamp, cap in (
            ("broadcast_quality_history", "observed_at", MAX_SAMPLES),
            ("broadcast_diagnostic_events", "received_at", MAX_EVENTS),
        ):
            # Fixed identifiers, bounded deletion batches; no VACUUM in the media/control loop.
            db.execute(
                f"DELETE FROM {table} WHERE id IN "  # noqa: S608
                f"(SELECT id FROM {table} WHERE {stamp}<? LIMIT 5000)",
                (cutoff,),
            )
            db.execute(
                f"DELETE FROM {table} WHERE id IN "  # noqa: S608
                f"(SELECT id FROM {table} WHERE id<=(SELECT id FROM {table} "
                "ORDER BY id DESC LIMIT 1 OFFSET ?) LIMIT 5000)",
                (cap,),
            )


def bounds(hours: int, until: datetime | None) -> tuple[str, str]:
    end = until or datetime.now(UTC)
    if end.tzinfo is None:
        raise BroadcastError("diagnostic_timezone_required", 422)
    end = min(end.astimezone(UTC), datetime.now(UTC))
    return (end - timedelta(hours=hours)).isoformat(), end.isoformat()


def records(
    store: BroadcastStore, output_id: str, since: str, until: str, *, limit: int | None = None
) -> Iterator[dict[str, Any]]:
    """Keyset pages release the SQLite read transaction before sending network bytes."""
    with store.database.connect() as db:
        store.row(db, "SELECT id FROM broadcast_outputs WHERE id=?", (output_id,))
        nodes = {
            r["id"]: r["address"]
            for r in db.execute(
                "SELECT n.id,n.address FROM restream_nodes n "
                "JOIN broadcast_routes r ON r.node_id=n.id WHERE r.output_id=?",
                (output_id,),
            )
        }
    queries = (
        (
            "sample",
            "SELECT h.* FROM broadcast_quality_history h "
            "WHERE h.output_id=? AND h.observed_at>=? AND h.observed_at<=?",
            [output_id, since, until],
        ),
        (
            "process",
            "SELECT h.* FROM broadcast_diagnostic_events h WHERE h.node_id IN "
            "(SELECT node_id FROM broadcast_routes WHERE output_id=?) AND (h.route_id IS NULL "
            "OR h.route_id IN (SELECT id FROM broadcast_routes WHERE output_id=?)) "
            "AND h.received_at>=? AND h.received_at<=?",
            [output_id, output_id, since, until],
        ),
        (
            "event",
            "SELECT h.* FROM broadcast_events h "
            "WHERE h.output_id=? AND h.created_at>=? AND h.created_at<=?",
            [output_id, since, until],
        ),
    )
    for kind, query, parameters in queries:
        cursor, emitted = 2**63 - 1, 0
        while True:
            size = min(200, limit - emitted) if limit is not None else 200
            if size <= 0:
                break
            with store.database.connect() as db:
                rows = db.execute(
                    query + " AND h.id<? ORDER BY h.id DESC LIMIT ?", (*parameters, cursor, size)
                ).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = row["id"]
                if kind == "sample":
                    result = {
                        "kind": kind,
                        "id": row["id"],
                        "at": row["observed_at"],
                        "server": nodes.get(row["node_id"]),
                        **json.loads(row["payload_json"]),
                    }
                elif kind == "process":
                    result = {
                        "kind": kind,
                        "id": row["id"],
                        "at": row["received_at"],
                        "occurred_at": row["occurred_at"],
                        "server": nodes.get(row["node_id"]),
                        "route_id": row["route_id"],
                        "component": row["component"],
                        "code": row["code"],
                        "value": row["value"],
                    }
                else:
                    detail = json.loads(row["safe_detail_json"])
                    code = detail.get("code", "")
                    # Never export arbitrary audit detail or provider payloads.
                    result = {
                        "kind": kind,
                        "id": row["id"],
                        "at": row["created_at"],
                        "event": row["event_type"],
                        "code": code
                        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code)
                        else None,
                    }
                emitted += 1
                yield result


def report(
    store: BroadcastStore, output_id: str, hours: int, until: datetime | None
) -> dict[str, Any]:
    since, end = bounds(hours, until)
    data = list(records(store, output_id, since, end, limit=101))
    groups = {
        kind: [r for r in data if r["kind"] == kind] for kind in ("sample", "process", "event")
    }
    return {
        "output_id": output_id,
        "since": since,
        "until": end,
        "retention_days": RETENTION_DAYS,
        "sample_interval_seconds": 5,
        "idle_interval_seconds": 15,
        "truncated": {kind: len(rows) > 100 for kind, rows in groups.items()},
        **{kind: rows[:100] for kind, rows in groups.items()},
    }
