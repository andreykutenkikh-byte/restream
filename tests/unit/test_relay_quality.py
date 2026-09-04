from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.services.relay_quality import (
    BASELINE_MIN_SAMPLES,
    BASELINE_RESET_OFFLINE_SECONDS,
    BASELINE_WARMUP_SAMPLES,
    HEARTBEAT_BLACK_SECONDS,
    MAX_SAMPLES_PER_ROUTE,
    MAX_TRACKED_ROUTES,
    RECOMMENDATION_COOLDOWN_SECONDS,
    STANDBY_HEALTHY_MIN_SECONDS,
    HealthLevel,
    MeasurementConfidence,
    RecommendationAction,
    RelayQualityEvaluation,
    RelayQualityTracker,
    StreamRouteSnapshot,
)

BITRATE = 4_000_000
MEMORY_TOTAL = 4 * 1024**3


def route(
    route_id: str = "hong_kong",
    display_name: str = "Hong Kong",
    *,
    live: bool = True,
    source: str | None = None,
    bitrate: int | None = BITRATE,
    youtube: str = "active",
    overall: str = "healthy",
    heartbeat: float | None = 1.0,
    available: bool = True,
    ready: bool = True,
    cpu: float | None = 20.0,
    memory_available: int | None = 3 * 1024**3,
    error_code: str | None = None,
    pending_command: str | None = None,
    eligible: bool = True,
    portrait: bool = True,
    youtube_configured: bool = True,
    kind: str = "relay",
) -> StreamRouteSnapshot:
    return StreamRouteSnapshot(
        route_id=route_id,
        display_name=display_name,
        kind=kind,  # type: ignore[arg-type]
        available=available,
        live=live,
        ready=ready,
        source=source if source is not None else ("LIVE" if live else "NONE"),
        input_bitrate_bps=bitrate,
        youtube_forward_state=youtube,
        overall_state=overall,
        heartbeat_age_seconds=heartbeat,
        host_cpu_percent=cpu,
        host_memory_available_bytes=memory_available,
        host_memory_total_bytes=MEMORY_TOTAL,
        error_code=error_code,
        pending_command=pending_command,
        recommendation_eligible=eligible,
        portrait_profile_valid=portrait,
        youtube_configured=youtube_configured,
    )


def standby(
    route_id: str = "tokyo",
    display_name: str = "Tokyo",
    **changes: object,
) -> StreamRouteSnapshot:
    snapshot = route(
        route_id, display_name, live=False, bitrate=0, youtube="stopped", overall="ready"
    )
    return replace(snapshot, **changes)


def evaluate(
    tracker: RelayQualityTracker,
    active: StreamRouteSnapshot,
    now: float,
    *others: StreamRouteSnapshot,
    session: str | None = "session-a",
    active_route_id: str | None = None,
) -> RelayQualityEvaluation:
    return tracker.evaluate(
        [active, *others],
        now=now,
        stream_session_id=session,
        active_route_id=active_route_id,
    )


def warm_stable(
    tracker: RelayQualityTracker,
    *,
    start: float = 0.0,
    active: StreamRouteSnapshot | None = None,
    others: tuple[StreamRouteSnapshot, ...] = (),
    session: str | None = "session-a",
) -> float:
    current = active or route()
    samples = BASELINE_WARMUP_SAMPLES + BASELINE_MIN_SAMPLES
    now = start
    for index in range(samples):
        now = start + index * 2.0
        evaluate(tracker, current, now, *others, session=session)
    return now


def test_initial_state_is_unknown_and_three_good_samples_become_green() -> None:
    tracker = RelayQualityTracker()

    first = evaluate(tracker, route(), 0)
    second = evaluate(tracker, route(), 2)
    third = evaluate(tracker, route(), 4)

    assert first.health.level == HealthLevel.UNKNOWN
    assert first.health.reason_codes == ("monitoring_initializing",)
    assert second.health.level == HealthLevel.UNKNOWN
    assert third.health.level == HealthLevel.GREEN
    assert third.recommendation.action == RecommendationAction.STAY


def test_baseline_skips_first_two_and_requires_six_more_valid_live_samples() -> None:
    tracker = RelayQualityTracker()
    required = BASELINE_WARMUP_SAMPLES + BASELINE_MIN_SAMPLES

    for index in range(required - 1):
        evaluate(tracker, route(bitrate=BITRATE), float(index))
        assert tracker.baseline_for("hong_kong") is None

    evaluate(tracker, route(bitrate=BITRATE), float(required - 1))
    assert tracker.baseline_for("hong_kong") == BITRATE


