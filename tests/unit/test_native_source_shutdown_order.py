from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

SELF_TEST = Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"


def bind_source_shutdown(namespace, feeder, helper, publisher, *, previous_order=False):
    """Execute the actual nested helper without starting the full native topology."""
    source = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "stop_primary_srt_source"
    ]
    assert len(functions) == 1
    stop = copy.deepcopy(functions[0])
    if previous_order:
        # Replay only the former order: leave the SRT sender alive throughout
        # feeder.finish(). Every operation and failure guard stays identical.
        helper_guard = next(
            index
            for index, node in enumerate(stop.body)
            if isinstance(node, ast.If)
            and "safe_stop(primary_helper, force=True)" in ast.unparse(node.test)
        )
        helper_stop = stop.body[helper_guard : helper_guard + 2]
        assert ast.unparse(helper_stop[1]) == "primary_helper = None"
        del stop.body[helper_guard : helper_guard + 2]
        feeder_cleared = next(
            index for index, node in enumerate(stop.body) if ast.unparse(node) == "feeder = None"
        )
        stop.body[feeder_cleared + 1 : feeder_cleared + 1] = helper_stop
    wrapper = ast.parse(
        "def bind(feeder, primary_helper, publisher):\n"
        "    pass\n"
        "    return stop_primary_srt_source, lambda: (feeder, primary_helper, publisher)\n"
    )
    wrapper.body[0].body[0] = stop
    ast.fix_missing_locations(wrapper)
    exec(compile(wrapper, str(SELF_TEST), "exec"), namespace)  # noqa: S102 - repository test code
    return namespace["bind"](feeder, helper, publisher)


def shutdown_fixture(*, cleanup_delay=1.5, helper_stops=True, feeder_stops=True):
    clock = SimpleNamespace(now=0.0)
    helper = SimpleNamespace(alive=True)
    publisher = SimpleNamespace(alive=True)
    events = []
    srt_sends = []

    def finish_feeder():
        events.append(("feeder-cleanup", clock.now))
        # Model a permitted slow terminate/join while the helper has already
        # buffered media. Its SRT sender is independent of the feeder thread.
        for _ in range(3):
            clock.now += cleanup_delay / 3
            if helper.alive:
                srt_sends.append(clock.now)
        return feeder_stops

    feeder = SimpleNamespace(healthy=lambda: True, finish=finish_feeder)

    def safe_stop(process, *, force):
        assert force is True
        if process is helper:
            events.append(("srt-cut", clock.now))
            helper.alive = not helper_stops
            return helper_stops
        assert process is publisher
        events.append(("publisher-stop", clock.now))
        publisher.alive = False
        return True

    def wait_ports_released(ports):
        assert ports == (("tcp", 11936), ("udp", 11937), ("tcp", 11938))
        assert not helper.alive and not publisher.alive
        events.append(("ports-released", clock.now))

    namespace = {
        "TestFailure": RuntimeError,
        "safe_stop": safe_stop,
        "wait_ports_released": wait_ports_released,
        "SOURCE_PRIMARY_RTMP_PORT": 11936,
        "SOURCE_PRIMARY_FEED_PORT": 11937,
        "SOURCE_PRIMARY_METRICS_PORT": 11938,
    }
    return namespace, feeder, helper, publisher, events, srt_sends


@pytest.mark.parametrize("cleanup_delay", [0.03, 0.5, 1.5])
def test_outage_cuts_srt_before_slow_feeder_cleanup(cleanup_delay):
    for previous_order in (True, False):
        namespace, feeder, helper, publisher, events, srt_sends = shutdown_fixture(
            cleanup_delay=cleanup_delay
        )
        stop, remaining = bind_source_shutdown(
            namespace, feeder, helper, publisher, previous_order=previous_order
        )
        stop("synthetic outage")
        assert remaining() == (None, None, None)
        if previous_order:
            assert srt_sends == pytest.approx(
                [cleanup_delay / 3, cleanup_delay * 2 / 3, cleanup_delay]
            )
            assert events[:2] == [("feeder-cleanup", 0.0), ("srt-cut", cleanup_delay)]
        else:
            assert srt_sends == []
            assert events[:2] == [("srt-cut", 0.0), ("feeder-cleanup", 0.0)]
        assert [event for event, _when in events[2:]] == ["publisher-stop", "ports-released"]


def test_failed_srt_cut_does_not_enter_slow_cleanup_or_claim_shutdown():
    namespace, feeder, helper, publisher, events, _sends = shutdown_fixture(helper_stops=False)
    stop, remaining = bind_source_shutdown(namespace, feeder, helper, publisher)
    with pytest.raises(RuntimeError, match="SRT source helper did not stop"):
        stop("synthetic outage")
    assert remaining() == (feeder, helper, publisher)
    assert events == [("srt-cut", 0.0)]


def test_failed_feeder_cleanup_keeps_remaining_processes_for_outer_cleanup():
    namespace, feeder, helper, publisher, events, srt_sends = shutdown_fixture(feeder_stops=False)
    stop, remaining = bind_source_shutdown(namespace, feeder, helper, publisher)
    with pytest.raises(RuntimeError, match="live feeder did not stop"):
        stop("synthetic outage")
    assert remaining() == (feeder, None, publisher)
    assert not helper.alive and publisher.alive
    assert srt_sends == []
    assert events == [("srt-cut", 0.0), ("feeder-cleanup", 0.0)]
