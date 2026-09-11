"""Sending-clock evidence is an optional bounded projection, not media proof."""

from copy import deepcopy

import pytest
from test_native_reader_nal_diagnostics import media

from scripts.ci_node_onboarding_smoke import (
    _safe_failure_media,
    _safe_source_clock,
    safe_strict_sink_reader_timings,
)


def clock():
    return {
        "state": "known",
        "packets": 500,
        "seconds": 4.671657,
        "ratio": 0.999563,
        "last_age": 0.003456,
        "max_gap": 0.012345,
        "discarded": 0.023456,
        "rebases": 2,
    }


def test_clock_evidence_is_copied_and_accepted_in_failure_and_success():
    value = media()
    value["source_clock"] = clock()
    original = deepcopy(value)
    projected = _safe_failure_media(value)
    assert projected["source_clock"] == clock()
    assert (
        safe_strict_sink_reader_timings([{"segment": 1, "diagnostic": value}])[0]["diagnostic"][
            "source_clock"
        ]
        == clock()
    )
    projected["source_clock"]["packets"] = 1000
    assert value == original


def test_source_episode_may_precede_the_reader_and_unknown_does_not_invent_zero():
    value = media()
    value["source_clock"] = {**clock(), "seconds": 120, "last_age": 20}
    assert _safe_failure_media(value)["source_clock"]["seconds"] == 120
    value["source_clock"] = {"state": "unknown"}
    assert _safe_failure_media(value)["source_clock"] == {"state": "unknown"}
    value["scope"] = "crash"
    assert _safe_failure_media(value) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(state="PRIVATE"),
        lambda x: x.update(url="rtmp://PRIVATE/key"),
        lambda x: x.update(state="unknown"),
        lambda x: x.update(packets=True),
        lambda x: x.update(packets=1),
        lambda x: x.update(packets=1_000_001),
        lambda x: x.update(rebases=True),
        lambda x: x.update(rebases=-1),
        lambda x: x.update(rebases=1_000_001),
        lambda x: x.update(seconds=0.999),
        lambda x: x.update(seconds=float("inf")),
        lambda x: x.update(seconds=10**500),
        lambda x: x.update(max_gap=5),
        lambda x: x.update(ratio=-1),
        lambda x: x.update(ratio=4.001),
        lambda x: x.update(ratio=True),
        lambda x: x.update(last_age=float("nan")),
        lambda x: x.update(last_age=-1),
        lambda x: x.update(discarded=661),
        lambda x: x.pop("packets"),
    ],
)
def test_malformed_clock_rejects_the_nested_diagnostic(change):
    value = media()
    value["source_clock"] = clock()
    change(value["source_clock"])
    assert _safe_source_clock(value["source_clock"]) is None
    assert _safe_failure_media(value) is None


@pytest.mark.parametrize("value", [None, [], "PRIVATE", {"state": "unknown", "ratio": 0}])
def test_non_schema_unknown_data_is_not_reflected(value):
    assert _safe_source_clock(value) is None


def test_numeric_projection_is_bounded_and_rounded_without_mutating_input():
    value = clock()
    value["ratio"] = 0.987654321
    original = deepcopy(value)
    assert _safe_source_clock(value)["ratio"] == 0.987654
    assert value == original