def test_invalid_and_non_live_samples_do_not_build_baseline() -> None:
    tracker = RelayQualityTracker()
    invalid_samples = (
        route(bitrate=None),
        route(bitrate=-1),
        route(live=False, bitrate=BITRATE),
        route(source="SLATE", bitrate=BITRATE),
        route(available=False),
        route(heartbeat=HEARTBEAT_BLACK_SECONDS + 1),
    )

    for index, snapshot in enumerate(invalid_samples):
        evaluate(tracker, snapshot, float(index), active_route_id="hong_kong")

    assert tracker.baseline_for("hong_kong") is None


def test_single_spike_does_not_raise_baseline_or_ema_to_spike() -> None:
    tracker = RelayQualityTracker()
    values = [BITRATE, BITRATE, BITRATE, BITRATE, BITRATE * 10, BITRATE, BITRATE, BITRATE]

    for index, bitrate in enumerate(values):
        evaluate(tracker, route(bitrate=bitrate), float(index))

    baseline = tracker.baseline_for("hong_kong")
    ema = tracker.ema_for("hong_kong")
    assert baseline == BITRATE
    assert ema is not None and ema < BITRATE * 1.5


def test_one_bitrate_dip_does_not_change_an_established_green_state() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)

    result = evaluate(tracker, route(bitrate=2_800_000), now + 2)

    assert result.health.level == HealthLevel.GREEN
    assert "input_bitrate_low" in result.health.reason_codes
    assert result.recommendation.action == RecommendationAction.STAY


def test_sustained_twenty_five_percent_drop_becomes_yellow() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)

    evaluate(tracker, route(bitrate=2_800_000), now + 2)
    result = evaluate(tracker, route(bitrate=2_800_000), now + 4)

    assert result.health.level == HealthLevel.YELLOW
    assert result.recommendation.action == RecommendationAction.WATCH


def test_fifty_percent_drop_needs_samples_and_eight_seconds_before_red() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)

    first = evaluate(tracker, route(bitrate=2_000_000), now + 2)
    second = evaluate(tracker, route(bitrate=2_000_000), now + 6)
    third = evaluate(tracker, route(bitrate=2_000_000), now + 10)

    assert first.health.level == HealthLevel.GREEN
    assert second.health.level == HealthLevel.YELLOW
    assert third.health.level == HealthLevel.RED


def test_stale_heartbeat_is_immediately_black() -> None:
    tracker = RelayQualityTracker()
    warm_stable(tracker)

    result = evaluate(tracker, route(heartbeat=HEARTBEAT_BLACK_SECONDS + 0.1), 40)

    assert result.health.level == HealthLevel.BLACK
    assert "heartbeat_lost" in result.health.reason_codes


def test_youtube_failure_is_red_without_claiming_route_loss() -> None:
    tracker = RelayQualityTracker()

    result = evaluate(tracker, route(youtube="failed"), 0)

    assert result.health.level == HealthLevel.RED
    assert result.health.reason_codes == ("youtube_forward_failed",)


def test_cpu_and_memory_pressure_each_require_multiple_samples() -> None:
    for pressured in (
        route(cpu=90),
        route(memory_available=int(MEMORY_TOTAL * 0.10)),
    ):
        tracker = RelayQualityTracker()
        now = warm_stable(tracker)

        first = evaluate(tracker, pressured, now + 2)
        second = evaluate(tracker, pressured, now + 4)

        assert first.health.level == HealthLevel.GREEN
        assert second.health.level == HealthLevel.YELLOW


def test_recovery_from_bad_state_requires_three_consecutive_good_samples() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)
    bad = route(youtube="failed")
    assert evaluate(tracker, bad, now + 2).health.level == HealthLevel.RED

    first = evaluate(tracker, route(), now + 4)
    second = evaluate(tracker, route(), now + 6)
    third = evaluate(tracker, route(), now + 8)

    assert first.health.level == HealthLevel.RED
    assert first.health.reason_codes == ("recovering",)
    assert second.health.level == HealthLevel.RED
    assert third.health.level == HealthLevel.GREEN


def test_new_session_resets_baseline_and_health() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker, session="old-session")
    assert tracker.baseline_for("hong_kong") == BITRATE

    result = evaluate(tracker, route(), now + 2, session="new-session")

    assert tracker.baseline_for("hong_kong") is None
    assert result.health.level == HealthLevel.UNKNOWN


