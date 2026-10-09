"""Explainable route suitability from bounded observations, never from ping alone.

This is a read model. It grants no leases and never selects or switches a sender.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.broadcast.network_models import PROBE_CAP_BPS

VERSION = 1
HISTORY_HOURS = 24
MAX_ROWS = 50_000
MAX_EVENTS = 5_000
CACHE_SECONDS = 30
MAX_GAP_SECONDS = 15
STABLE_SECONDS = 30 * 60
EVIDENCE_TTL_SECONDS = 6 * 60 * 60
HEADROOM = 1.5


def context(db: sqlite3.Connection, route_id: str) -> str:
    """A non-secret fingerprint fences source, destination, topology and resources."""
    row = db.execute(
        "SELECT s.source_id,src.ingress_node_id,src.profile_json,b.credential_fingerprint,"
        "m.node_id,m.srt_host,m.srt_port,m.profile_json,m.limits_json "
        "FROM broadcast_routes r JOIN broadcast_outputs o ON o.id=r.output_id "
        "JOIN broadcast_sessions s ON s.id=o.session_id "
        "JOIN broadcast_sources src ON src.id=s.source_id "
        "JOIN youtube_bindings b ON b.output_id=o.id "
        "JOIN broadcast_media_nodes m ON m.node_id=r.node_id WHERE r.id=?",
        (route_id,),
    ).fetchone()
    return hashlib.sha256(json.dumps([VERSION, *tuple(row or ())]).encode()).hexdigest()[:24]


def seconds(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 2)


def input_alive(sample: dict[str, Any]) -> bool:
    return bool(
        sample.get("source_kind") == "direct"
        and sample.get("input_bytes") is not None
        and sample.get("input_age_ms") is not None
        and sample["input_age_ms"] <= 3000
        and sample.get("input_epoch") is not None
    )


def analyse(
    samples: list[dict[str, Any]],
    *,
    scope: str,
    boot: str | None,
    ingress: list[dict[str, Any]],
    legacy_generations: set[int],
    transitions: list[tuple[float, float]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Count adjacent, source-confirmed, steady intervals; gaps remain unknown."""
    input_times = [r["at"] for r in ingress]
    starts = [w[0] for w in transitions]

    def transition(start: float, end: float) -> bool:
        index = bisect.bisect_right(starts, end) - 1
        return index >= 0 and transitions[index][1] >= start

    def source_live(row: dict[str, Any]) -> bool:
        net = row.get("ingress_network") or {}
        if row.get("assessment_context"):
            return bool(net.get("state") == "FRESH" and (net.get("bitrate_bps") or 0) > 0)
        # Old records have no ingress telemetry. A contemporaneous independent
        # direct-input counter must prove activity, even when its own publisher fails.
        i = bisect.bisect_right(input_times, row["at"]) - 1
        if i < 1:
            return False
        current, prior = ingress[i], ingress[i - 1]
        return bool(
            0 <= row["at"] - current["at"] <= 10
            and 0 < current["at"] - prior["at"] <= MAX_GAP_SECONDS
            and input_alive(current)
            and input_alive(prior)
            and current.get("boot") == prior.get("boot")
            and current["input_epoch"] == prior["input_epoch"]
            and current["input_bytes"] > prior["input_bytes"]
        )

    def compatible(row: dict[str, Any]) -> bool:
        fingerprint = row.get("assessment_context")
        return bool(
            fingerprint == scope
            or (not fingerprint and row.get("egress_generation") in legacy_generations)
        )

    result: dict[str, Any] = dict(
        observed_seconds=0.0,
        healthy_seconds=0.0,
        verified_seconds=0.0,
        historical_seconds=0.0,
        backlog_seconds=0.0,
        stalled_seconds=0.0,
        slow_seconds=0.0,
        transition_seconds=0.0,
        unknown_seconds=0.0,
        incidents=0,
        restarts=0,
        media_warnings=0,
        sender_drop_windows=0,
        max_queue_packets=None,
        max_queue_bytes=None,
        last_stream_at=None,
        last_issue_at=None,
        tcp_samples=0,
        tcp_failed_samples=0,
        tcp_window_seconds=0.0,
        tcp_p50_ms=None,
        tcp_p95_ms=None,
        srt_p95_ms=None,
        overhead_percent=None,
        requires_retest=False,
    )
    tcp: dict[str, dict[str, Any]] = {}
    srt: dict[str, dict[str, Any]] = {}
    covered: list[tuple[float, float]] = []
    backlog_run = stall_run = slow_run = 0.0
    previous: dict[str, Any] | None = None
    for row in samples:
        if row.get("assessment_context") == scope:
            link = (row.get("network") or {}).get("tcp") or {}
            if link.get("state") == "FRESH" and link.get("observed_at"):
                tcp[link["observed_at"]] = link
        before, previous = previous, row
        if before is None:
            continue
        elapsed = row["at"] - before["at"]
        selected = all(
            r.get("role") == "current" and r.get("desired_enabled") for r in (before, row)
        )
        if not selected or not all(compatible(r) for r in (before, row)):
            backlog_run = stall_run = slow_run = 0.0
            continue
        if elapsed <= 0 or elapsed > MAX_GAP_SECONDS:
            result["unknown_seconds"] += max(0, elapsed)
            backlog_run = stall_run = slow_run = 0.0
            continue
        if transition(before["at"], row["at"]) or any(
            r.get("assessment_transition") for r in (before, row)
        ):
            result["transition_seconds"] += elapsed
            backlog_run = stall_run = slow_run = 0.0
            continue
        if (
            not all(source_live(r) for r in (before, row))
            or any(
                before.get(k) != row.get(k)
                for k in ("boot", "source_epoch", "input_epoch", "egress_generation")
            )
            or not all(r.get("source_kind") in ("direct", "forwarded") for r in (before, row))
            or any(
                r.get("input_epoch") is None or r.get("publisher_epoch") is None
                for r in (before, row)
            )
        ):
            result["unknown_seconds"] += elapsed
            backlog_run = stall_run = slow_run = 0.0
            continue
        # Progress-age and frame counters must exist; older agents cannot earn a green grade.
        if any(
            r.get("publisher_progress_age_ms") is None
            or r.get("publisher_frames") is None
            or r.get("selector_queue_packets") is None
            for r in (before, row)
        ):
            result["unknown_seconds"] += elapsed
            backlog_run = stall_run = slow_run = 0.0
            continue
        if row.get("publisher_epoch") == before.get("publisher_epoch") and (
            row["publisher_frames"] < before["publisher_frames"]
            or (row.get("publisher_retries") or 0) < (before.get("publisher_retries") or 0)
        ):
            result["unknown_seconds"] += elapsed
            backlog_run = stall_run = slow_run = 0.0
            continue
        current_boot = bool(
            boot and row.get("boot") == boot and row.get("assessment_context") == scope
        )
        result["observed_seconds"] += elapsed
        result["verified_seconds" if current_boot else "historical_seconds"] += elapsed
        result["requires_retest"] |= not current_boot
        result["last_stream_at"] = datetime.fromtimestamp(row["at"], UTC).isoformat()
        covered.append((before["at"], row["at"]))
        queues = [
            r["selector_queue_packets"]
            for r in (before, row)
            if r.get("selector_queue_packets") is not None
        ]
        if queues:
            result["max_queue_packets"] = max(queues + [result["max_queue_packets"] or 0])
        backlog = len(queues) == 2 and min(queues) >= 768
        byte_queues = [
            r["selector_queue_bytes"]
            for r in (before, row)
            if r.get("selector_queue_bytes") is not None
        ]
        if byte_queues:
            result["max_queue_bytes"] = max(byte_queues + [result["max_queue_bytes"] or 0])
        backlog |= len(byte_queues) == 2 and min(byte_queues) >= 12 * 1024 * 1024
        restarted = row.get("publisher_epoch") != before.get("publisher_epoch") or (
            row.get("publisher_retries") or 0
        ) > (before.get("publisher_retries") or 0)
        stalled = bool(
            not restarted
            and (
                not all(
                    r.get("publisher_connected") and r.get("publisher_running")
                    for r in (before, row)
                )
                or row["publisher_frames"] <= before["publisher_frames"]
                or min(r["publisher_progress_age_ms"] for r in (before, row)) > 5000
            )
        )
        nominal_fps = row.get("video_fps") or 0
        input_fps = ((row.get("video_frames") or 0) - (before.get("video_frames") or 0)) / elapsed
        slow = bool(
            not restarted
            and not stalled
            and nominal_fps > 0
            and input_fps >= nominal_fps * 0.8
            and (row["publisher_frames"] - before["publisher_frames"]) / elapsed < nominal_fps * 0.8
        )
        if restarted:
            result["restarts"] += 1
        result["backlog_seconds"] += elapsed if backlog else 0
        result["stalled_seconds"] += elapsed if stalled else 0
        result["slow_seconds"] += elapsed if slow else 0
        for flag, run in ((backlog, backlog_run), (stalled, stall_run), (slow, slow_run)):
            if flag and run < 30 <= run + elapsed:
                result["incidents"] += 1
        backlog_run = backlog_run + elapsed if backlog else 0.0
        stall_run = stall_run + elapsed if stalled else 0.0
        slow_run = slow_run + elapsed if slow else 0.0
        if backlog or stalled or slow or restarted:
            result["last_issue_at"] = result["last_stream_at"]
        else:
            result["healthy_seconds"] += elapsed
        link = (row.get("network") or {}).get("srt") or {}
        if link.get("state") == "FRESH" and link.get("observed_at"):
            # Do not count overlapping or transition-crossing SRT windows as steady.
            window = link.get("window_seconds")
            end = seconds(link["observed_at"])
            if window and window <= MAX_GAP_SECONDS and not transition(end - window, end):
                srt[link["observed_at"]] = link
    newest = samples[-1]["at"] if samples else 0
    tcp = {at: r for at, r in tcp.items() if seconds(at) >= newest - 900}
    if len(tcp) > 1:
        result["tcp_window_seconds"] = seconds(max(tcp)) - seconds(min(tcp))
    result["tcp_samples"] = len(tcp)
    result["tcp_failed_samples"] = sum(not r.get("reachable") for r in tcp.values())
    rtts = [
        float(r["rtt_ms"])
        for r in tcp.values()
        if r.get("reachable") and r.get("rtt_ms") is not None
    ]
    result["tcp_p50_ms"], result["tcp_p95_ms"] = percentile(rtts, 0.5), percentile(rtts, 0.95)
    result["srt_p95_ms"] = percentile(
        [float(r["rtt_ms"]) for r in srt.values() if r.get("rtt_ms") is not None], 0.95
    )
    overhead = [
        max(0.0, (r["wire_bitrate_bps"] / r["media_bitrate_bps"] - 1) * 100)
        for r in srt.values()
        if r.get("media_bitrate_bps") and r.get("wire_bitrate_bps") is not None
    ]
    result["overhead_percent"] = percentile(overhead, 0.5)
    result["sender_drop_windows"] = sum(
        (r.get("sender_dropped_packets") or 0) > 0 for r in srt.values()
    )
    covered_starts = [w[0] for w in covered]
    warning_codes = {
        "invalid_media",
        "too_many_reordered_frames",
        "selector_invalid_avc",
        "selector_nonadvancing_dts",
        "active_queue_overflow",
    }
    for event in events:
        t = event["at"]
        i = bisect.bisect_right(covered_starts, t) - 1
        if (
            i >= 0
            and t <= covered[i][1]
            and not transition(t, t)
            and event["code"] in warning_codes
        ):
            result["media_warnings"] += 1
            result["last_issue_at"] = max(
                result["last_issue_at"] or "", datetime.fromtimestamp(t, UTC).isoformat()
            )
    for key, value in result.items():
        if isinstance(value, float):
            result[key] = round(value, 1)
    return result


