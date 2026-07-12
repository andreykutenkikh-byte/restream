from __future__ import annotations

import pytest

from app.core.backoff import BackoffExhausted, ExponentialBackoff, calculate_backoff


def test_calculate_backoff_is_exponential_and_capped() -> None:
    delays = [
        calculate_backoff(attempt, initial_delay=1, max_delay=5, multiplier=2)
        for attempt in range(6)
    ]

    assert delays == [1, 2, 4, 5, 5, 5]


def test_calculate_backoff_handles_huge_attempt_without_overflow() -> None:
    assert calculate_backoff(10**9, max_delay=30) == 30


def test_stateful_policy_counts_failures_and_exhausts() -> None:
    policy = ExponentialBackoff(
        initial_delay=0.5,
        max_delay=2,
        max_attempts=3,
    )

    assert [policy.next_delay() for _ in range(3)] == [0.5, 1, 2]
    assert policy.attempts == 3
    assert policy.remaining_attempts == 0
    assert policy.exhausted
    with pytest.raises(BackoffExhausted):
        policy.next_delay()


def test_only_stable_success_resets_backoff() -> None:
    policy = ExponentialBackoff(stable_reset_after=10)
    policy.next_delay()
    policy.next_delay()

    assert not policy.record_success(9.99)
    assert policy.attempts == 2
    assert policy.record_success(10)
    assert policy.attempts == 0
    assert policy.next_delay() == 1


def test_manual_reset_recovers_an_exhausted_policy() -> None:
    policy = ExponentialBackoff(max_attempts=1)
    policy.next_delay()
    assert policy.exhausted

    policy.reset()

    assert not policy.exhausted
    assert policy.next_delay() == 1


@pytest.mark.parametrize(
    ("sample", "expected"),
    [(0.0, 5.0), (0.5, 10.0), (1.0, 15.0)],
)
def test_jitter_is_deterministic_when_random_source_is_injected(
    sample: float, expected: float
) -> None:
    policy = ExponentialBackoff(
        initial_delay=10,
        max_delay=20,
        max_attempts=None,
        jitter_ratio=0.5,
        random_source=lambda: sample,
    )

    assert policy.next_delay() == expected


def test_jitter_never_exceeds_maximum_delay() -> None:
    policy = ExponentialBackoff(
        initial_delay=10,
        max_delay=10,
        max_attempts=None,
        jitter_ratio=1,
        random_source=lambda: 1.0,
    )

    assert policy.next_delay() == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_delay": 0},
        {"initial_delay": 2, "max_delay": 1},
        {"multiplier": 1},
        {"max_attempts": 0},
        {"stable_reset_after": -1},
        {"jitter_ratio": 1.1},
    ],
)
def test_invalid_policy_configuration_is_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ExponentialBackoff(**kwargs)  # type: ignore[arg-type]


def test_negative_connection_duration_is_rejected() -> None:
    with pytest.raises(ValueError):
        ExponentialBackoff().record_success(-1)
