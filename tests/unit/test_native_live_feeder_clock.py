from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

SELF_TEST = Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"


def run_delayed_feeder(*, wakeup_delay: float, sends: int, long_delay_at: int | None = None):
    """Execute the real feeder loop with a deterministic clock and local transport."""
    with (
        patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"), "resource": ModuleType("resource")}),
        patch.dict(os.environ, {"MOBLIN_RELAY_SELF_TEST_STAGE_FILE": ""}),
    ):
        namespace = runpy.run_path(str(SELF_TEST), run_name="_native_feeder_clock_test")
    cls = namespace["PacedMPEGTSFeeder"]
    chunk_size = namespace["LIVE_FEED_CHUNK_BYTES"]
    rate = namespace["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] / 8
    interval = chunk_size / rate
    clock = SimpleNamespace(now=0.0, waits=0)
    observed: list[tuple[float, bytes]] = []

    class Condition:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def notify_all(self):
            pass

        def wait(self, duration):
            assert duration > 0
            clock.waits += 1
            delay = 0.5 if clock.waits == long_delay_at else wakeup_delay
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
            if len(observed) == sends:
                feeder._stop_requested = True
            return len(payload)

    remux = SimpleNamespace(stdout=Pipe(), poll=lambda: 0, wait=lambda **_kwargs: 0)
    feeder = cls(["synthetic-remux"], 18999)
    feeder._condition = Condition()
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
        later[0] > earlier[0] for earlier, later in zip(observed[:-1], observed[1:], strict=True)
    )
    return observed, interval


@pytest.mark.parametrize("jitter", [0.0001, 0.001, 0.003])
def test_regular_scheduler_jitter_does_not_accumulate_media_clock_drift(jitter):
    observed, interval = run_delayed_feeder(wakeup_delay=jitter, sends=1001)
    # A late wakeup is one phase offset, not another delay added to each of
    # 1000 packet intervals. The old loop accumulated one full second here
    # with 1 ms jitter, making this 30 fps fixture run at about 27.1 fps.
    assert observed[-1][0] == pytest.approx(1000 * interval + jitter, abs=1e-9)


def test_a_missed_packet_interval_reanchors_without_a_catch_up_burst():
    observed, interval = run_delayed_feeder(wakeup_delay=0.001, sends=15, long_delay_at=5)
    assert observed[5][0] - observed[4][0] >= 0.5
    assert observed[6][0] - observed[5][0] == pytest.approx(interval + 0.001)
    # After the rebase the source resumes its encoded media rate. No bytes
    # were discarded and no datagrams are flushed at the same clock instant.
    assert observed[-1][0] - observed[6][0] == pytest.approx(8 * interval)