def assessment(
    history: dict[str, Any], network: dict[str, Any], *, ready: bool, required_bps: int, now: float
) -> dict[str, Any]:
    # A full new control stream can supersede provisional evidence from the old
    # agent/configuration. Keep the previous incident counts visible separately.
    original = history
    current = history.get("current")
    if current and current["observed_seconds"] >= STABLE_SECONDS:
        history = {**current, "truncated": original["truncated"]}
    else:
        history = {k: v for k, v in history.items() if k != "current"}
    reasons: list[str] = []
    last = history["last_stream_at"]
    age = max(0, now - seconds(last)) if last else None
    recent = age is not None and age <= EVIDENCE_TTL_SECONDS
    usable = age is not None and age <= HISTORY_HOURS * 3600
    probe = network.get("probe") or {}
    probe_age = max(0, now - seconds(probe["finished_at"])) if probe.get("finished_at") else None
    rate = (
        probe.get("throughput_bps")
        if probe.get("state") == "COMPLETED"
        and probe_age is not None
        and probe_age <= EVIDENCE_TTL_SECONDS
        else None
    )
    demand = required_bps * (1 + (history["overhead_percent"] or 0) / 100)
    ratio = round(rate / demand, 2) if rate and demand else None
    capped = bool(rate and rate >= PROBE_CAP_BPS * 0.95)
    tcp = network.get("tcp") or {}
    tcp_fresh = tcp.get("state") == "FRESH"
    state = "UNTESTED"
    if not ready:
        state, reasons = "UNAVAILABLE", ["server_unavailable"]
    elif usable and (history["incidents"] or history["restarts"] >= 2):
        current_problem = current and (current["incidents"] or current["restarts"] >= 2)
        state = "PROBLEM" if current_problem or not history["requires_retest"] else "RISK"
        if history["backlog_seconds"]:
            reasons.append("sustained_backlog")
        if history["stalled_seconds"]:
            reasons.append("sender_stalled")
        if history["restarts"]:
            reasons.append("sender_restarted")
        if history["slow_seconds"]:
            reasons.append("sender_slow")
    elif usable and (
        history["media_warnings"] >= 2
        or history["sender_drop_windows"]
        or (history["max_queue_packets"] or 0) >= 768
        or (history["max_queue_bytes"] or 0) >= 12 * 1024 * 1024
        or history["restarts"]
        or history["stalled_seconds"] >= 15
        or history["slow_seconds"] >= 15
    ):
        state = "RISK"
        if history["media_warnings"]:
            reasons.append("media_warnings")
        if history["sender_drop_windows"]:
            reasons.append("srt_sender_drops")
        if (history["max_queue_packets"] or 0) >= 768 or (
            history["max_queue_bytes"] or 0
        ) >= 12 * 1024 * 1024:
            reasons.append("backlog_observed")
        if history["restarts"]:
            reasons.append("sender_restarted")
        if history["stalled_seconds"] >= 15:
            reasons.append("sender_stalled")
        if history["slow_seconds"] >= 15:
            reasons.append("sender_slow")
    elif usable and history["healthy_seconds"] >= STABLE_SECONDS:
        state = (
            "STABLE"
            if recent and not history["requires_retest"] and not history.get("truncated")
            else "HISTORY"
        )
        reasons.append("steady_stream_observed")
    if ready and ratio is not None and ratio < HEADROOM and state != "PROBLEM":
        if capped:
            reasons.append("probe_ceiling")
        else:
            state = "RISK"
            reasons.append("little_capacity_headroom")
    if ready and tcp_fresh and not tcp.get("reachable"):
        reasons.append("tcp_check_failed")
        if state in ("UNTESTED", "PRECHECK"):
            state = "RISK"
    tcp_unstable = (
        history["tcp_samples"] >= 8 and history["tcp_failed_samples"] / history["tcp_samples"] > 0.1
    )
    if ready and tcp_unstable:
        reasons.append("tcp_unstable")
        if state in ("UNTESTED", "PRECHECK"):
            state = "RISK"
    if (
        state == "UNTESTED"
        and tcp_fresh
        and tcp.get("reachable")
        and history["tcp_samples"] >= 8
        and history["tcp_window_seconds"] >= 120
        and history["tcp_failed_samples"] / history["tcp_samples"] <= 0.05
        and ratio is not None
        and ratio >= HEADROOM
    ):
        state = "PRECHECK"
        reasons.append("precheck_only")
    if not recent and last:
        reasons.append("stream_evidence_stale")
    if history["requires_retest"] and usable:
        reasons.append("retest_after_update")
    if history.get("truncated"):
        reasons.append("history_limited")
    if rate is None and not network.get("local"):
        reasons.append("capacity_unknown")
    if state == "UNTESTED":
        reasons.append("not_enough_stream_observation")
    return dict(
        version=VERSION,
        state=state,
        reasons=reasons,
        history_hours=HISTORY_HOURS,
        confidence="MEASURED"
        if state == "STABLE"
        else "LIMITED"
        if usable and history["observed_seconds"]
        else "PRELIMINARY",
        evidence_age_seconds=round(age) if age is not None else None,
        required_bitrate_bps=required_bps,
        probe_headroom_ratio=ratio,
        probe_capped=capped,
        previous_incidents=max(0, original["incidents"] - history["incidents"]),
        **history,
    )


