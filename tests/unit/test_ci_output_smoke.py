from __future__ import annotations

import json
import time
from collections.abc import Sequence

import pytest

from scripts import ci_output_smoke


def test_process_snapshot_retries_a_transient_container_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def transient_exec(
        command: Sequence[str],
        *,
        service: str = "backend",
        **_: object,
    ) -> str:
        nonlocal calls
        calls += 1
        assert command[:2] == ("python", "-c")
        assert service == "backend"
        if calls == 1:
            raise ci_output_smoke.SmokeFailure("transient exec race")
        return json.dumps(
            {
                "publisher": None,
                "publisher_start_ticks": None,
                "publisher_exists": False,
                "publisher_alive": False,
                "ffmpeg": [],
            }
        )

    monkeypatch.setattr(ci_output_smoke, "compose_exec", transient_exec)
    monkeypatch.setattr(time, "sleep", delays.append)

    snapshot = ci_output_smoke.process_snapshot()

    assert calls == 2
    assert delays == [0.1]
    assert snapshot["ffmpeg"] == []


def test_process_snapshot_fails_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def failed_exec(*_: object, **__: object) -> str:
        nonlocal calls
        calls += 1
        raise ci_output_smoke.SmokeFailure("persistent exec failure")

    monkeypatch.setattr(ci_output_smoke, "compose_exec", failed_exec)
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(
        ci_output_smoke.SmokeFailure,
        match="ci-rtmp-publisher process inspection failed after bounded retries",
    ):
        ci_output_smoke.process_snapshot(service="ci-rtmp-publisher")

    assert calls == 3
    assert delays == [0.1, 0.1]
