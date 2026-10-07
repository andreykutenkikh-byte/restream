from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.broadcast import media_runtime
from app.broadcast.media_runtime import Publisher


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
