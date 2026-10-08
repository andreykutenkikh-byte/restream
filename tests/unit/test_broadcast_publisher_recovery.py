from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.broadcast import media_runtime
from app.broadcast.media_runtime import MediaRuntime, Publisher


@pytest.mark.parametrize(
    "progress,feed,recover",
    [(0.0, True, True), (50.0, True, True), (99.0, True, False), (0.0, False, False)],
)
def test_live_but_stalled_publisher_uses_bounded_retry(
    monkeypatch: pytest.MonkeyPatch, progress: float, feed: bool, recover: bool
) -> None:
    monkeypatch.setattr(media_runtime.time, "monotonic", lambda: 100.0)
    process = SimpleNamespace(returncode=None, stdin=None, stdout=None)
    process.poll = lambda: process.returncode
    stopped = Mock(side_effect=lambda p: setattr(p, "returncode", -9))
    monkeypatch.setattr(media_runtime, "stop", stopped)
    publisher = Publisher.__new__(Publisher)
    publisher.diagnostics = Mock()
    publisher.exhausted_retry_seconds = None
    publisher.feed = feed
    publisher.selector = None
    publisher.process = process
    publisher.started = 1.0
    publisher.last_progress = progress
    publisher.first_progress = progress
    publisher.frames = 0 if not progress else 1000
    publisher.failures = 2
    publisher.retry_at = 0.0
    publisher.reader = None
    publisher.tick()
    if recover:
        publisher.diagnostics.assert_any_call("publisher_stalled", 30)
    if recover:
        assert stopped.call_count >= 1
        assert publisher.process is None
        assert publisher.failures == 3 and publisher.retry_at == 108.0
    else:
        stopped.assert_not_called()
        assert publisher.process is process


@pytest.mark.parametrize("retry_seconds", [None, 60.0])
def test_exhausted_reader_reconnects_slowly_but_output_publisher_stays_stopped(
    monkeypatch: pytest.MonkeyPatch, retry_seconds: float | None
) -> None:
    clock = [100.0]
    monkeypatch.setattr(media_runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media_runtime.threading, "Thread", Mock())
    monkeypatch.setattr(media_runtime, "stop", Mock())
    process = SimpleNamespace(returncode=1, stdin=None, stdout=[])
    process.poll = lambda: process.returncode
    restarted = SimpleNamespace(poll=lambda: None, stdin=None, stdout=[])
    launch = Mock(return_value=restarted)
    monkeypatch.setattr(media_runtime, "launch", launch)
    worker = Publisher.__new__(Publisher)
    worker.argv = ["ffmpeg"]
    worker.diagnostics = Mock()
    worker.exhausted_retry_seconds = retry_seconds
    worker.feed = False
    worker.selector = None
    worker.process = process
    worker.reader = None
    worker.started = 0.0
    worker.last_progress = 0.0
    worker.failures = 4
    worker.retry_at = 0.0
    worker.tick()
    assert worker.failures == 5 and worker.process is None
    launch.assert_not_called()
    clock[0] = 159.0
    worker.tick()
    launch.assert_not_called()
    clock[0] = 160.0
    worker.tick()
    if retry_seconds is None:
        launch.assert_not_called()
        return
    launch.assert_called_once()
    assert worker.process is restarted
    # Continuing outages receive one attempt per minute, without a new fast burst.
    restarted.poll = lambda: 1
    clock[0] = 161.0
    worker.tick()
    assert worker.failures == 5 and worker.retry_at == 221.0
    clock[0] = 220.0
    worker.tick()
    assert launch.call_count == 1
    clock[0] = 221.0
    worker.tick()
    assert launch.call_count == 2


def test_only_interrelay_reader_receives_slow_recovery_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = Mock()
    monkeypatch.setattr(media_runtime, "Publisher", constructor)
    runtime = MediaRuntime.__new__(MediaRuntime)
    runtime.forwarders = {}
    runtime.publishers = {}
    runtime._worker(runtime.forwarders, "route", "input", ["reader"])
    assert constructor.call_args.kwargs["exhausted_retry_seconds"] == 60
    runtime._worker(runtime.publishers, "route", "output", ["publisher"], feed=True)
    assert constructor.call_args.kwargs["exhausted_retry_seconds"] is None


def test_verified_frame_rate_change_reopens_publisher_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = Mock()
    monkeypatch.setattr(media_runtime, "Publisher", constructor)
    runtime = MediaRuntime.__new__(MediaRuntime)
    runtime.forwarders = {}
    runtime.publishers = {}
    runtime.source_switches = {}
    old = Mock(fps=30, last_progress=123.0)
    runtime.publishers["route"] = ("same-source-and-destination", old)
    runtime._worker(
        runtime.publishers,
        "route",
        "same-source-and-destination",
        ["publisher"],
        feed=True,
        fps=30,
    )
    old.tick.assert_called_once()
    old.close.assert_not_called()
    constructor.assert_not_called()
    runtime._worker(
        runtime.publishers,
        "route",
        "same-source-and-destination",
        ["publisher"],
        feed=True,
        fps=60,
    )
    old.close.assert_called_once()
    assert constructor.call_args.kwargs["fps"] == 60
