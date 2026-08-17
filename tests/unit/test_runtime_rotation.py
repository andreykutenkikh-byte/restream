from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.core.config import Settings
from app.db import Database
from app.runtime import (
    MEDIAMTX_AUTH_TIMEOUT_SECONDS,
    PUBLISH_AUTH_SETTLE_MARGIN_SECONDS,
    PUBLISH_DRAIN_INTERVAL_SECONDS,
    ApplicationRuntime,
)
from app.services.mediamtx import IngestState, IngestStatus


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    async def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= PUBLISH_DRAIN_INTERVAL_SECONDS
        self.advance(seconds)


class DrainMediaMTX:
    def __init__(self, clock: FakeClock, result: Callable[[float, int], int]) -> None:
        self.clock = clock
        self.result = result
        self.calls: list[tuple[str, float]] = []

    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, path_name: str) -> int:
        self.calls.append((path_name, self.clock()))
        return self.result(self.clock(), len(self.calls))


def make_runtime(
    settings: Settings,
    clock: FakeClock,
    media: DrainMediaMTX,
) -> ApplicationRuntime:
    database = Database(settings.database_path)
    database.migrate()
    runtime = ApplicationRuntime(
        settings,
        database,
        mediamtx=media,  # type: ignore[arg-type]
        monotonic=clock,
        sleeper=clock.sleep,
    )
    runtime.initialize_ingest()
    return runtime


def authorize_current_publisher(runtime: ApplicationRuntime) -> str:
    path = runtime.ingest_path()
    assert runtime.authorize_mediamtx(
        action="publish",
        protocol="rtmp",
        path=path,
        user="",
        password="",
    )
    return path


def test_recent_publish_authorization_is_drained_through_timeout_horizon(
    settings: Settings,
) -> None:
    clock = FakeClock()
    media = DrainMediaMTX(clock, lambda _now, _call: 0)
    runtime = make_runtime(settings, clock, media)
    old_path = authorize_current_publisher(runtime)

    asyncio.run(runtime.rotate_ingest_key())

    horizon = MEDIAMTX_AUTH_TIMEOUT_SECONDS + PUBLISH_AUTH_SETTLE_MARGIN_SECONDS
    assert clock() == pytest.approx(horizon + PUBLISH_DRAIN_INTERVAL_SECONDS)
    assert len(media.calls) > 2
    assert {path for path, _ in media.calls} == {old_path}
    assert sum(observed_at >= horizon for _, observed_at in media.calls) == 2


def test_rejected_publish_auth_does_not_extend_rotation_horizon(settings: Settings) -> None:
    clock = FakeClock()
    media = DrainMediaMTX(clock, lambda _now, _call: 0)
    runtime = make_runtime(settings, clock, media)
    authorize_current_publisher(runtime)
    clock.advance(MEDIAMTX_AUTH_TIMEOUT_SECONDS + PUBLISH_AUTH_SETTLE_MARGIN_SECONDS + 5)

    assert not runtime.authorize_mediamtx(
        action="publish",
        protocol="rtmp",
        path="live/rejected-key",
        user="",
        password="",
    )
    started_at = clock()
    asyncio.run(runtime.rotate_ingest_key())

    assert clock() - started_at == pytest.approx(PUBLISH_DRAIN_INTERVAL_SECONDS)
    assert len(media.calls) == 2


def test_publisher_appearing_near_auth_horizon_is_kicked_before_success(
    settings: Settings,
) -> None:
    clock = FakeClock()
    appeared = False

    def late_publisher(now: float, _call: int) -> int:
        nonlocal appeared
        if not appeared and now >= MEDIAMTX_AUTH_TIMEOUT_SECONDS - 0.25:
            appeared = True
            return 1
        return 0

    media = DrainMediaMTX(clock, late_publisher)
    runtime = make_runtime(settings, clock, media)
    authorize_current_publisher(runtime)

    asyncio.run(runtime.rotate_ingest_key())

    assert appeared
    horizon = MEDIAMTX_AUTH_TIMEOUT_SECONDS + PUBLISH_AUTH_SETTLE_MARGIN_SECONDS
    assert sum(observed_at >= horizon for _, observed_at in media.calls) == 2


def test_persistent_old_publisher_rolls_back_ingest_key(settings: Settings) -> None:
    clock = FakeClock()
    media = DrainMediaMTX(clock, lambda _now, _call: 1)
    runtime = make_runtime(settings, clock, media)
    old_key = runtime.ingest_key()
    authorize_current_publisher(runtime)

    with pytest.raises(RuntimeError, match="publisher revocation did not become stable"):
        asyncio.run(runtime.rotate_ingest_key())

    assert runtime.ingest_key() == old_key
    assert len(media.calls) > 2


def test_unrelated_previous_authorization_uses_fast_quiet_postcondition(
    settings: Settings,
) -> None:
    clock = FakeClock()
    media = DrainMediaMTX(clock, lambda _now, _call: 0)
    runtime = make_runtime(settings, clock, media)
    runtime._last_publish_authorization = ("live/other-key", clock())

    asyncio.run(runtime.rotate_ingest_key())

    assert clock() == pytest.approx(PUBLISH_DRAIN_INTERVAL_SECONDS)
    assert len(media.calls) == 2