def test_changing_from_anonymous_to_identified_session_resets_baseline() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker, session=None)

    evaluate(tracker, route(), now + 2, session="identified-session")

    assert tracker.baseline_for("hong_kong") is None


def test_active_route_change_resets_measurements() -> None:
    tracker = RelayQualityTracker()
    tokyo_idle = standby()
    now = warm_stable(tracker, others=(tokyo_idle,))
    tokyo_live = route("tokyo", "Tokyo")
    hong_kong_idle = standby("hong_kong", "Hong Kong")

    result = evaluate(tracker, tokyo_live, now + 2, hong_kong_idle)

    assert result.current_route is not None
    assert result.current_route.route_id == "tokyo"
    assert tracker.baseline_for("tokyo") is None
    assert result.health.level == HealthLevel.UNKNOWN


def test_long_offline_period_resets_baseline_and_source_loss_is_black() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)
    lost = route(live=False, source="NONE", bitrate=0)

    first = evaluate(tracker, lost, now + 2, active_route_id="hong_kong")
    later = evaluate(
        tracker,
        lost,
        now + 3 + BASELINE_RESET_OFFLINE_SECONDS,
        active_route_id="hong_kong",
    )

    assert first.health.level == HealthLevel.BLACK
    assert later.health.level == HealthLevel.BLACK
    assert tracker.baseline_for("hong_kong") is None


def test_tracker_remembers_previous_active_route_when_source_disappears() -> None:
    tracker = RelayQualityTracker()
    evaluate(tracker, route(), 0)

    result = evaluate(tracker, route(live=False, source="SLATE", bitrate=0), 2)

    assert result.health.level == HealthLevel.BLACK
    assert "source_lost" in result.health.reason_codes


def test_missing_media_progress_escalates_from_red_to_black() -> None:
    tracker = RelayQualityTracker()
    now = warm_stable(tracker)
    no_media = route(bitrate=0)

    evaluate(tracker, no_media, now + 2)
    evaluate(tracker, no_media, now + 10)
    red = evaluate(tracker, no_media, now + 18)
    black = evaluate(tracker, no_media, now + 34)

    assert red.health.level == HealthLevel.RED
    assert black.health.level == HealthLevel.BLACK
    assert "media_stalled" in black.health.reason_codes


def test_sample_history_and_route_state_are_bounded() -> None:
    tracker = RelayQualityTracker()
    for index in range(MAX_SAMPLES_PER_ROUTE + 25):
        evaluate(tracker, route(), float(index))
    assert tracker.route_sample_count("hong_kong") == MAX_SAMPLES_PER_ROUTE

    many_routes = [standby(f"route_{index}", f"Relay {index:02d}") for index in range(50)]
    tracker.evaluate(many_routes, now=200, stream_session_id="session-a")
    assert tracker.tracked_route_count == MAX_TRACKED_ROUTES


def test_missing_revoked_and_explicitly_removed_routes_are_cleaned_up() -> None:
    tracker = RelayQualityTracker()
    evaluate(tracker, route(), 0, standby())
    assert tracker.tracked_route_count == 2

    evaluate(tracker, route(), 2)
    assert tracker.tracked_route_count == 1

    revoked = replace(route(), overall_state="revoked")
    tracker.evaluate([revoked], now=4, stream_session_id="session-a")
    assert tracker.tracked_route_count == 0

    evaluate(tracker, route(), 6)
    tracker.remove_routes({"hong_kong"})
    assert tracker.tracked_route_count == 0


def test_multiple_live_routes_are_ambiguous_and_never_recommended() -> None:
    tracker = RelayQualityTracker()
    result = tracker.evaluate(
        [route(), route("tokyo", "Tokyo")],
        now=0,
        stream_session_id="session-a",
    )

    assert result.stream_state == "ambiguous"
    assert result.health.reason_codes == ("multiple_live_routes",)
    assert result.current_route is None
    assert result.recommendation.action == RecommendationAction.WATCH
    assert result.recommendation.target_route_id is None


def test_standby_must_be_healthy_for_ten_seconds_and_selection_is_deterministic() -> None:
    tracker = RelayQualityTracker()
    active = route(youtube="failed")
    zulu = standby("zulu", "Zulu")
    alpha = standby("alpha", "Alpha")

    first = evaluate(tracker, active, 0, zulu, alpha)
    ready = evaluate(tracker, active, STANDBY_HEALTHY_MIN_SECONDS, zulu, alpha)

    assert first.recommendation.target_route_id is None
    assert ready.recommendation.action == RecommendationAction.SWITCH_RECOMMENDED
    assert ready.recommendation.target_route_id == "alpha"
    assert ready.recommendation.confidence == MeasurementConfidence.STANDBY_SERVER_READINESS
    assert ready.recommendation.route_to_target_measured is False


