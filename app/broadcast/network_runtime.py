"""Read-only local transport statistics and low-traffic candidate TCP latency probes."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any, Literal

import httpx

from app.broadcast.network_models import IngressMeasurement, LinkMeasurement, ProbeProgress
from app.broadcast.network_probe import ProbeServer, run_probe, tcp_rtt


def numeric(item: dict[str, Any], key: str) -> float | None:
    value = item.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value) if 0 <= value <= 10**18 else None
    return None


class Counters:
    def __init__(
        self, item: dict[str, Any], previous: tuple[float, dict[str, Any]] | None, now: float
    ) -> None:
        self.item, self.previous = item, previous
        elapsed = now - previous[0] if previous else 0
        self.window = elapsed if 0 < elapsed <= 30 else None

    def delta(self, key: str) -> int | None:
        after = numeric(self.item, key)
        before = numeric(self.previous[1], key) if self.previous else None
        if self.window and after is not None and before is not None and after >= before:
            return int(after - before)
        return None

    def bitrate(self, key: str) -> int | None:
        count = self.delta(key)
        return int(count * 8 / self.window) if count is not None and self.window else None


class TransportSampler:
    def __init__(self) -> None:
        self.previous: dict[str, tuple[float, dict[str, Any]]] = {}

    def sample(
        self,
        plan: dict[str, Any],
        srt: list[dict[str, Any]],
        rtmp: list[dict[str, Any]],
        now: float,
    ) -> tuple[list[LinkMeasurement], list[IngressMeasurement]]:
        links: list[LinkMeasurement] = []
        ingress: list[IngressMeasurement] = []
        updated: dict[str, tuple[float, dict[str, Any]]] = {}
        exports = {e["path"]: e["route_id"] for e in plan.get("exports", [])}
        sources = {f"source/{sid}/direct": sid for sid in plan.get("sources", {})}
        protocols: tuple[tuple[Literal["srt", "rtmp"], list[dict[str, Any]]], ...] = (
            ("srt", srt),
            ("rtmp", rtmp),
        )
        for protocol, connections in protocols:
            for item in connections[:64]:
                if not item.get("id"):
                    continue
                identity = (protocol, item["id"], item.get("path"), item.get("state"))
                epoch = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
                counters = Counters(item, self.previous.get(epoch), now)
                window = counters.window
                updated[epoch] = (now, item)
                path, state = item.get("path"), item.get("state")
                if protocol == "srt" and path in exports and state == "read":
                    links.append(
                        LinkMeasurement(
                            route_id=exports[path],
                            sampled_at=now,
                            kind="srt",
                            reachable=True,
                            rtt_ms=numeric(item, "msRTT"),
                            media_bitrate_bps=counters.bitrate("bytesSentUnique"),
                            wire_bitrate_bps=counters.bitrate("bytesSent"),
                            retransmitted_packets=counters.delta("packetsRetrans"),
                            sender_dropped_packets=counters.delta("packetsSendDrop"),
                            window_seconds=window,
                        )
                    )
                if path in sources and state == "publish":
                    ingress.append(
                        IngressMeasurement(
                            source_id=sources[path],
                            sampled_at=now,
                            protocol=protocol,
                            connection_epoch=epoch,
                            bitrate_bps=counters.bitrate(
                                "bytesReceivedUnique" if protocol == "srt" else "bytesReceived"
                            ),
                            rtt_ms=numeric(item, "msRTT") if protocol == "srt" else None,
                            received_packets=counters.delta("packetsReceivedUnique")
                            if protocol == "srt"
                            else None,
                            missing_packets=counters.delta("packetsReceivedLoss")
                            if protocol == "srt"
                            else None,
                            retransmitted_packets=counters.delta("packetsReceivedRetrans")
                            if protocol == "srt"
                            else None,
                            dropped_packets=counters.delta("packetsReceivedDrop")
                            if protocol == "srt"
                            else None,
                            window_seconds=window,
                        )
                    )
        self.previous = updated
        return links[:32], ingress[:32]


class NetworkMonitor:
    def __init__(
        self,
        api_port: int,
        plan: Callable[[], dict[str, Any]],
        idle: Callable[[], bool],
        probe_port: int | None = None,
    ) -> None:
        self.plan, self.idle = plan, idle
        self.ending, self.lock = threading.Event(), threading.Lock()
        self.links: list[LinkMeasurement] = []
        self.tcp: list[LinkMeasurement] = []
        self.ingress: list[IngressMeasurement] = []
        self.progress: dict[str, ProbeProgress] = {}
        self.sampler = TransportSampler()
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", timeout=1, trust_env=False
        )
        self.server: ProbeServer | None = None
        if probe_port is not None:
            with suppress(OSError):
                self.server = ProbeServer(probe_port, idle)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="link-ping")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.tcp_thread = threading.Thread(target=self._tcp_loop, daemon=True)
        self.probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self.thread.start()
        self.tcp_thread.start()
        self.probe_thread.start()

    @property
    def port(self) -> int | None:
        return self.server.port if self.server else None

    def items(self, kind: str) -> list[dict[str, Any]]:
        response = self.client.get(f"/v3/{kind}/list")
        if response.is_success:
            payload = response.json()
            return payload.get("items", [])[:64] if isinstance(payload, dict) else []
        return []

    def _loop(self) -> None:
        while not self.ending.is_set():
            try:
                plan = self.plan()
                links, ingress = self.sampler.sample(
                    plan, self.items("srtconns"), self.items("rtmpconns"), time.time()
                )
                with self.lock:
                    self.links, self.ingress = links, ingress
            except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
                pass  # Missing measurements expire; never fabricate zero loss.
            self.ending.wait(2)

    def _tcp_loop(self) -> None:
        while not self.ending.is_set():
            try:
                targets = self.plan().get("monitor_targets", [])[:32]
                futures = [self.pool.submit(tcp_rtt, t["host"], int(t["port"])) for t in targets]
                tcp = []
                for target, future in zip(targets, futures, strict=True):
                    reachable, rtt = future.result(timeout=10)
                    tcp.append(
                        LinkMeasurement(
                            route_id=target["route_id"],
                            sampled_at=time.time(),
                            kind="tcp",
                            reachable=reachable,
                            rtt_ms=rtt,
                        )
                    )
                with self.lock:
                    self.tcp = tcp
            except (ValueError, KeyError, TypeError, TimeoutError):
                pass
            self.ending.wait(15)

    def _probe_loop(self) -> None:
        while not self.ending.is_set():
            try:
                jobs = self.plan().get("network_probes", [])[:4]
                if self.server:
                    self.server.accept(jobs)
                for job in jobs:
                    with self.lock:
                        old = self.progress.get(job["id"])
                    if job["role"] == "receiver" and self.server:
                        progress = ProbeProgress(job_id=job["id"], role="receiver", state="READY")
                    elif job["role"] == "sender" and job.get("peer_ready") and old is None:
                        progress = run_probe(job, self.idle)
                    else:
                        continue
                    with self.lock:
                        self.progress[job["id"]] = progress
                allowed = {job["id"] for job in jobs}
                with self.lock:
                    self.progress = {k: v for k, v in self.progress.items() if k in allowed}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
                pass
            self.ending.wait(2)

    def snapshot(
        self,
    ) -> tuple[list[LinkMeasurement], list[IngressMeasurement], list[ProbeProgress]]:
        with self.lock:
            return [*self.links, *self.tcp], list(self.ingress), list(self.progress.values())[:4]

    def close(self) -> None:
        self.ending.set()
        if self.server:
            self.server.close()
        self.thread.join(timeout=12)
        self.tcp_thread.join(timeout=12)
        self.probe_thread.join(timeout=12)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.client.close()
