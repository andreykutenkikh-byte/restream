"""Allowlisted, measured link status; missing samples never imply a healthy phone route."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.broadcast.models import CAPABILITIES
from app.broadcast.store import BroadcastStore


def snapshot(store: BroadcastStore, session_id: str | None = None) -> dict[str, Any]:
    result = store.snapshot(session_id)
    now = datetime.now(UTC)
    with store.database.connect() as db:
        nodes = {
            r["node_id"]: dict(r)
            for r in db.execute(
                "SELECT m.node_id,m.enabled,m.capabilities_json,m.last_seen_at,n.revoked_at "
                "FROM broadcast_media_nodes m JOIN restream_nodes n ON n.id=m.node_id"
            )
        }
        observations = {
            r["route_id"]: dict(r) for r in db.execute("SELECT * FROM broadcast_media_observations")
        }
        for node in result["nodes"]:
            media = nodes.get(node["id"])
            node["mode"] = "managed_egress" if media else "legacy_static"
            node["server_ready"] = bool(
                media
                and media["enabled"]
                and not media["revoked_at"]
                and media["last_seen_at"]
                and (now - datetime.fromisoformat(media["last_seen_at"])).total_seconds() <= 20
                and CAPABILITIES.issubset(json.loads(media["capabilities_json"]))
            )
        for session in result["sessions"]:
            for output in session["outputs"]:
                output["credential_stored"] = bool(
                    db.execute(
                        "SELECT credentials_encrypted IS NOT NULL "
                        "FROM youtube_bindings WHERE output_id=?",
                        (output["id"],),
                    ).fetchone()[0]
                )
                output["active_egress_leases"] = db.execute(
                    "SELECT COUNT(*) FROM broadcast_egress_leases "
                    "WHERE output_id=? AND state='ACTIVE' AND expires_at>?",
                    (output["id"], now.isoformat()),
                ).fetchone()[0]
                for route in output["routes"]:
                    obs = observations.get(route["id"])
                    fresh = bool(
                        obs
                        and (now - datetime.fromisoformat(obs["observed_at"])).total_seconds() <= 5
                    )
                    node = next(n for n in result["nodes"] if n["id"] == route["node_id"])
                    measured = bool(fresh and obs and obs["valid_samples"] >= 2)
                    direct = bool(measured and obs and obs["source_kind"] == "direct")
                    forwarded = bool(measured and obs and obs["source_kind"] == "forwarded")
                    route["server_ready"] = node["server_ready"]
                    route["phone_link"] = {
                        "state": "MEASURED" if direct else "UNKNOWN",
                        "bitrate_bps": obs["bitrate_bps"] if direct and obs else None,
                        "rtt_ms": None,
                        "loss_percent": None,
                        "retransmissions": None,
                    }
                    route["interrelay_link"] = {
                        "state": "MEDIA_READY" if forwarded else "UNKNOWN",
                        "bitrate_bps": obs["bitrate_bps"] if forwarded and obs else None,
                        "rtt_ms": None,
                        "loss_percent": None,
                    }
                    route["egress_link"] = {
                        "state": "CONNECTED"
                        if fresh and obs and obs["publisher_connected"]
                        else ("ERROR" if fresh and obs and obs["safe_error_code"] else "UNKNOWN"),
                        "frames": obs["publisher_frames"] if fresh and obs else None,
                        "bytes": obs["publisher_bytes"] if fresh and obs else None,
                        "viewer_playback": "UNKNOWN",
                    }
                    route["media_state"] = (
                        "LOST"
                        if fresh and obs and obs["safe_error_code"] == "source_lost"
                        else ("LIVE" if measured else "UNKNOWN")
                    )
                current = next(r for r in output["routes"] if r["role"] == "current")
                candidates = [
                    r["id"]
                    for r in output["routes"]
                    if r["role"] == "standby" and r["server_ready"]
                ]
                output["recommendation"] = {
                    "target_route_ids": candidates
                    if session["policy"] == "assisted"
                    and (
                        current["media_state"] == "LOST"
                        or current["egress_link"]["state"] == "ERROR"
                    )
                    else [],
                    "reason": "SERVER_READY_PHONE_PATH_UNKNOWN",
                    "automatic": False,
                }
                if output["switch"]:
                    row = db.execute(
                        "SELECT active,durations_json FROM broadcast_switches WHERE id=?",
                        (output["switch"]["id"],),
                    ).fetchone()
                    output["switch"]["active"] = bool(row["active"])
                    output["switch"]["durations"] = json.loads(row["durations_json"])
    return result


def limited_snapshot(store: BroadcastStore, session_id: str | None) -> dict[str, Any]:
    data = snapshot(store, session_id)
    used = {r["node_id"] for s in data["sessions"] for o in s["outputs"] for r in o["routes"]}
    return {
        "sessions": data["sessions"],
        "nodes": [
            {k: n[k] for k in ("id", "display_name", "server_ready")}
            for n in data["nodes"]
            if n["id"] in used
        ],
        "auto_failover_enabled": False,
    }