class QualityReader:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, tuple[Any, ...], dict[str, dict[str, Any]]]] = (
            OrderedDict()
        )

    def history(
        self,
        db: sqlite3.Connection,
        output_id: str,
        ingress_node_id: str,
        scopes: dict[str, str],
        boots: dict[str, str | None],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        binding = db.execute(
            "SELECT credential_fingerprint FROM youtube_bindings WHERE output_id=?", (output_id,)
        ).fetchone()
        fingerprint = binding[0] if binding else None
        switches = db.execute(
            "SELECT created_at,updated_at,active FROM broadcast_switches "
            "WHERE output_id=? AND updated_at>=? ORDER BY created_at",
            (output_id, (now - timedelta(hours=HISTORY_HOURS)).isoformat()),
        ).fetchall()
        key = (
            ingress_node_id,
            tuple(scopes.items()),
            tuple(boots.items()),
            tuple(
                (s["created_at"], None if s["active"] else s["updated_at"], s["active"])
                for s in switches
            ),
            fingerprint,
        )
        with self._lock:
            cached = self._cache.get(output_id)
            if cached and 0 <= now.timestamp() - cached[0] < CACHE_SECONDS and cached[1] == key:
                self._cache.move_to_end(output_id)
                return cached[2]
            value = self._load(
                db, output_id, ingress_node_id, scopes, boots, fingerprint, switches, now
            )
            self._cache[output_id] = (now.timestamp(), key, value)
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
            return value

    @staticmethod
    def _load(
        db: sqlite3.Connection,
        output_id: str,
        ingress_node_id: str,
        scopes: dict[str, str],
        boots: dict[str, str | None],
        fingerprint: str | None,
        switches: list[sqlite3.Row],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        since = (now - timedelta(hours=HISTORY_HOURS)).isoformat()
        rows = db.execute(
            "SELECT * FROM (SELECT route_id,node_id,observed_at,payload_json "
            "FROM broadcast_quality_history WHERE output_id=? "
            "AND observed_at>=? AND observed_at<=? "
            "AND ((json_extract(payload_json,'$.role')='current' "
            "AND json_extract(payload_json,'$.desired_enabled')=1) "
            "OR (node_id=? AND json_extract(payload_json,'$.source_kind')='direct') "
            "OR observed_at>=?) "
            "ORDER BY observed_at DESC,id DESC LIMIT ?) ORDER BY observed_at",
            (
                output_id,
                since,
                now.isoformat(),
                ingress_node_id,
                (now - timedelta(minutes=15)).isoformat(),
                MAX_ROWS + 1,
            ),
        )
        grouped: dict[str, list[dict[str, Any]]] = {r: [] for r in scopes}
        ingress: list[dict[str, Any]] = []
        count = 0
        keep = {
            "assessment_context",
            "assessment_transition",
            "boot",
            "source_epoch",
            "role",
            "desired_enabled",
            "source_kind",
            "input_epoch",
            "input_bytes",
            "input_age_ms",
            "video_frames",
            "video_fps",
            "publisher_epoch",
            "publisher_frames",
            "publisher_progress_age_ms",
            "publisher_connected",
            "publisher_running",
            "publisher_retries",
            "selector_queue_packets",
            "selector_queue_bytes",
            "egress_generation",
            "network",
            "ingress_network",
        }
        for row in rows:
            count += 1
            raw = json.loads(row["payload_json"])
            data = {k: v for k, v in raw.items() if k in keep}
            data["at"] = seconds(row["observed_at"])
            if row["route_id"] in grouped:
                grouped[row["route_id"]].append(data)
            if row["node_id"] == ingress_node_id and input_alive(data):
                ingress.append(data)
        windows: list[tuple[float, float]] = []
        for switch in switches:
            start, end = (
                seconds(switch["created_at"]),
                (now.timestamp() if switch["active"] else seconds(switch["updated_at"])) + 5,
            )
            if windows and start <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(end, windows[-1][1]))
            else:
                windows.append((start, end))
        leases: dict[str, set[int]] = {r: set() for r in scopes}
        if fingerprint:
            for row in db.execute(
                "SELECT DISTINCT route_id,generation FROM broadcast_egress_leases "
                "WHERE output_id=? AND credential_fingerprint=?",
                (output_id, fingerprint),
            ):
                if row["route_id"] in leases:
                    leases[row["route_id"]].add(row["generation"])
        events: dict[str, list[dict[str, Any]]] = {r: [] for r in scopes}
        total_events = 0
        for row in db.execute(
            "SELECT route_id,occurred_at,code FROM broadcast_diagnostic_events "
            "WHERE route_id IN (SELECT id FROM broadcast_routes WHERE output_id=?) "
            "AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at DESC LIMIT ?",
            (output_id, since, now.isoformat(), MAX_EVENTS + 1),
        ):
            total_events += 1
            if row["route_id"] in events:
                events[row["route_id"]].append(
                    dict(at=seconds(row["occurred_at"]), code=row["code"])
                )
        result = {}
        for route, route_samples in grouped.items():
            args: dict[str, Any] = dict(
                scope=scopes[route],
                boot=boots[route],
                ingress=ingress,
                legacy_generations=leases[route],
                transitions=windows,
                events=events[route],
            )
            result[route] = {
                **analyse(route_samples, **args),
                "current": analyse(
                    [
                        r
                        for r in route_samples
                        if r.get("assessment_context") == scopes[route]
                        and r.get("boot") == boots[route]
                    ],
                    **args,
                ),
                "truncated": count > MAX_ROWS or total_events > MAX_EVENTS,
            }
        return result


def recommend(routes: list[dict[str, Any]]) -> dict[str, Any]:
    """Offer candidates, never change the selected route or broadcast intent."""
    rank = {"STABLE": 0, "HISTORY": 1, "PRECHECK": 2}
    candidates = [
        r for r in routes if r["quality"]["state"] in rank and not r.get("admission_error")
    ]
    candidates.sort(
        key=lambda r: (
            rank[r["quality"]["state"]],
            r["quality"]["tcp_failed_samples"] / max(1, r["quality"]["tcp_samples"]),
            r["quality"]["srt_p95_ms"]
            if r["quality"]["srt_p95_ms"] is not None
            else r["quality"]["tcp_p95_ms"]
            if r["quality"]["tcp_p95_ms"] is not None
            else float("inf"),
            -r["quality"]["healthy_seconds"],
            r["id"],
        )
    )
    return {
        "route_ids": [r["id"] for r in candidates[:2]],
        "basis": candidates[0]["quality"]["state"] if candidates else "INSUFFICIENT",
        "automatic": False,
    }
