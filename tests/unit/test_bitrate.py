from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db import Database
from app.runtime import ApplicationRuntime
from app.services.bitrate import IngestBitrateSampler
from app.services.mediamtx import IngestState, IngestStatus, StreamMetadata

READY_TIME = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def live_status(
    bytes_received: int | None,
    *,
    since: datetime | None = READY_TIME,
    metadata: StreamMetadata = StreamMetadata(),
) -> IngestStatus:
    return IngestStatus(
        IngestState.LIVE,
        metadata=metadata,
        since=since,
        bytes_received=bytes_received,
    )


def test_first_sample_is_unknown_and_second_uses_monotonic_delta() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)

    assert sampler.sample(stream_id="live/key", status=live_status(100)) is None
    clock.advance(2)

    assert sampler.sample(stream_id="live/key", status=live_status(1_100)) == 4_000


def test_subsequent_samples_use_ema_smoothing() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(ema_alpha=0.25, clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(1)

    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) == 8_000
    clock.advance(1)

    assert sampler.sample(stream_id="live/key", status=live_status(3_000)) == 10_000


def test_offline_state_resets_the_baseline_and_ema() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) == 8_000

    assert (
        sampler.sample(
            stream_id="live/key",
            status=IngestStatus(IngestState.OFFLINE, bytes_received=1_000),
        )
        is None
    )
    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) is None
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(2_000)) == 8_000


@pytest.mark.parametrize("invalid_counter", [None, -1, True])
def test_missing_or_invalid_counter_resets_the_sampler(invalid_counter: int | None) -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) == 8_000

    assert sampler.sample(stream_id="live/key", status=live_status(invalid_counter)) is None
    assert sampler.sample(stream_id="live/key", status=live_status(2_000)) is None


def test_changed_ingest_path_resets_the_sampler() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/old-key", status=live_status(0))
    clock.advance(1)
    assert sampler.sample(stream_id="live/old-key", status=live_status(1_000)) == 8_000

    assert sampler.sample(stream_id="live/new-key", status=live_status(1_000)) is None


def test_counter_decrease_starts_a_new_baseline() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(1_000))
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(2_000)) == 8_000
    clock.advance(1)

    assert sampler.sample(stream_id="live/key", status=live_status(100)) is None
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(1_100)) == 8_000


def test_changed_ready_time_starts_a_new_baseline() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) == 8_000
    clock.advance(1)

    restarted = READY_TIME + timedelta(minutes=1)
    assert sampler.sample(stream_id="live/key", status=live_status(2_000, since=restarted)) is None


def test_stale_interval_discards_the_old_rate() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(stale_after_seconds=10, clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(10)

    assert sampler.sample(stream_id="live/key", status=live_status(10_000)) is None
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(11_000)) == 8_000


def test_concurrent_same_instant_reads_return_one_consistent_ema() -> None:
    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    sampler.sample(stream_id="live/key", status=live_status(0))
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(1_000)) == 8_000

    status = live_status(1_000)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(sampler.sample, stream_id="live/key", status=status) for _ in range(32)
        ]

    assert {future.result() for future in futures} == {8_000}
    clock.advance(1)
    assert sampler.sample(stream_id="live/key", status=live_status(2_000)) == 8_000


@pytest.mark.asyncio
async def test_runtime_uses_only_sampled_bitrate_in_flat_and_nested_metadata(
    settings: Settings,
) -> None:
    class MutableMediaMTX:
        def __init__(self) -> None:
            self.status = live_status(
                100,
                metadata=StreamMetadata(video_codec="h264", bitrate_bps=999_999),
            )

        async def get_ingest_status(self, _: str) -> IngestStatus:
            return self.status

    clock = FakeClock()
    sampler = IngestBitrateSampler(clock=clock)
    media = MutableMediaMTX()
    database = Database(settings.database_path)
    database.migrate()
    runtime = ApplicationRuntime(
        settings,
        database,
        mediamtx=media,  # type: ignore[arg-type]
        bitrate_sampler=sampler,
    )
    runtime.initialize_ingest()

    first = await runtime.ingest_view()
    assert first["bitrate_bps"] is None
    assert first["metadata"]["bitrate_bps"] is None

    clock.advance(2)
    media.status = live_status(
        1_100,
        metadata=StreamMetadata(video_codec="h264", bitrate_bps=999_999),
    )
    second = await runtime.ingest_view()

    assert second["bitrate_bps"] == 4_000
    assert second["metadata"]["bitrate_bps"] == second["bitrate_bps"]
