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


def test_accepted_publisher_uses_one_clean_independent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ci_output_smoke.PublisherIdentity(101, 1001)
    second = ci_output_smoke.PublisherIdentity(202, 2002)
    launched = iter((first, second))
    wait_calls = 0
    stopped: list[bool] = []
    removed: list[bool] = []
    delays: list[float] = []

    monkeypatch.setattr(ci_output_smoke, "launch_publisher", lambda _key: next(launched))
    monkeypatch.setattr(ci_output_smoke, "stop_publisher", lambda: stopped.append(True))
    monkeypatch.setattr(ci_output_smoke, "publisher_log_category", lambda: "resource_limit")
    monkeypatch.setattr(
        ci_output_smoke, "remove_test_files", lambda **_kwargs: removed.append(True)
    )
    monkeypatch.setattr(time, "sleep", delays.append)

    def fake_wait_for(*_: object, **__: object) -> object:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise ci_output_smoke.SmokeFailure("first publisher exited")
        return {}

    monkeypatch.setattr(ci_output_smoke, "wait_for", fake_wait_for)
    seen: set[ci_output_smoke.PublisherIdentity] = set()

    accepted = ci_output_smoke.launch_accepted_publisher(
        "synthetic-key",
        description="replacement publisher",
        seen_identities=seen,
    )

    assert accepted == second
    assert seen == {first, second}
    assert wait_calls == 3  # first attempt, its cleanup, second attempt
    assert stopped == [True]
    assert removed == [True]
    assert delays == [1]


def test_accepted_publisher_fails_after_two_clean_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(
        (
            ci_output_smoke.PublisherIdentity(101, 1001),
            ci_output_smoke.PublisherIdentity(202, 2002),
        )
    )
    attempt_waits = 0

    monkeypatch.setattr(ci_output_smoke, "launch_publisher", lambda _key: next(identities))
    monkeypatch.setattr(ci_output_smoke, "stop_publisher", lambda: None)
    monkeypatch.setattr(ci_output_smoke, "publisher_log_category", lambda: "resource_limit")
    monkeypatch.setattr(ci_output_smoke, "remove_test_files", lambda **_kwargs: None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def failed_attempt_or_cleanup(*_: object, **__: object) -> object:
        nonlocal attempt_waits
        attempt_waits += 1
        if attempt_waits in {1, 3}:
            raise ci_output_smoke.SmokeFailure("publisher exited")
        return {}

    monkeypatch.setattr(ci_output_smoke, "wait_for", failed_attempt_or_cleanup)

    with pytest.raises(
        ci_output_smoke.SmokeFailure,
        match=(
            "replacement publisher failed after two independent attempts "
            r"\(categories=resource_limit,resource_limit\)"
        ),
    ):
        ci_output_smoke.launch_accepted_publisher(
            "synthetic-key",
            description="replacement publisher",
            seen_identities=set(),
        )

    assert attempt_waits == 4


def test_publisher_encoder_threads_are_bounded_by_policy() -> None:
    source = ci_output_smoke.launch_publisher.__code__.co_consts
    script = next(value for value in source if isinstance(value, str) and "exec ffmpeg" in value)

    assert "-filter_threads 1" in script
    assert "-filter_complex_threads 1" in script
    assert "-threads:v 1" in script
    assert "-threads:a 1" in script
