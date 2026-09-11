"""Actual feeder-loop evidence without changing its scheduling decisions."""

from __future__ import annotations

import ast
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_moblin_relay_bundle import load_self_test, make_media_diagnostic
from test_native_live_feeder_clock import SELF_TEST, run_delayed_feeder


def measured_feeder(**kwargs):
    namespace = load_self_test()
    original = namespace["PacedMPEGTSFeeder"]
    instances = []

    class ObservedFeeder(original):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            instances.append(self)

    namespace["PacedMPEGTSFeeder"] = ObservedFeeder
    observed, interval, clock = run_delayed_feeder(feeder_namespace=namespace, **kwargs)
    return instances[0], observed, interval, clock


def snapshot(feeder, now):
    with patch.dict(
        feeder.clock_snapshot.__globals__, {"time": SimpleNamespace(monotonic=lambda: now)}
    ):
        return feeder.clock_snapshot()


@pytest.mark.parametrize("jitter", [0.001, 0.010, 0.015, 0.050])
def test_actual_loop_clock_matches_successful_dispatches_and_detects_large_jitter(jitter):
    feeder, observed, interval, clock = measured_feeder(wakeup_delay=jitter, sends=1001)
    value = snapshot(feeder, clock.now + 0.25)
    seconds = observed[-1][0] - observed[0][0]
    assert value == {
        "state": "known",
        "packets": 1001,
        "seconds": round(seconds, 6),
        "ratio": round(1000 * interval / seconds, 6),
        "last_age": 0.25,
        "max_gap": round(
            max(b[0] - a[0] for a, b in zip(observed[:-1], observed[1:], strict=True)), 6
        ),
        "discarded": round(feeder._clock_discarded, 6),
        "rebases": feeder._clock_rebases,
    }
    assert feeder._clock_bytes == 1001 * feeder._clock_first_bytes
    if jitter == 0.050:
        assert value["ratio"] < 0.2
        assert value["rebases"] == 1000
        assert value["discarded"] == pytest.approx(50.0)
    else:
        assert 0.995 <= value["ratio"] <= 1
        assert value["rebases"] == value["discarded"] == 0
    assert len(json.dumps(value, separators=(",", ":"))) < 200


def test_actual_resume_ack_resets_episode_without_counting_pause_or_losing_bytes():
    feeder, observed, interval, clock = measured_feeder(
        wakeup_delay=0.001, sends=1001, long_delay_at=5, pause_after=400
    )
    value = snapshot(feeder, clock.now)
    assert clock.pauses and len(observed) == 1001
    assert value["packets"] == 601
    assert value["seconds"] == round(observed[-1][0] - observed[400][0], 6)
    assert value["ratio"] == round(600 * interval / (observed[-1][0] - observed[400][0]), 6)
    assert value["rebases"] == value["discarded"] == 0
    assert value["max_gap"] < 0.011


def test_send_completion_cost_and_long_delay_are_observed_not_hidden():
    feeder, observed, interval, clock = measured_feeder(
        wakeup_delay=0.001, sends=1001, long_delay_at=5, processing_intervals=0.1
    )
    value = snapshot(feeder, clock.now)
    assert feeder._clock_first == pytest.approx(observed[0][0] + interval * 0.1)
    assert feeder._clock_last == pytest.approx(observed[-1][0] + interval * 0.1)
    assert value["last_age"] == 0
    assert value["max_gap"] > 0.5
    assert value["discarded"] >= 0.5
    assert value["rebases"] >= 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("_paused", True),
        ("_pause_requested", True),
        ("_clock_regressed", True),
        ("_clock_packets", 1),
        ("_clock_packets", 1_000_001),
        ("_clock_rebases", -1),
        ("_clock_rebases", 1_000_001),
        ("_clock_first", None),
        ("_clock_last", None),
        ("_clock_first", float("nan")),
        ("_clock_last", float("inf")),
        ("_clock_max_gap", -1),
        ("_clock_max_gap", 661),
        ("_clock_max_gap", 10),
        ("_clock_discarded", float("nan")),
        ("_clock_discarded", 661),
        ("_clock_bytes", 1_000_000_000),
    ],
)
def test_snapshot_is_unknown_for_incomplete_or_unbounded_evidence(field, invalid):
    feeder, _observed, _interval, clock = measured_feeder(wakeup_delay=0.001, sends=201)
    setattr(feeder, field, invalid)
    assert snapshot(feeder, clock.now) == {"state": "unknown"}


@pytest.mark.parametrize("age", [-0.001, 660.001, float("nan"), float("inf")])
def test_snapshot_rejects_invalid_last_dispatch_age(age):
    feeder, _observed, _interval, clock = measured_feeder(wakeup_delay=0.001, sends=201)
    assert snapshot(feeder, clock.now + age) == {"state": "unknown"}


def test_short_episode_is_unknown_and_snapshot_does_not_mutate_stats():
    feeder, _observed, _interval, clock = measured_feeder(wakeup_delay=0.001, sends=20)
    before = dict(vars(feeder))
    assert snapshot(feeder, clock.now) == {"state": "unknown"}
    assert vars(feeder) == before


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("callback_fails", [False, True])
def test_exit_records_once_on_success_or_failure_without_replacing_exception(
    monkeypatch, failure, callback_fails
):
    _namespace, diagnostic = make_media_diagnostic(monkeypatch, scope="capture")
    calls = []
    value = {"state": "unknown"}

    def source_clock():
        calls.append("snapshot")
        if callback_fails:
            raise RuntimeError("PRIVATE_SOURCE_URL_AND_TOKEN")
        return value

    diagnostic.source_clock = source_clock
    diagnostic.thread = SimpleNamespace(join=lambda **_kwargs: None, is_alive=lambda: False)
    error = RuntimeError("original reader failure") if failure else None
    assert diagnostic.__exit__(type(error), error, None) is False
    assert calls == ["snapshot"]
    assert diagnostic.values["source_clock"] == value
    stored = diagnostic.__exit__.__globals__["SELF_TEST_MEDIA_FAILURE"]
    if failure:
        assert stored[0] is error
        assert stored[1]["source_clock"] == value
    else:
        assert stored is None
    assert "PRIVATE" not in json.dumps(diagnostic.values)


def test_capture_callback_freezes_current_feeder_and_only_snapshots_on_exit():
    tree = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    record = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "record_strict_sink_segment"
    )
    keyword = next(
        keyword
        for node in ast.walk(record)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MediaFailureDiagnostics"
        for keyword in node.keywords
        if keyword.arg == "source_clock"
    )
    first = SimpleNamespace(clock_snapshot=lambda: {"state": "known"})
    scope = {"feeder": first}
    callback = eval(  # noqa: S307 - exact checked-in callback AST, no external input
        compile(ast.Expression(keyword.value), "<source-clock-test>", "eval"), scope
    )
    scope["feeder"] = None
    assert callback() == {"state": "known"}
    diagnostic_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MediaFailureDiagnostics"
    )
    callers = [
        method.name
        for method in diagnostic_class.body
        if isinstance(method, ast.FunctionDef)
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "source_clock"
    ]
    assert callers == ["__exit__"]
