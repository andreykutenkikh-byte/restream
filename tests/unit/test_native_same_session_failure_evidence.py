"""Same-session continuity failures retain existing bounded evidence before cleanup."""

from __future__ import annotations

import ast
import contextlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test
from test_native_observer_wait import flow_sample

from scripts.ci_node_onboarding_smoke import safe_self_test_progress

SELF_TEST = Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"


def continuity_gate():
    """Execute the actual gate body without starting services or a media source."""
    tree = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "same_session_no_growth"
    ]
    assert len(matches) == 1
    node = matches[0]
    assert isinstance(node.test.ops[0], ast.Gt)
    assert isinstance(node.test.comparators[0], ast.Name)
    assert node.test.comparators[0].id == "CAPTURE_NO_GROWTH_LIMIT_SECONDS"
    return compile(ast.Module(body=[node], type_ignores=[]), str(SELF_TEST), "exec")


def gate_state(gap, record):
    namespace = load_self_test()
    assert namespace["CAPTURE_NO_GROWTH_LIMIT_SECONDS"] == 3.0
    return {
        "same_session_no_growth": gap,
        "CAPTURE_NO_GROWTH_LIMIT_SECONDS": namespace["CAPTURE_NO_GROWTH_LIMIT_SECONDS"],
        "TestFailure": namespace["TestFailure"],
        "contextlib": contextlib,
        "time": SimpleNamespace(monotonic=lambda: 114.5),
        "record_outage_flow_failure": record,
        "observer": object(),
        "same_session_started": 100.0,
        "transition_log_descriptor": 7,
        "pause_log_offset": 999,
        "capture_observer": object(),
    }


@pytest.mark.parametrize("gap", [0.0, 2.999, 3.0])
def test_success_and_exact_existing_limit_do_not_collect_failure_evidence(gap):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Successful continuity checks must not inspect failure captures")

    state = gate_state(gap, unexpected)
    exec(continuity_gate(), state)  # noqa: S102 - execute the actual isolated test gate
    assert "failure" not in state


@pytest.mark.parametrize("gap", [3.000001, 3.640988569])
def test_original_limit_message_and_failure_identity_are_preserved(gap):
    calls = []
    state = gate_state(gap, lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(state["TestFailure"]) as caught:
        exec(continuity_gate(), state)  # noqa: S102
    assert str(caught.value) == (
        f"downstream media stalled during same-session recovery: {gap:.3f}s"
    )
    assert calls == [
        (
            (caught.value, state["observer"], 100.0, 114.5, 7, 999),
            {"capture_observer": state["capture_observer"]},
        )
    ]


@pytest.mark.parametrize("diagnostic_error", [OSError("PRIVATE"), RuntimeError("PRIVATE")])
def test_diagnostic_error_cannot_replace_or_suppress_the_original_failure(diagnostic_error):
    def broken_diagnostic(*_args, **_kwargs):
        raise diagnostic_error

    state = gate_state(3.641, broken_diagnostic)
    with pytest.raises(state["TestFailure"]) as caught:
        exec(continuity_gate(), state)  # noqa: S102
    assert str(caught.value) == "downstream media stalled during same-session recovery: 3.641s"
    assert "PRIVATE" not in str(caught.value)


def test_same_session_gate_connects_existing_track_collector_to_safe_checkpoint(monkeypatch):
    namespace = load_self_test()
    record = namespace["record_outage_flow_failure"]
    scope = record.__globals__
    samples = [flow_sample(100.1), flow_sample(114.4, sink_bytes=12346)]
    captured = [
        {"t": 100.1, "capture_ok": True, "capture_size": 13},
        {"t": 114.4, "capture_ok": True, "capture_size": 99999},
    ]
    tracks = {
        "state": "known",
        "partial_tail": False,
        "sample_window_seconds": [0.1, 14.4],
        "video": {"packets": 60, "last_observed_seconds": 9.7, "pts_span_seconds": 1.967},
        "audio": {"packets": 200, "last_observed_seconds": 14.3, "pts_span_seconds": 4.245},
    }
    inspections = []

    def inspect_capture(path, observations, first, last):
        inspections.append((path, observations, first, last))
        return tracks

    monkeypatch.setitem(scope, "capture_track_failure_summary", inspect_capture)
    monkeypatch.setitem(scope, "read_validated_log_tail", lambda *_args: b"PRIVATE_URL\n")
    monkeypatch.setitem(scope, "os", SimpleNamespace(geteuid=lambda: 0))
    state = gate_state(3.641, record)
    state["observer"] = SimpleNamespace(samples_between=lambda first, last: samples)
    state["capture_observer"] = SimpleNamespace(
        capture=Path("PRIVATE_CAPTURE.flv"), lock=threading.Lock(), samples=captured
    )
    with pytest.raises(state["TestFailure"]) as caught:
        exec(continuity_gate(), state)  # noqa: S102
    failure, flow = scope["SELF_TEST_FLOW_FAILURE"]
    assert failure is caught.value
    assert inspections == [(Path("PRIVATE_CAPTURE.flv"), captured, 100.0, 114.5)]
    assert inspections[0][1] is not captured  # Snapshot owns its list and each sample.
    assert inspections[0][1][0] is not captured[0]
    assert flow["channels"]["sink"] == {"state": "growth", "last_growth_seconds": [0.1, 14.425]}
    assert flow["capture_tracks"] == tracks
    payload = {
        "job_id": "same-session-job",
        "stage": "stall-cont",
        "elapsed_seconds": 191.698,
        "failure_flow": flow,
    }
    assert safe_self_test_progress(payload, job_id="same-session-job")["failure_flow"] == flow
    for unrelated_stage in ("stall-pre", "stall-live", "stall-ident", "cont-capture"):
        assert safe_self_test_progress(
            {**payload, "stage": unrelated_stage}, job_id="same-session-job"
        ) == {"progress": "unavailable"}
    assert "PRIVATE" not in json.dumps(flow)
