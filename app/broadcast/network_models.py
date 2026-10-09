"""Public measurements, with explicit transport and missing-data semantics."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.broadcast.models import Input

NETWORK_CAPABILITY = "network_monitor_v1"
PROBE_CAPABILITY = "network_probe_v1"
PROBE_SECONDS = 3
PROBE_CAP_BPS = 64_000_000
PROBE_MAX_BYTES = PROBE_SECONDS * PROBE_CAP_BPS // 8


class LinkMeasurement(Input):
    route_id: str = Field(min_length=1, max_length=64)
    sampled_at: float = Field(ge=0, allow_inf_nan=False)
    kind: Literal["tcp", "srt"]
    reachable: bool
    rtt_ms: float | None = Field(default=None, ge=0, le=60000, allow_inf_nan=False)
    jitter_ms: float | None = Field(default=None, ge=0, le=60000, allow_inf_nan=False)
    media_bitrate_bps: int | None = Field(default=None, ge=0, le=1_000_000_000)
    wire_bitrate_bps: int | None = Field(default=None, ge=0, le=2_000_000_000)
    retransmitted_packets: int | None = Field(default=None, ge=0, le=10**12)
    sender_dropped_packets: int | None = Field(default=None, ge=0, le=10**12)
    window_seconds: float | None = Field(default=None, gt=0, le=120, allow_inf_nan=False)


class IngressMeasurement(Input):
    source_id: str = Field(min_length=1, max_length=64)
    sampled_at: float = Field(ge=0, allow_inf_nan=False)
    protocol: Literal["rtmp", "srt"]
    connection_epoch: str = Field(min_length=1, max_length=64)
    bitrate_bps: int | None = Field(default=None, ge=0, le=1_000_000_000)
    rtt_ms: float | None = Field(default=None, ge=0, le=60000, allow_inf_nan=False)
    received_packets: int | None = Field(default=None, ge=0, le=10**12)
    missing_packets: int | None = Field(default=None, ge=0, le=10**12)
    retransmitted_packets: int | None = Field(default=None, ge=0, le=10**12)
    dropped_packets: int | None = Field(default=None, ge=0, le=10**12)
    window_seconds: float | None = Field(default=None, gt=0, le=120, allow_inf_nan=False)


class ProbeProgress(Input):
    job_id: str = Field(min_length=1, max_length=64)
    role: Literal["sender", "receiver"]
    state: Literal["READY", "RUNNING", "COMPLETED", "FAILED"]
    throughput_bps: int | None = Field(default=None, ge=0, le=2_000_000_000)
    bytes_received: int | None = Field(default=None, ge=0, le=PROBE_MAX_BYTES)
    elapsed_ms: float | None = Field(default=None, gt=0, le=15000, allow_inf_nan=False)
    safe_error: (
        Literal["media_active", "peer_unavailable", "authentication_failed", "expired"] | None
    ) = None


class ObsMeasurement(Input):
    sequence: int = Field(ge=1, le=10**15)
    boot_id: str = Field(min_length=16, max_length=64)
    active: bool
    reconnecting: bool
    duration_ms: int = Field(ge=0, le=10**15)
    total_frames: int = Field(ge=0, le=10**12)
    dropped_frames: int = Field(ge=0, le=10**12)
    bytes_sent: int = Field(ge=0, le=10**18)
    congestion: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
