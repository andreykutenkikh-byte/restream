from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

SELF_TEST = Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"


def run_delayed_feeder(
    *,
    wakeup_delay: float,
    sends: int,
    long_delay_at: int | None = None,
    catchup_chunks: int | None = None,
    first_delay_intervals: float | None = None,
    processing_intervals: float = 0,
    pause_after: int | None = None,
    feeder_namespace: dict | None = None,
    check_burst_bound: bool = True,
):
    """Execute the real feeder loop with a deterministic clock and local transport."""
    with (
        patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"), "resource": ModuleType("resource")}),
        patch.dict(os.environ, {"MOBLIN_RELAY_SELF_TEST_STAGE_FILE": ""}),
    ):
        namespace = feeder_namespace or runpy.run_path(
            str(SELF_TEST), run_name="_native_feeder_clock_test"
        )
    cls = namespace["PacedMPEGTSFeeder"]
    chunk_size = namespace["LIVE_FEED_CHUNK_BYTES"]
    rate = namespace["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] / 8
    interval = chunk_size / rate
    clock = SimpleNamespace(now=0.0, waits=0, bursts={}, pauses=[])
    observed: list[tuple[float, bytes]] = []

    class Condition:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def notify_all(self):
            pass

        def wait(self, duration=None):
            if duration is None:
                assert feeder._paused and feeder._pause_requested
                clock.pauses.append((len(observed), clock.now))
                clock.now += 0.5
                feeder._pause_requested = False
                clock.waits += 1
                return
            assert duration > 0
            clock.waits += 1
            delay = 0.5 if clock.waits == long_delay_at else wakeup_delay
            if clock.waits == 1 and first_delay_intervals is not None:
                delay = first_delay_intervals * interval
            clock.now += duration + delay

    class Pipe:
        def read(self, size):
            assert size == chunk_size
            return bytes([len(observed) % 251]) * size

        def close(self):
            pass

    class Sender:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def sendto(self, payload, destination):
            assert destination == ("127.0.0.1", 18999)
            observed.append((clock.now, bytes(payload)))
            clock.bursts[clock.waits] = clock.bursts.get(clock.waits, 0) + 1
            clock.now += processing_intervals * interval
            if len(observed) == pause_after:
                feeder._pause_requested = True
            if len(observed) == sends:
                feeder._stop_requested = True
            return len(payload)

    remux = SimpleNamespace(stdout=Pipe(), poll=lambda: 0, wait=lambda **_kwargs: 0)
    feeder = cls(["synthetic-remux"], 18999)
    feeder._condition = Condition()
    if catchup_chunks is not None:
        feeder._catchup_chunks = catchup_chunks
    with patch.dict(
        cls.run.__globals__,
        {
            "time": SimpleNamespace(monotonic=lambda: clock.now),
            "socket": SimpleNamespace(AF_INET=2, SOCK_DGRAM=2, socket=lambda *_args: Sender()),
            "subprocess": SimpleNamespace(
                Popen=lambda *_args, **_kwargs: remux,
                DEVNULL=-1,
                PIPE=-1,
                TimeoutExpired=TimeoutError,
            ),
        },
    ):
        feeder.run()
    assert feeder.failure_kind is None
    assert len(observed) == sends
    assert [payload[0] for _, payload in observed] == [index % 251 for index in range(sends)]
    assert all(len(payload) == chunk_size for _, payload in observed)
    assert all(
        later[0] >= earlier[0] for earlier, later in zip(observed[:-1], observed[1:], strict=True)
    )
    if check_burst_bound:
        assert max(clock.bursts.values()) <= feeder._catchup_chunks
    return observed, interval, clock


@pytest.mark.parametrize("jitter", [0.0001, 0.001, 0.003])
def test_regular_scheduler_jitter_does_not_accumulate_media_clock_drift(jitter):
    observed, interval, _clock = run_delayed_feeder(wakeup_delay=jitter, sends=1001)
    # A late wakeup is one phase offset, not another delay added to each of
    # 1000 packet intervals. The old loop accumulated one full second here
    # with 1 ms jitter, making this 30 fps fixture run at about 27.1 fps.
    assert observed[-1][0] == pytest.approx(1000 * interval + jitter, abs=1e-9)


def test_a_missed_packet_interval_reanchors_without_a_catch_up_burst():
    observed, interval, clock = run_delayed_feeder(wakeup_delay=0.001, sends=15, long_delay_at=5)
    assert observed[5][0] - observed[4][0] >= 0.5
    assert observed[6][0] - observed[5][0] == pytest.approx(interval + 0.001)
    # After the rebase the source resumes its encoded media rate. No bytes
    # were discarded and no datagrams are flushed at the same clock instant.
    assert observed[-1][0] - observed[6][0] == pytest.approx(8 * interval)
    assert max(clock.bursts.values()) == 1


@pytest.mark.parametrize("jitter", [0.010, 0.015])
def test_one_frame_credit_repairs_the_demonstrated_single_interval_scheduling_cliff(jitter):
    current, interval, _clock = run_delayed_feeder(
        wakeup_delay=jitter, sends=1001, catchup_chunks=1
    )
    fixed, _, clock = run_delayed_feeder(wakeup_delay=jitter, sends=1001)
    media_seconds = 1000 * interval
    assert media_seconds / current[-1][0] < 0.5
    assert 0.995 <= media_seconds / fixed[-1][0] <= 1.0
    assert max(clock.bursts.values()) in {2, 3}
    assert 3 * interval <= 1 / 30 < 4 * interval


@pytest.mark.parametrize("delay_intervals", [2.999, 3.0, 3.001, 50.0])
def test_catchup_credit_below_at_and_above_cap(delay_intervals):
    observed, interval, clock = run_delayed_feeder(
        wakeup_delay=0.001, sends=15, first_delay_intervals=delay_intervals
    )
    if delay_intervals < 3:
        assert clock.bursts[1] == 3
        assert observed[1][0] == observed[2][0] == observed[3][0]
    else:
        assert clock.bursts[1] == 1
        assert observed[2][0] - observed[1][0] == pytest.approx(interval + 0.001)


def test_processing_between_sends_cannot_turn_three_packet_credit_into_four_packet_burst():
    _observed, _interval, clock = run_delayed_feeder(
        wakeup_delay=0,
        sends=20,
        first_delay_intervals=2.9,
        processing_intervals=0.1,
    )
    # A phase-debt-only cap emits four packets in this same scheduling turn.
    # Actual send accounting must force a wait after the third one.
    assert clock.bursts[1] == 3


def test_pause_discards_all_catchup_credit_without_losing_or_replaying_bytes():
    observed, interval, clock = run_delayed_feeder(wakeup_delay=0.010, sends=20, pause_after=3)
    assert clock.pauses == [(3, observed[2][0])]
    assert observed[3][0] - observed[2][0] == pytest.approx(0.5)
    assert observed[4][0] - observed[3][0] == pytest.approx(interval + 0.010)