@pytest.mark.parametrize(
    "candidate",
    [
        standby(available=False),
        standby(heartbeat_age_seconds=11),
        standby(overall_state="failed"),
        standby(portrait_profile_valid=False),
        standby(youtube_configured=False),
        standby(pending_command="start"),
        standby(error_code="relay_failed"),
        standby(host_cpu_percent=90),
        standby(host_memory_available_bytes=int(MEMORY_TOTAL * 0.10)),
        standby(recommendation_eligible=False),
        replace(standby(), overall_state="revoked"),
    ],
)
def test_unready_or_revoked_standby_is_never_recommended(
    candidate: StreamRouteSnapshot,
) -> None:
    tracker = RelayQualityTracker()

    evaluate(tracker, route(youtube="failed"), 0, candidate)
    result = evaluate(
        tracker,
        route(youtube="failed"),
        STANDBY_HEALTHY_MIN_SECONDS + 1,
        candidate,
    )

    assert result.recommendation.target_route_id is None
    assert result.recommendation.reason_code == "no_healthy_standby"


def test_no_standby_never_invents_a_target() -> None:
    tracker = RelayQualityTracker()
    result = evaluate(tracker, route(youtube="failed"), 0)

    assert result.recommendation.action == RecommendationAction.WATCH
    assert result.recommendation.target_route_id is None
    assert result.recommendation.target_display_name is None


def test_recommendation_cooldown_prevents_a_new_flapping_recommendation() -> None:
    tracker = RelayQualityTracker()
    backup = standby()
    evaluate(tracker, route(), 0, backup)
    first = evaluate(tracker, route(youtube="failed"), 10, backup)
    assert first.recommendation.action == RecommendationAction.SWITCH_RECOMMENDED

    evaluate(tracker, route(), 12, backup)
    evaluate(tracker, route(), 14, backup)
    recovered = evaluate(tracker, route(), 16, backup)
    assert recovered.health.level == HealthLevel.GREEN

    cooldown = evaluate(tracker, route(youtube="failed"), 20, backup)
    after = evaluate(
        tracker,
        route(youtube="failed"),
        10 + RECOMMENDATION_COOLDOWN_SECONDS,
        backup,
    )

    assert cooldown.recommendation.action == RecommendationAction.WATCH
    assert cooldown.recommendation.reason_code == "recommendation_cooldown"
    assert after.recommendation.action == RecommendationAction.SWITCH_RECOMMENDED


def test_payload_uses_only_supported_confidence_and_contains_no_raw_status_data() -> None:
    tracker = RelayQualityTracker()
    snapshot = replace(
        route(),
        youtube_forward_state="rtmps://secret.example/key",
        overall_state="ssh-secret-value",
        error_code="super-secret",
        pending_command="secret command",
    )
    result = evaluate(tracker, snapshot, 0)
    payload_text = json.dumps(result.as_payload(), ensure_ascii=False)

    assert "end_to_end_measured" not in payload_text
    assert "rtmps://" not in payload_text
    assert "super-secret" not in payload_text
    assert "secret command" not in payload_text
    assert result.current_route is not None
    assert result.current_route.youtube_forward_state == "unknown"
    assert result.current_route.overall_state == "unknown"
    assert result.health.confidence == MeasurementConfidence.ACTIVE_PATH_MEASURED
    assert result.recommendation.route_to_target_measured is False


@pytest.mark.parametrize(
    ("route_id", "display_name"),
    [
        ("https://relay.example", "Tokyo"),
        ("relay", "203.0.113.42"),
        ("relay", "Server https://relay.example"),
    ],
)
def test_snapshot_rejects_network_locators(route_id: str, display_name: str) -> None:
    with pytest.raises(ValueError):
        route(route_id, display_name)


@pytest.mark.parametrize("invalid_now", [-1.0, float("nan"), float("inf"), True])
def test_invalid_clock_values_are_rejected(invalid_now: float) -> None:
    with pytest.raises(ValueError):
        RelayQualityTracker().evaluate(
            [route()],
            now=invalid_now,
            stream_session_id="session-a",
        )
