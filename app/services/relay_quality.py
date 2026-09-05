"""Bounded, in-memory health evaluation for normalized relay snapshots.

The tracker is intentionally conservative.  It measures the active ingest path
and the readiness of a standby server, but it never claims to measure the
phone-to-standby route.  It has no persistence and performs no background work.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from threading import Lock
from typing import Literal

MAX_SAMPLES_PER_ROUTE = 72
MAX_TRACKED_ROUTES = 32
BASELINE_WARMUP_SAMPLES = 2
BASELINE_MIN_SAMPLES = 6
BASELINE_ALPHA = 0.08
BASELINE_MAX_UPWARD_SAMPLE_RATIO = 1.25
EMA_ALPHA = 0.25
EMA_MAX_UPWARD_SAMPLE_RATIO = 1.50
BITRATE_YELLOW_RATIO = 0.75
BITRATE_RED_RATIO = 0.50
YELLOW_MIN_SAMPLES = 2
RED_MIN_SAMPLES = 3
RED_MIN_DURATION_SECONDS = 8.0
RECOVERY_MIN_GOOD_SAMPLES = 3
HEARTBEAT_DELAYED_SECONDS = 10.0
HEARTBEAT_BLACK_SECONDS = 30.0
MEDIA_STALLED_BLACK_SECONDS = 30.0
BASELINE_RESET_OFFLINE_SECONDS = 60.0
YOUTUBE_CONNECTING_YELLOW_SECONDS = 10.0
CPU_PRESSURE_PERCENT = 85.0
CPU_CRITICAL_PERCENT = 95.0
MEMORY_PRESSURE_RATIO = 0.15
MEMORY_CRITICAL_RATIO = 0.05
RESOURCE_PRESSURE_MIN_SAMPLES = 2
STANDBY_FRESH_HEARTBEAT_SECONDS = 10.0
STANDBY_HEALTHY_MIN_SECONDS = 10.0
RECOMMENDATION_COOLDOWN_SECONDS = 60.0
# Observation policy, not agent recovery telemetry: the conservative 94-second
# runtime/observation budget is rounded up to the next 30-second retry period.
RECOVERY_GRACE_SECONDS = 120.0
ACTIVE_CONTEXT_RETENTION_SECONDS = 120.0

RouteKind = Literal["main", "relay"]
StreamState = Literal["idle", "active", "ambiguous", "unknown"]
BitrateTrend = Literal["unknown", "rising", "stable", "falling"]


class HealthLevel(StrEnum):
    UNKNOWN = "unknown"
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    BLACK = "black"


class MeasurementConfidence(StrEnum):
    """The only measurement claims supported by Stage 4B.0."""

    ACTIVE_PATH_MEASURED = "active_path_measured"
    STANDBY_SERVER_READINESS = "standby_server_readiness"


class RecommendationAction(StrEnum):
    STAY = "stay"
    WATCH = "watch"
    SWITCH_RECOMMENDED = "switch_recommended"
    RECONNECT = "reconnect"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamRouteSnapshot:
    """Secret-free normalized input from the existing main/relay services."""

    route_id: str
    display_name: str
    kind: RouteKind
    available: bool
    live: bool
    ready: bool
    source: str
    input_bitrate_bps: int | None
    youtube_forward_state: str
    overall_state: str
    heartbeat_age_seconds: float | None
    host_cpu_percent: float | None = None
    host_memory_available_bytes: int | None = None
    host_memory_total_bytes: int | None = None
    error_code: str | None = None
    pending_command: str | None = None
    recommendation_eligible: bool = True
    portrait_profile_valid: bool = False
    youtube_configured: bool = False
    service_state: str = "unknown"
    main_process_state: str = "unknown"
    srt_listener_state: str = "unknown"

    def __post_init__(self) -> None:
        if _SAFE_ROUTE_ID_RE.fullmatch(self.route_id) is None:
            raise ValueError("route_id must be a safe opaque identifier")
        if not self.display_name.strip() or len(self.display_name) > 128:
            raise ValueError("display_name must contain between 1 and 128 characters")
        normalized_name = self.display_name.strip()
        if (
            _IPV4_IN_TEXT_RE.search(normalized_name) is not None
            or "://" in normalized_name
            or any(ord(character) < 32 for character in normalized_name)
        ):
            raise ValueError("display_name must not contain network locators")
        object.__setattr__(self, "display_name", normalized_name)
        if self.kind not in ("main", "relay"):
            raise ValueError("kind must be main or relay")


@dataclass(frozen=True, slots=True)
class HealthAssessment:
    level: HealthLevel
    title: str
    message: str
    reason_codes: tuple[str, ...]
    confidence: MeasurementConfidence
    state_duration_seconds: float

    def as_payload(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "title": self.title,
            "message": self.message,
            "reason_codes": list(self.reason_codes),
            "confidence": self.confidence.value,
            "state_duration_seconds": round(self.state_duration_seconds, 1),
        }


@dataclass(frozen=True, slots=True)
class RouteQualityView:
    route_id: str
    display_name: str
    kind: RouteKind
    source: str
    input_bitrate_bps: int | None
    ema_input_bitrate_bps: int | None
    stable_baseline_bps: int | None
    bitrate_trend: BitrateTrend
    youtube_forward_state: str
    overall_state: str
    heartbeat_age_seconds: float | None
    host_cpu_percent: float | None
    host_memory_available_bytes: int | None
    host_memory_total_bytes: int | None

    def as_payload(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "source": self.source,
            "input_bitrate_bps": self.input_bitrate_bps,
            "ema_input_bitrate_bps": self.ema_input_bitrate_bps,
            "stable_baseline_bps": self.stable_baseline_bps,
            "bitrate_trend": self.bitrate_trend,
            "youtube_forward_state": self.youtube_forward_state,
            "overall_state": self.overall_state,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "host_cpu_percent": self.host_cpu_percent,
            "host_memory_available_bytes": self.host_memory_available_bytes,
            "host_memory_total_bytes": self.host_memory_total_bytes,
        }


@dataclass(frozen=True, slots=True)
class RouteRecommendation:
    action: RecommendationAction
    target_route_id: str | None
    target_display_name: str | None
    confidence: MeasurementConfidence
    reason: str
    reason_code: str
    route_to_target_measured: Literal[False] = False

    def as_payload(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "target_route_id": self.target_route_id,
            "target_display_name": self.target_display_name,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "route_to_target_measured": False,
        }


@dataclass(frozen=True, slots=True)
class RelayQualityEvaluation:
    stream_state: StreamState
    health: HealthAssessment
    current_route: RouteQualityView | None
    standby_route: RouteQualityView | None
    recommendation: RouteRecommendation

    def as_payload(self) -> dict[str, object]:
        return {
            "stream_state": self.stream_state,
            "health": self.health.as_payload(),
            "current_route": (
                self.current_route.as_payload() if self.current_route is not None else None
            ),
            "standby_route": (
                self.standby_route.as_payload() if self.standby_route is not None else None
            ),
            "recommendation": self.recommendation.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class _Sample:
    observed_at: float
    live: bool
    bitrate_bps: int | None
    heartbeat_age_seconds: float | None


@dataclass(slots=True)
class _RouteState:
    samples: deque[_Sample] = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES_PER_ROUTE))
    baseline_candidates: deque[float] = field(
        default_factory=lambda: deque(maxlen=MAX_SAMPLES_PER_ROUTE)
    )
    valid_live_samples: int = 0
    ema_bps: float | None = None
    baseline_bps: float | None = None
    previous_ema_bps: float | None = None
    consecutive_good: int = 0
    consecutive_yellow: int = 0
    consecutive_red: int = 0
    cpu_pressure_samples: int = 0
    memory_pressure_samples: int = 0
    red_started_at: float | None = None
    offline_started_at: float | None = None
    zero_bitrate_started_at: float | None = None
    youtube_connecting_started_at: float | None = None
    standby_ready_since: float | None = None
    level: HealthLevel = HealthLevel.UNKNOWN
    level_started_at: float | None = None
    last_seen_at: float = 0.0
    source_lost_started_at: float | None = None

    def reset_measurements(self) -> None:
        self.samples.clear()
        self.baseline_candidates.clear()
        self.valid_live_samples = 0
        self.ema_bps = None
        self.baseline_bps = None
        self.previous_ema_bps = None
        self.consecutive_good = 0
        self.consecutive_yellow = 0
        self.consecutive_red = 0
        self.cpu_pressure_samples = 0
        self.memory_pressure_samples = 0
        self.red_started_at = None
        self.zero_bitrate_started_at = None
        self.youtube_connecting_started_at = None
        self.level = HealthLevel.UNKNOWN
        self.level_started_at = None


_HEALTHY_OVERALL_STATES = frozenset({"active", "healthy", "ok", "ready", "running"})
_DEGRADED_OVERALL_STATES = frozenset({"degraded", "failed", "error"})
_PROCESS_FAILED_STATES = frozenset(
    {"offline", "process_failed", "service_failed", "stopped_unexpectedly", "unavailable"}
)
_YOUTUBE_ACTIVE_STATES = frozenset({"active", "forwarding", "live", "running"})
_YOUTUBE_CONNECTING_STATES = frozenset({"connecting", "starting", "pending"})
_YOUTUBE_FAILED_STATES = frozenset({"failed", "error", "rejected"})
_STANDBY_OVERALL_STATES = frozenset({"healthy", "ok", "ready", "stopped", "inactive"})
_SAFE_ROUTE_ID_RE = re.compile(r"\A[a-zA-Z0-9_-]{1,128}\Z")
_IPV4_IN_TEXT_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


class RelayQualityTracker:
    """Evaluate stream quality without persistence or unbounded telemetry."""

    def __init__(self) -> None:
        self._states: dict[str, _RouteState] = {}
        self._session_digest: bytes | None = None
        self._session_initialized = False
        self._active_route_id: str | None = None
        self._last_active_observed_at: float | None = None
        self._unknown_context = False
        self._last_recommendation_at: float | None = None
        self._last_recommendation_target: str | None = None
        self._recommendation_episode_active = False
        self._lock = Lock()

    @property
    def tracked_route_count(self) -> int:
        with self._lock:
            return len(self._states)

    @property
    def tracked_route_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._states)

    def route_sample_count(self, route_id: str) -> int:
        with self._lock:
            state = self._states.get(route_id)
            return len(state.samples) if state is not None else 0

    def baseline_for(self, route_id: str) -> int | None:
        with self._lock:
            state = self._states.get(route_id)
            if state is None or state.baseline_bps is None:
                return None
            return round(state.baseline_bps)

    def ema_for(self, route_id: str) -> int | None:
        with self._lock:
            state = self._states.get(route_id)
            if state is None or state.ema_bps is None:
                return None
            return round(state.ema_bps)

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self._session_digest = None
            self._session_initialized = False
            self._active_route_id = None
            self._last_active_observed_at = None
            self._unknown_context = False
            self._reset_recommendation_unlocked()

    def remove_routes(self, route_ids: Collection[str]) -> None:
        """Forget deleted or revoked routes immediately."""

        with self._lock:
            for route_id in route_ids:
                self._states.pop(route_id, None)
            if self._active_route_id in route_ids:
                self._active_route_id = None
                self._reset_recommendation_unlocked()

    def evaluate(
        self,
        snapshots: Sequence[StreamRouteSnapshot],
        *,
        now: float,
        stream_session_id: str | None,
        active_route_id: str | None = None,
        new_sample_route_ids: Collection[str] | None = None,
    ) -> RelayQualityEvaluation:
        """Evaluate one full route inventory at a caller-supplied monotonic time.

        ``snapshots`` must be the full current inventory.  Missing routes and
        snapshots whose overall state is ``revoked`` are removed from memory.
        ``stream_session_id`` is digested before being retained.
        """

        if isinstance(now, bool) or not math.isfinite(now) or now < 0:
            raise ValueError("now must be a finite non-negative monotonic time")
        if stream_session_id is not None and len(stream_session_id) > 1024:
            raise ValueError("stream_session_id is too long")

        with self._lock:
            current = self._normalize_inventory_unlocked(snapshots)
            self._reset_for_session_unlocked(stream_session_id)

            fresh_ids = (
                {snapshot.route_id for snapshot in current}
                if new_sample_route_ids is None
                else set(new_sample_route_ids)
            )
            live_routes = [
                snapshot
                for snapshot in current
                if self._source_is_live(snapshot) and self._heartbeat_is_fresh(snapshot)
            ]
            if len(live_routes) > 1:
                self._sample_all_unlocked(
                    current, now=now, active_route_id=None, new_sample_route_ids=fresh_ids
                )
                return self._ambiguous_evaluation(now)

            requested_active_id = active_route_id or self._active_route_id
            active = self._select_active_route(current, live_routes, requested_active_id)
            coherent_stopped = active is not None and self.coherent_stop(active)
            if coherent_stopped:
                self._change_active_route_unlocked(None)
                self._unknown_context = False
                active = None
            if active is not None:
                if self._heartbeat_is_fresh(active) and (
                    self._source_is_live(active) or self._runtime_running(active)
                ):
                    if active.route_id in fresh_ids:
                        self._last_active_observed_at = now
                elif (
                    self._last_active_observed_at is not None
                    and now - self._last_active_observed_at > ACTIVE_CONTEXT_RETENTION_SECONDS
                ):
                    active = None
                    self._unknown_context = True
            elif requested_active_id is not None and not coherent_stopped:
                self._unknown_context = True
            selected_id = active.route_id if active is not None else None
            self._change_active_route_unlocked(selected_id)
            self._sample_all_unlocked(
                current, now=now, active_route_id=selected_id, new_sample_route_ids=fresh_ids
            )

            if active is None:
                self._recommendation_episode_active = False
                if coherent_stopped:
                    return self._idle_evaluation(now)
                if any(
                    self._heartbeat_is_fresh(item) and self._process_failed(item)
                    for item in current
                ):
                    return self._initial_process_failure_evaluation()
                if self._unknown_context or any(
                    not self._heartbeat_is_fresh(snapshot) for snapshot in current
                ):
                    return self._unknown_evaluation(None, now)
                return self._idle_evaluation(now)

            state = self._states[active.route_id]
            if (
                not self._heartbeat_is_fresh(active)
                or (not active.available and not self._process_failed(active))
                or (
                    self._safe_source(active.source) == "UNKNOWN"
                    and not self._process_failed(active)
                )
            ):
                return self._unknown_evaluation(active, now)
            self._unknown_context = False
            new_sample = active.route_id in fresh_ids
            bitrate = self._valid_bitrate(active.input_bitrate_bps)
            if self._source_is_live(active) and bitrate is not None and bitrate > 0:
                if new_sample and (
                    state.source_lost_started_at is not None
                    or (
                        state.level == HealthLevel.BLACK
                        and state.zero_bitrate_started_at is not None
                    )
                ):
                    state.consecutive_good = 0
                    state.consecutive_yellow = 0
                    state.consecutive_red = 0
                    state.red_started_at = None
                    state.level = HealthLevel.UNKNOWN
                    state.level_started_at = now
                    self._recommendation_episode_active = False
                if new_sample:
                    state.source_lost_started_at = None
            elif not self._source_is_live(active) and state.source_lost_started_at is None:
                state.source_lost_started_at = now
            reasons, hard_level = self._sample_health_unlocked(active, state, now, new_sample)
            level = self._apply_hysteresis_unlocked(state, reasons, hard_level, now, new_sample)
            standby = self._select_standby_unlocked(current, active.route_id, now)
            return self._active_evaluation(active, state, standby, level, reasons, now)

    def _normalize_inventory_unlocked(
        self, snapshots: Sequence[StreamRouteSnapshot]
    ) -> list[StreamRouteSnapshot]:
        current: list[StreamRouteSnapshot] = []
        seen: set[str] = set()
        for snapshot in snapshots:
            if snapshot.route_id in seen:
                raise ValueError("duplicate route_id")
            seen.add(snapshot.route_id)
            if self._normalized(snapshot.overall_state) == "revoked":
                self._states.pop(snapshot.route_id, None)
                continue
            current.append(snapshot)

        retained = {snapshot.route_id for snapshot in current}
        for removed_id in self._states.keys() - retained:
            del self._states[removed_id]

        return current

    def _reset_for_session_unlocked(self, stream_session_id: str | None) -> None:
        digest = (
            hashlib.blake2s(stream_session_id.encode("utf-8")).digest()
            if stream_session_id is not None
            else None
        )
        if self._session_initialized and digest != self._session_digest:
            self._states.clear()
            self._active_route_id = None
            self._last_active_observed_at = None
            self._unknown_context = False
            self._reset_recommendation_unlocked()
        self._session_digest = digest
        self._session_initialized = True

    def _change_active_route_unlocked(self, route_id: str | None) -> None:
        if self._active_route_id is not None and route_id != self._active_route_id:
            for state in self._states.values():
                state.reset_measurements()
                state.standby_ready_since = None
            self._reset_recommendation_unlocked()
        self._active_route_id = route_id
        if route_id is None:
            self._last_active_observed_at = None

    def _reset_recommendation_unlocked(self) -> None:
        self._last_recommendation_at = None
        self._last_recommendation_target = None
        self._recommendation_episode_active = False

    def _state_for_unlocked(self, route_id: str, now: float) -> _RouteState:
        state = self._states.get(route_id)
        if state is not None:
            state.last_seen_at = now
            return state
        if len(self._states) >= MAX_TRACKED_ROUTES:
            oldest_id = min(
                self._states,
                key=lambda candidate: (self._states[candidate].last_seen_at, candidate),
            )
            del self._states[oldest_id]
        state = _RouteState(last_seen_at=now)
        self._states[route_id] = state
        return state

    def _sample_all_unlocked(
        self,
        snapshots: Sequence[StreamRouteSnapshot],
        *,
        now: float,
        active_route_id: str | None,
        new_sample_route_ids: Collection[str],
    ) -> None:
        ordered = sorted(
            snapshots,
            key=lambda snapshot: (
                snapshot.route_id != active_route_id,
                snapshot.display_name.casefold(),
                snapshot.route_id,
            ),
        )
        for snapshot in ordered[:MAX_TRACKED_ROUTES]:
            state = self._state_for_unlocked(snapshot.route_id, now)
            if snapshot.route_id != active_route_id:
                self._sample_standby_readiness_unlocked(snapshot, state, now)
            if snapshot.route_id not in new_sample_route_ids or not self._heartbeat_is_fresh(
                snapshot
            ):
                continue
            bitrate = self._valid_bitrate(snapshot.input_bitrate_bps)
            heartbeat = self._valid_non_negative(snapshot.heartbeat_age_seconds)
            state.samples.append(
                _Sample(
                    observed_at=now,
                    live=self._is_live(snapshot),
                    bitrate_bps=bitrate,
                    heartbeat_age_seconds=heartbeat,
                )
            )
            if snapshot.route_id == active_route_id:
                self._sample_active_measurements_unlocked(snapshot, state, bitrate, now)

    def _sample_active_measurements_unlocked(
        self,
        snapshot: StreamRouteSnapshot,
        state: _RouteState,
        bitrate: int | None,
        now: float,
    ) -> None:
        valid_live = (
            self._source_is_live(snapshot)
            and snapshot.available
            and bitrate is not None
            and bitrate > 0
            and self._heartbeat_is_fresh(snapshot)
            and self._normalized(snapshot.overall_state) not in _PROCESS_FAILED_STATES
        )
        if not valid_live:
            if state.offline_started_at is None:
                state.offline_started_at = now
            elif now - state.offline_started_at >= BASELINE_RESET_OFFLINE_SECONDS:
                # Expire learned media quality, not the evidence of the outage.
                # Resetting the failure clock here on every heartbeat would
                # turn a persistent zero-byte LIVE source back into UNKNOWN.
                state.baseline_candidates.clear()
                state.valid_live_samples = 0
                state.ema_bps = None
                state.baseline_bps = None
                state.previous_ema_bps = None
            return

        assert bitrate is not None
        state.offline_started_at = None
        state.valid_live_samples += 1
        state.previous_ema_bps = state.ema_bps
        if state.ema_bps is None:
            state.ema_bps = float(bitrate)
        else:
            capped = min(float(bitrate), state.ema_bps * EMA_MAX_UPWARD_SAMPLE_RATIO)
            state.ema_bps = EMA_ALPHA * capped + (1.0 - EMA_ALPHA) * state.ema_bps

        if state.valid_live_samples <= BASELINE_WARMUP_SAMPLES:
            return
        candidate = float(bitrate)
        if state.baseline_bps is not None:
            candidate = min(candidate, state.baseline_bps * BASELINE_MAX_UPWARD_SAMPLE_RATIO)
        state.baseline_candidates.append(candidate)
        if state.baseline_bps is None:
            if len(state.baseline_candidates) >= BASELINE_MIN_SAMPLES:
                state.baseline_bps = float(median(state.baseline_candidates))
            return

        if candidate >= state.baseline_bps * BITRATE_YELLOW_RATIO:
            state.baseline_bps = (
                BASELINE_ALPHA * candidate + (1.0 - BASELINE_ALPHA) * state.baseline_bps
            )

    def _sample_standby_readiness_unlocked(
        self, snapshot: StreamRouteSnapshot, state: _RouteState, now: float
    ) -> None:
        if self._standby_is_ready(snapshot):
            if state.standby_ready_since is None:
                state.standby_ready_since = now
        else:
            state.standby_ready_since = None

    def _sample_health_unlocked(
        self, snapshot: StreamRouteSnapshot, state: _RouteState, now: float, new_sample: bool
    ) -> tuple[tuple[str, ...], HealthLevel | None]:
        reasons: list[str] = []
        hard_level: HealthLevel | None = None
        heartbeat = self._valid_non_negative(snapshot.heartbeat_age_seconds)
        overall = self._normalized(snapshot.overall_state)
        youtube = self._normalized(snapshot.youtube_forward_state)
        bitrate = self._valid_bitrate(snapshot.input_bitrate_bps)

        if not snapshot.available:
            reasons.append("route_unavailable")
            hard_level = HealthLevel.BLACK
        elif heartbeat is not None and heartbeat > HEARTBEAT_DELAYED_SECONDS:
            reasons.append("heartbeat_delayed")

        if self._process_failed(snapshot):
            reasons.append("relay_process_failed")
            hard_level = HealthLevel.BLACK
        elif overall in _DEGRADED_OVERALL_STATES:
            reasons.append("relay_degraded")

        if not self._source_is_live(snapshot):
            reasons.append("source_lost")
            hard_level = HealthLevel.BLACK

        if bitrate is None or bitrate <= 0:
            reasons.append("input_bitrate_missing")
            if state.zero_bitrate_started_at is None:
                state.zero_bitrate_started_at = now
            elif now - state.zero_bitrate_started_at > MEDIA_STALLED_BLACK_SECONDS:
                reasons.append("media_stalled")
                hard_level = HealthLevel.BLACK
        else:
            state.zero_bitrate_started_at = None
            if state.baseline_bps is not None:
                ratio = bitrate / state.baseline_bps
                if ratio <= BITRATE_RED_RATIO:
                    reasons.append("input_bitrate_critical")
                elif ratio <= BITRATE_YELLOW_RATIO:
                    reasons.append("input_bitrate_low")

        if youtube in _YOUTUBE_FAILED_STATES:
            reasons.append("youtube_forward_failed")
            if hard_level != HealthLevel.BLACK:
                hard_level = HealthLevel.RED
        elif youtube in _YOUTUBE_CONNECTING_STATES:
            if state.youtube_connecting_started_at is None:
                state.youtube_connecting_started_at = now
            elif now - state.youtube_connecting_started_at >= YOUTUBE_CONNECTING_YELLOW_SECONDS:
                reasons.append("youtube_connecting")
        else:
            state.youtube_connecting_started_at = None

        cpu = self._valid_percentage(snapshot.host_cpu_percent)
        if new_sample:
            if cpu is not None and cpu >= CPU_PRESSURE_PERCENT:
                state.cpu_pressure_samples += 1
            else:
                state.cpu_pressure_samples = 0
        if state.cpu_pressure_samples >= RESOURCE_PRESSURE_MIN_SAMPLES:
            reasons.append("cpu_pressure")
            if cpu is not None and cpu >= CPU_CRITICAL_PERCENT:
                reasons.append("cpu_critical")

        memory_ratio = self._memory_available_ratio(snapshot)
        if new_sample:
            if memory_ratio is not None and memory_ratio < MEMORY_PRESSURE_RATIO:
                state.memory_pressure_samples += 1
            else:
                state.memory_pressure_samples = 0
        if state.memory_pressure_samples >= RESOURCE_PRESSURE_MIN_SAMPLES:
            reasons.append("memory_pressure")
            if memory_ratio is not None and memory_ratio < MEMORY_CRITICAL_RATIO:
                reasons.append("memory_critical")

        service_good = overall in _HEALTHY_OVERALL_STATES
        youtube_good = youtube in _YOUTUBE_ACTIVE_STATES
        if not reasons and service_good and youtube_good:
            return ("healthy",), hard_level
        if not reasons:
            reasons.append("service_initializing")
        return tuple(dict.fromkeys(reasons)), hard_level

    def _apply_hysteresis_unlocked(
        self,
        state: _RouteState,
        reasons: tuple[str, ...],
        hard_level: HealthLevel | None,
        now: float,
        new_sample: bool,
    ) -> HealthLevel:
        good = reasons == ("healthy",)
        red_candidate = any(
            reason
            in {
                "input_bitrate_critical",
                "input_bitrate_missing",
                "relay_degraded",
                "cpu_critical",
                "memory_critical",
            }
            for reason in reasons
        )
        yellow_candidate = not good

        if good and new_sample:
            state.consecutive_good += 1
            state.consecutive_yellow = 0
            state.consecutive_red = 0
            state.red_started_at = None
        elif new_sample:
            state.consecutive_good = 0
            state.consecutive_yellow = state.consecutive_yellow + 1 if yellow_candidate else 0
            if red_candidate:
                state.consecutive_red += 1
                if state.red_started_at is None:
                    state.red_started_at = now
            else:
                state.consecutive_red = 0
                state.red_started_at = None

        next_level = state.level
        if hard_level == HealthLevel.BLACK:
            next_level = HealthLevel.BLACK
        elif hard_level == HealthLevel.RED or (
            red_candidate
            and state.consecutive_red >= RED_MIN_SAMPLES
            and state.red_started_at is not None
            and now - state.red_started_at >= RED_MIN_DURATION_SECONDS
        ):
            next_level = HealthLevel.RED
        elif state.level in {HealthLevel.RED, HealthLevel.BLACK, HealthLevel.YELLOW}:
            if good and state.consecutive_good >= RECOVERY_MIN_GOOD_SAMPLES:
                next_level = HealthLevel.GREEN
        elif good and state.consecutive_good >= RECOVERY_MIN_GOOD_SAMPLES:
            next_level = HealthLevel.GREEN
        elif yellow_candidate and (
            state.consecutive_yellow >= YELLOW_MIN_SAMPLES
            or "cpu_pressure" in reasons
            or "memory_pressure" in reasons
        ):
            next_level = HealthLevel.YELLOW

        if next_level != state.level:
            state.level = next_level
            state.level_started_at = now
        elif state.level_started_at is None:
            state.level_started_at = now
        return state.level

    def _select_standby_unlocked(
        self,
        snapshots: Sequence[StreamRouteSnapshot],
        active_route_id: str,
        now: float,
    ) -> StreamRouteSnapshot | None:
        eligible: list[StreamRouteSnapshot] = []
        for snapshot in snapshots:
            if snapshot.route_id == active_route_id or not self._standby_is_ready(snapshot):
                continue
            state = self._states.get(snapshot.route_id)
            if (
                state is None
                or state.standby_ready_since is None
                or now - state.standby_ready_since < STANDBY_HEALTHY_MIN_SECONDS
            ):
                continue
            eligible.append(snapshot)
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda snapshot: (snapshot.display_name.casefold(), snapshot.route_id),
        )

    def _active_evaluation(
        self,
        active: StreamRouteSnapshot,
        state: _RouteState,
        standby: StreamRouteSnapshot | None,
        level: HealthLevel,
        reasons: tuple[str, ...],
        now: float,
    ) -> RelayQualityEvaluation:
        health = self._health_assessment(level, reasons, state, now)
        if "source_lost" in reasons and not self._process_failed(active):
            health = HealthAssessment(
                level=level,
                title="Входящее видео пропало",
                message=(
                    "Сервер передаёт заставку. Ожидаем восстановления связи"
                    if self._normalized(active.source) == "slate"
                    and self._runtime_running(active)
                    and self._normalized(active.youtube_forward_state) in _YOUTUBE_ACTIVE_STATES
                    else "Входящий источник недоступен. Ожидаем восстановления связи."
                ),
                reason_codes=health.reason_codes,
                confidence=health.confidence,
                state_duration_seconds=health.state_duration_seconds,
            )
        loss_started = state.source_lost_started_at
        if loss_started is None and "media_stalled" in reasons and not self._process_failed(active):
            loss_started = state.zero_bitrate_started_at
            health = HealthAssessment(
                level=level,
                title="Нет подтверждённого входящего видеопотока",
                message=(
                    "Свежая телеметрия не подтверждает поступление медиаданных. "
                    "Ожидаем восстановления связи."
                ),
                reason_codes=health.reason_codes,
                confidence=health.confidence,
                state_duration_seconds=health.state_duration_seconds,
            )
        in_grace = (
            loss_started is not None
            and now - loss_started < RECOVERY_GRACE_SECONDS
            and not self._process_failed(active)
        )
        if in_grace:
            recommendation = self._watch_recommendation(
                "recovery_grace", "Ожидаем автоматического восстановления связи."
            )
        elif (reasons == ("healthy",) and level != HealthLevel.GREEN) or (
            self._source_is_live(active)
            and level == HealthLevel.BLACK
            and "media_stalled" not in reasons
            and not self._process_failed(active)
        ):
            self._recommendation_episode_active = False
            recommendation = self._watch_recommendation(
                "recovering_source", "Входящий поток восстановлен. Проверяем стабильность."
            )
        else:
            recommendation = self._recommendation_unlocked(level, standby, now)
        return RelayQualityEvaluation(
            stream_state="active",
            health=health,
            current_route=self._route_view(active, state),
            standby_route=(
                self._route_view(standby, self._states[standby.route_id])
                if standby is not None
                else None
            ),
            recommendation=recommendation,
        )

    def _recommendation_unlocked(
        self,
        level: HealthLevel,
        standby: StreamRouteSnapshot | None,
        now: float,
    ) -> RouteRecommendation:
        if level not in {HealthLevel.RED, HealthLevel.BLACK}:
            if level == HealthLevel.GREEN:
                self._recommendation_episode_active = False
                action = RecommendationAction.STAY
                reason_code = "current_route_healthy"
                reason = "Текущий входящий поток стабилен."
            else:
                action = RecommendationAction.WATCH
                reason_code = "monitor_current_route"
                reason = "Продолжается наблюдение за текущим потоком."
            return RouteRecommendation(
                action=action,
                target_route_id=None,
                target_display_name=None,
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                reason=reason,
                reason_code=reason_code,
            )

        if standby is None:
            return RouteRecommendation(
                action=(
                    RecommendationAction.RECONNECT
                    if level == HealthLevel.BLACK
                    else RecommendationAction.WATCH
                ),
                target_route_id=None,
                target_display_name=None,
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                reason=(
                    "Переподключите поток в Moblin. Готового резервного сервера сейчас нет."
                    if level == HealthLevel.BLACK
                    else "Связь нестабильна. Доступного резервного сервера сейчас нет."
                ),
                reason_code="no_healthy_standby",
            )

        continuing = (
            self._recommendation_episode_active
            and self._last_recommendation_target == standby.route_id
        )
        cooldown_active = (
            not continuing
            and self._last_recommendation_at is not None
            and now - self._last_recommendation_at < RECOMMENDATION_COOLDOWN_SECONDS
        )
        if cooldown_active:
            return RouteRecommendation(
                action=RecommendationAction.WATCH,
                target_route_id=None,
                target_display_name=None,
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                reason="Продолжается наблюдение; недавняя рекомендация ещё актуальна.",
                reason_code="recommendation_cooldown",
            )

        if not continuing:
            self._recommendation_episode_active = True
            self._last_recommendation_at = now
            self._last_recommendation_target = standby.route_id
        return RouteRecommendation(
            action=RecommendationAction.SWITCH_RECOMMENDED,
            target_route_id=standby.route_id,
            target_display_name=standby.display_name,
            confidence=MeasurementConfidence.STANDBY_SERVER_READINESS,
            reason=(
                "Текущий поток нестабилен; резервный сервер исправен. "
                "Качество маршрута с телефона до резерва ещё не измерено."
            ),
            reason_code="standby_server_ready",
        )

    def _health_assessment(
        self,
        level: HealthLevel,
        reasons: tuple[str, ...],
        state: _RouteState,
        now: float,
    ) -> HealthAssessment:
        titles = {
            HealthLevel.UNKNOWN: "Состояние уточняется",
            HealthLevel.GREEN: "ЭФИР СТАБИЛЕН",
            HealthLevel.YELLOW: "СВЯЗЬ УХУДШАЕТСЯ",
            HealthLevel.RED: "ПОТОК НЕСТАБИЛЕН",
            HealthLevel.BLACK: "ПОТОК ПОТЕРЯН",
        }
        messages = {
            HealthLevel.UNKNOWN: "Недостаточно свежих данных для оценки.",
            HealthLevel.GREEN: "Входящий поток и отправка в YouTube работают.",
            HealthLevel.YELLOW: "Наблюдаем за ухудшением входящего потока.",
            HealthLevel.RED: "Ухудшение входящего потока сохраняется.",
            HealthLevel.BLACK: "Нет свежего медиапотока от текущего relay.",
        }
        started_at = state.level_started_at if state.level_started_at is not None else now
        displayed_reasons = reasons
        if reasons == ("healthy",) and level == HealthLevel.UNKNOWN:
            displayed_reasons = ("monitoring_initializing",)
        elif reasons == ("healthy",) and level in {
            HealthLevel.YELLOW,
            HealthLevel.RED,
            HealthLevel.BLACK,
        }:
            displayed_reasons = ("recovering",)
        return HealthAssessment(
            level=level,
            title="Ошибка процесса relay" if "relay_process_failed" in reasons else titles[level],
            message=(
                "Свежая телеметрия подтверждает ошибку процесса relay. Проверьте сервер."
                if "relay_process_failed" in reasons
                else messages[level]
            ),
            reason_codes=displayed_reasons,
            confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
            state_duration_seconds=max(0.0, now - started_at),
        )

    def _ambiguous_evaluation(self, now: float) -> RelayQualityEvaluation:
        return RelayQualityEvaluation(
            stream_state="ambiguous",
            health=HealthAssessment(
                level=HealthLevel.YELLOW,
                title="Обнаружено несколько активных потоков",
                message="Невозможно безопасно определить текущий маршрут.",
                reason_codes=("multiple_live_routes",),
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                state_duration_seconds=0.0,
            ),
            current_route=None,
            standby_route=None,
            recommendation=RouteRecommendation(
                action=RecommendationAction.WATCH,
                target_route_id=None,
                target_display_name=None,
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                reason="Сначала устраните неоднозначность активного маршрута.",
                reason_code="multiple_live_routes",
            ),
        )

    def _idle_evaluation(self, now: float) -> RelayQualityEvaluation:
        return RelayQualityEvaluation(
            stream_state="idle",
            health=HealthAssessment(
                level=HealthLevel.UNKNOWN,
                title="Эфир не обнаружен",
                message="Активный входящий поток сейчас не определён.",
                reason_codes=("no_active_route",),
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                state_duration_seconds=0.0,
            ),
            current_route=None,
            standby_route=None,
            recommendation=RouteRecommendation(
                action=RecommendationAction.UNAVAILABLE,
                target_route_id=None,
                target_display_name=None,
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                reason="Нет активного маршрута для оценки.",
                reason_code="no_active_route",
            ),
        )

    @staticmethod
    def _watch_recommendation(reason_code: str, reason: str) -> RouteRecommendation:
        return RouteRecommendation(
            action=RecommendationAction.WATCH,
            target_route_id=None,
            target_display_name=None,
            confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
            reason=reason,
            reason_code=reason_code,
        )

    def _unknown_evaluation(
        self, snapshot: StreamRouteSnapshot | None, now: float
    ) -> RelayQualityEvaluation:
        self._recommendation_episode_active = False
        return RelayQualityEvaluation(
            stream_state="unknown",
            health=HealthAssessment(
                level=HealthLevel.UNKNOWN,
                title="Нет свежей телеметрии",
                message=(
                    "Состояние видеопотока неизвестно. "
                    "Потеря мониторинга не означает остановку эфира."
                ),
                reason_codes=("telemetry_unavailable",),
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                state_duration_seconds=0.0,
            ),
            current_route=(
                self._route_view(snapshot, self._states[snapshot.route_id])
                if snapshot is not None
                else None
            ),
            standby_route=None,
            recommendation=self._watch_recommendation(
                "telemetry_unavailable", "Ожидаем свежих данных мониторинга."
            ),
        )

    def _route_view(self, snapshot: StreamRouteSnapshot, state: _RouteState) -> RouteQualityView:
        fresh = self._heartbeat_is_fresh(snapshot)
        return RouteQualityView(
            route_id=snapshot.route_id,
            display_name=snapshot.display_name.strip(),
            kind=snapshot.kind,
            source=self._safe_source(snapshot.source) if fresh else "UNKNOWN",
            input_bitrate_bps=self._valid_bitrate(snapshot.input_bitrate_bps) if fresh else None,
            ema_input_bitrate_bps=(round(state.ema_bps) if state.ema_bps is not None else None),
            stable_baseline_bps=(
                round(state.baseline_bps) if state.baseline_bps is not None else None
            ),
            bitrate_trend=self._trend(state),
            youtube_forward_state=(
                self._safe_youtube_state(snapshot.youtube_forward_state) if fresh else "unknown"
            ),
            overall_state=self._safe_overall_state(snapshot.overall_state),
            heartbeat_age_seconds=self._valid_non_negative(snapshot.heartbeat_age_seconds),
            host_cpu_percent=self._valid_percentage(snapshot.host_cpu_percent),
            host_memory_available_bytes=self._valid_non_negative_int(
                snapshot.host_memory_available_bytes
            ),
            host_memory_total_bytes=self._valid_non_negative_int(snapshot.host_memory_total_bytes),
        )

    def _initial_process_failure_evaluation(self) -> RelayQualityEvaluation:
        # A confirmed broken process is reportable even before observing LIVE,
        # but does not prove which route carried a broadcast before restart.
        return RelayQualityEvaluation(
            stream_state="unknown",
            health=HealthAssessment(
                level=HealthLevel.BLACK,
                title="Ошибка процесса relay",
                message="Свежая телеметрия подтверждает ошибку процесса relay. Проверьте сервер.",
                reason_codes=("relay_process_failed",),
                confidence=MeasurementConfidence.ACTIVE_PATH_MEASURED,
                state_duration_seconds=0.0,
            ),
            current_route=None,
            standby_route=None,
            recommendation=self._watch_recommendation(
                "relay_process_failed", "Проверьте процесс relay на сервере."
            ),
        )

    @staticmethod
    def _select_active_route(
        snapshots: Sequence[StreamRouteSnapshot],
        live_routes: Sequence[StreamRouteSnapshot],
        active_route_id: str | None,
    ) -> StreamRouteSnapshot | None:
        if len(live_routes) == 1:
            return live_routes[0]
        if active_route_id is None:
            return None
        return next(
            (snapshot for snapshot in snapshots if snapshot.route_id == active_route_id),
            None,
        )

    @staticmethod
    def _is_live(snapshot: StreamRouteSnapshot) -> bool:
        return snapshot.live or RelayQualityTracker._normalized(snapshot.source) == "live"

    @staticmethod
    def _source_is_live(snapshot: StreamRouteSnapshot) -> bool:
        return RelayQualityTracker._normalized(snapshot.source) == "live"

    @staticmethod
    def _heartbeat_is_fresh(snapshot: StreamRouteSnapshot) -> bool:
        age = RelayQualityTracker._valid_non_negative(snapshot.heartbeat_age_seconds)
        return age is not None and age <= HEARTBEAT_BLACK_SECONDS

    @staticmethod
    def _runtime_running(snapshot: StreamRouteSnapshot) -> bool:
        return (
            RelayQualityTracker._normalized(snapshot.service_state) == "active"
            and RelayQualityTracker._normalized(snapshot.main_process_state) == "running"
        )

    @staticmethod
    def _process_failed(snapshot: StreamRouteSnapshot) -> bool:
        return (
            RelayQualityTracker._normalized(snapshot.service_state) == "failed"
            or RelayQualityTracker._normalized(snapshot.main_process_state) == "failed"
            or RelayQualityTracker._normalized(snapshot.overall_state)
            in {"process_failed", "service_failed", "stopped_unexpectedly"}
        )

    @staticmethod
    def coherent_stop(snapshot: StreamRouteSnapshot) -> bool:
        """NONE alone, unavailable metrics, and process failures are not a stop."""
        return (
            RelayQualityTracker._heartbeat_is_fresh(snapshot)
            and RelayQualityTracker._normalized(snapshot.service_state) == "inactive"
            and RelayQualityTracker._normalized(snapshot.main_process_state) == "stopped"
            and RelayQualityTracker._normalized(snapshot.srt_listener_state) == "closed"
            and RelayQualityTracker._normalized(snapshot.source) == "none"
            and RelayQualityTracker._normalized(snapshot.youtube_forward_state)
            in {"inactive", "stopped", "disabled"}
            and snapshot.error_code is None
            and not RelayQualityTracker._process_failed(snapshot)
        )

    @staticmethod
    def _standby_is_ready(snapshot: StreamRouteSnapshot) -> bool:
        heartbeat = RelayQualityTracker._valid_non_negative(snapshot.heartbeat_age_seconds)
        cpu = RelayQualityTracker._valid_percentage(snapshot.host_cpu_percent)
        memory_ratio = RelayQualityTracker._memory_available_ratio(snapshot)
        return (
            snapshot.available
            and not RelayQualityTracker._is_live(snapshot)
            and snapshot.ready
            and snapshot.recommendation_eligible
            and snapshot.portrait_profile_valid
            and snapshot.youtube_configured
            and snapshot.pending_command is None
            and snapshot.error_code is None
            and heartbeat is not None
            and heartbeat <= STANDBY_FRESH_HEARTBEAT_SECONDS
            and RelayQualityTracker._normalized(snapshot.overall_state) in _STANDBY_OVERALL_STATES
            and (cpu is None or cpu < CPU_PRESSURE_PERCENT)
            and (memory_ratio is None or memory_ratio >= MEMORY_PRESSURE_RATIO)
        )

    @staticmethod
    def _trend(state: _RouteState) -> BitrateTrend:
        if state.ema_bps is None or state.previous_ema_bps is None:
            return "unknown"
        if state.previous_ema_bps <= 0:
            return "stable"
        ratio = state.ema_bps / state.previous_ema_bps
        if ratio > 1.05:
            return "rising"
        if ratio < 0.95:
            return "falling"
        return "stable"

    @staticmethod
    def _memory_available_ratio(snapshot: StreamRouteSnapshot) -> float | None:
        available = RelayQualityTracker._valid_non_negative_int(
            snapshot.host_memory_available_bytes
        )
        total = RelayQualityTracker._valid_non_negative_int(snapshot.host_memory_total_bytes)
        if available is None or total is None or total <= 0 or available > total:
            return None
        return available / total

    @staticmethod
    def _valid_bitrate(value: int | None) -> int | None:
        if isinstance(value, bool) or value is None or value < 0:
            return None
        return value

    @staticmethod
    def _valid_non_negative(value: float | None) -> float | None:
        if isinstance(value, bool) or value is None or not math.isfinite(value) or value < 0:
            return None
        return float(value)

    @staticmethod
    def _valid_non_negative_int(value: int | None) -> int | None:
        if isinstance(value, bool) or value is None or value < 0:
            return None
        return value

    @staticmethod
    def _valid_percentage(value: float | None) -> float | None:
        valid = RelayQualityTracker._valid_non_negative(value)
        if valid is None or valid > 100:
            return None
        return valid

    @staticmethod
    def _normalized(value: str) -> str:
        return value.strip().casefold().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _safe_source(value: str) -> str:
        normalized = RelayQualityTracker._normalized(value)
        if normalized in {"live", "slate", "none", "offline"}:
            return normalized.upper()
        return "UNKNOWN"

    @staticmethod
    def _safe_youtube_state(value: str) -> str:
        normalized = RelayQualityTracker._normalized(value)
        allowed = _YOUTUBE_ACTIVE_STATES | _YOUTUBE_CONNECTING_STATES | _YOUTUBE_FAILED_STATES
        allowed |= {"inactive", "stopped", "disabled", "unknown"}
        return normalized if normalized in allowed else "unknown"

    @staticmethod
    def _safe_overall_state(value: str) -> str:
        normalized = RelayQualityTracker._normalized(value)
        allowed = (
            _HEALTHY_OVERALL_STATES
            | _DEGRADED_OVERALL_STATES
            | _PROCESS_FAILED_STATES
            | _STANDBY_OVERALL_STATES
        )
        allowed |= {"unknown", "initializing"}
        return normalized if normalized in allowed else "unknown"


__all__ = [
    "BASELINE_MIN_SAMPLES",
    "BASELINE_RESET_OFFLINE_SECONDS",
    "BASELINE_WARMUP_SAMPLES",
    "BITRATE_RED_RATIO",
    "BITRATE_YELLOW_RATIO",
    "CPU_PRESSURE_PERCENT",
    "HEARTBEAT_BLACK_SECONDS",
    "HealthAssessment",
    "HealthLevel",
    "MAX_SAMPLES_PER_ROUTE",
    "MAX_TRACKED_ROUTES",
    "MeasurementConfidence",
    "RECOVERY_MIN_GOOD_SAMPLES",
    "RECOMMENDATION_COOLDOWN_SECONDS",
    "RED_MIN_DURATION_SECONDS",
    "RED_MIN_SAMPLES",
    "RESOURCE_PRESSURE_MIN_SAMPLES",
    "RecommendationAction",
    "RelayQualityEvaluation",
    "RelayQualityTracker",
    "RouteQualityView",
    "RouteRecommendation",
    "STANDBY_HEALTHY_MIN_SECONDS",
    "StreamRouteSnapshot",
    "YELLOW_MIN_SAMPLES",
]
