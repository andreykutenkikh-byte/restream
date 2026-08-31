from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from relay_agent.broker import (
    BROKER_RESPONSE_RESERVE_SECONDS,
    BROKER_SERVER_ACTION_TIMEOUT_SECONDS,
    BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS,
)
from relay_agent.broker_client import BROKER_CALL_TIMEOUT_SECONDS
from relay_agent.models import (
    HostMetrics,
    RelayCommand,
    RelayCompletion,
    RelaySnapshot,
    parse_timestamp,
    utc_timestamp,
)


def relay_snapshot() -> RelaySnapshot:
    return RelaySnapshot(
        service_state="active",
        enabled=True,
        main_process="running",
        srt_listener="listening",
        source="SLATE",
        youtube_forward="active",
        overall="healthy",
        youtube_url_configured=True,
        youtube_key_configured=True,
        healthy=True,
        portrait_profile=True,
        error_code=None,
    )


def test_safe_snapshot_has_exact_backend_contract() -> None:
    assert set(relay_snapshot().to_json()) == {
        "service_state",
        "enabled",
        "main_process",
        "srt_listener",
        "source",
        "youtube_forward",
        "overall",
        "youtube_url_configured",
        "youtube_key_configured",
        "healthy",
        "portrait_profile",
        "error_code",
    }


def test_command_contract_accepts_clear_and_wraps_configure_secret() -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    clear = RelayCommand.parse(
        {
            "id": str(uuid4()),
            "action": "CLEAR_YOUTUBE",
            "payload": {},
            "lease_seconds": 30,
            "attempt_count": 1,
            "expires_at": expires,
        }
    )
    assert clear.action == "CLEAR_YOUTUBE"

    sentinel = "YOUTUBE_KEY_WRAPPED_9bd4"
    configure = RelayCommand.parse(
        {
            "id": str(uuid4()),
            "action": "CONFIGURE_YOUTUBE",
            "payload": {
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": sentinel,
            },
            "lease_seconds": 30,
            "attempt_count": 1,
            "expires_at": expires,
        }
    )
    assert configure.youtube is not None
    assert sentinel not in repr(configure)
    assert sentinel not in repr(configure.youtube)


def test_completion_timestamp_round_trips_terminal_z_on_python_310() -> None:
    completed_at = utc_timestamp()

    assert completed_at.endswith("Z")
    assert parse_timestamp(completed_at).utcoffset() == timedelta(0)
    assert RelayCompletion("ok", completed_at, relay_snapshot()).completed_at == completed_at


def test_host_metrics_contract_is_bounded_and_safe() -> None:
    metrics = HostMetrics(1.0, 0.5, 25.0, 1024, 512, 2048, 1024)
    assert set(metrics.to_json()) == {
        "uptime_seconds",
        "load_1m",
        "cpu_percent",
        "memory_total_bytes",
        "memory_available_bytes",
        "disk_total_bytes",
        "disk_free_bytes",
    }


def test_local_broker_timeout_covers_relayctl_start_and_health_snapshot() -> None:
    assert 20 <= BROKER_CALL_TIMEOUT_SECONDS < 30
    assert (
        BROKER_SERVER_ACTION_TIMEOUT_SECONDS
        + BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS
        + BROKER_RESPONSE_RESERVE_SECONDS
        < BROKER_CALL_TIMEOUT_SECONDS
    )
