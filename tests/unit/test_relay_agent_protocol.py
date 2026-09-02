from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from relay_agent.broker import (
    BROKER_RESPONSE_RESERVE_SECONDS,
    BROKER_SERVER_ACTION_TIMEOUT_SECONDS,
    BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS,
)
from relay_agent.broker_client import BROKER_CALL_TIMEOUT_SECONDS
from relay_agent.errors import RelayAgentError
from relay_agent.models import (
    HostMetrics,
    RelayCommand,
    RelayCompletion,
    RelaySnapshot,
    YouTubeKeyConfiguration,
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


def test_live_snapshot_adds_bounded_bitrate_and_parses_old_snapshot() -> None:
    old_wire = relay_snapshot().to_json()
    assert RelaySnapshot.parse(old_wire) == relay_snapshot()

    live = replace(relay_snapshot(), source="LIVE", input_bitrate_bps=4_000_000)
    assert live.to_json()["input_bitrate_bps"] == 4_000_000
    assert RelaySnapshot.parse(live.to_json()) == live


@pytest.mark.parametrize("bitrate", [-1, True, 1_000_000_001])
def test_snapshot_rejects_invalid_or_non_live_bitrate(bitrate: object) -> None:
    with pytest.raises(RelayAgentError, match="invalid_protocol"):
        replace(relay_snapshot(), source="LIVE", input_bitrate_bps=bitrate)  # type: ignore[arg-type]
    with pytest.raises(RelayAgentError, match="invalid_protocol"):
        replace(relay_snapshot(), input_bitrate_bps=4_000_000)


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

    key_only = RelayCommand.parse(
        {
            "id": str(uuid4()),
            "action": "CONFIGURE_YOUTUBE_KEY",
            "payload": {"youtube_stream_key": sentinel},
            "lease_seconds": 30,
            "attempt_count": 1,
            "expires_at": expires,
        }
    )
    assert key_only.youtube is None
    assert key_only.youtube_key == YouTubeKeyConfiguration(sentinel)
    assert key_only.youtube_key.to_broker_payload() == {"youtube_stream_key": sentinel}
    assert sentinel not in repr(key_only)
    assert sentinel not in repr(key_only.youtube_key)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"youtube_stream_key": ""},
        {"youtube_stream_key": "x" * 257},
        {"youtube_stream_key": 123},
        {"youtube_stream_key": "bad key"},
        {"youtube_stream_key": "bad!key"},
        {
            "youtube_stream_key": "fixture-key",
            "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
        },
    ],
)
def test_key_only_command_rejects_malformed_or_expanded_payload(payload: object) -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    with pytest.raises(RelayAgentError, match="invalid_protocol"):
        RelayCommand.parse(
            {
                "id": str(uuid4()),
                "action": "CONFIGURE_YOUTUBE_KEY",
                "payload": payload,
                "lease_seconds": 30,
                "attempt_count": 1,
                "expires_at": expires,
            }
        )


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
