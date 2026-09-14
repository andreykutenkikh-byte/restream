"""Failure-only capture wait evidence preserves the original media oracle."""

from __future__ import annotations

import ast
import contextlib
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import SELF_TEST, load_self_test
from test_native_observer_wait import flow_sample

from scripts.ci_node_onboarding_smoke import _safe_capture_wait, safe_self_test_progress


def wait_probe(monkeypatch, *, final=112, valid=True, alive=True):
    namespace = load_self_test()
    capture = namespace["CaptureObserver"](Path("PRIVATE_CAPTURE.flv"))
    ticks = iter([100.0, 100.1, 103.0, 103.0])
    sleeps, reads = [], []
    samples = iter(
        [
            {"t": 100.0, "capture_ok": True, "capture_size": 100},
            {"t": 100.1, "capture_ok": valid, "capture_size": final},
        ]
    )

    def append_sample():
        sample = next(samples)
        reads.append(sample)
        return sample

    monkeypatch.setitem(
        capture.wait_growth.__globals__,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(ticks),
            sleep=sleeps.append,
        ),
    )
    monkeypatch.setattr(capture, "is_alive", lambda: alive)
    monkeypatch.setattr(capture, "append_sample", append_sample)
    return namespace, capture, reads, sleeps


def test_timeout_retains_evaluated_samples_exact_exception_and_original_wait(monkeypatch):
    namespace, capture, reads, sleeps = wait_probe(monkeypatch)
    with pytest.raises(namespace["TestFailure"], match="timed out waiting for PRIVATE") as caught:
        capture.wait_growth("PRIVATE", 16384, 3.0)
    assert capture.growth_failure == (
        caught.value,
        {
            "started": 100.0,
            "finished": 103.0,
            "growth_bytes": 12,
            "required_bytes": 16384,
            "elapsed_ms": 3000,
            "last_sample_age_ms": 2900,
        },
    )
    assert len(reads) == 2 and sleeps == [0.05]
    assert namespace["SLATE_CAPTURE_GROWTH_BYTES"] == 16384
    assert namespace["SLATE_CAPTURE_GROWTH_TIMEOUT_SECONDS"] == 3.0


def test_success_uses_same_threshold_and_clears_prior_diagnostic(monkeypatch):
    _namespace, capture, reads, sleeps = wait_probe(monkeypatch, final=16484)
    capture.growth_failure = (ValueError("old"), {})
    result = capture.wait_growth("PRIVATE", 16384, 3.0)
    assert result is reads[-1] and len(reads) == 2 and sleeps == []
    assert capture.growth_failure is None


@pytest.mark.parametrize("options", [{"valid": False}, {"alive": False}])
def test_other_failure_does_not_inherit_timeout_evidence(monkeypatch, options):
    namespace, capture, _reads, _sleeps = wait_probe(monkeypatch, **options)
    capture.growth_failure = (ValueError("old"), {})
    with pytest.raises(namespace["TestFailure"]):
        capture.wait_growth("PRIVATE", 16384, 3.0)
    assert capture.growth_failure is None


@pytest.mark.parametrize("final", [99, True, 2**31 + 1])
def test_invalid_numeric_capture_delta_is_unknown_without_changing_oracle(monkeypatch, final):
    namespace, capture, _reads, _sleeps = wait_probe(monkeypatch, final=final)
    # A value above the original minimum still succeeds; diagnostics may not override it.
    if final > 16484:
        assert capture.wait_growth("PRIVATE", 16384, 3.0)["capture_size"] == final
        assert capture.growth_failure is None
    else:
        with pytest.raises(namespace["TestFailure"]):
            capture.wait_growth("PRIVATE", 16384, 3.0)
        assert capture.growth_failure[1]["growth_bytes"] is None


def recorded(monkeypatch, samples, *, poll_result=None, poll_error=None, matching=True):
    namespace = load_self_test()
    record = namespace["record_outage_flow_failure"]
    scope = record.__globals__
    failure = namespace["TestFailure"]("PRIVATE_FAILURE")
    polls, requests = [], []

    def poll():
        polls.append(True)
        if poll_error:
            raise poll_error
        return poll_result

    def sample_window(first, last):
        requests.append((first, last))
        return samples

    capture = SimpleNamespace(
        capture=Path("PRIVATE_CAPTURE.flv"),
        lock=threading.Lock(),
        samples=[],
        growth_failure=(
            failure if matching else namespace["TestFailure"](str(failure)),
            {
                "started": 101.0,
                "finished": 104.0,
                "growth_bytes": 12,
                "required_bytes": 16384,
                "elapsed_ms": 3000,
                "last_sample_age_ms": 50,
            },
        ),
    )
    observer = SimpleNamespace(samples_between=sample_window, reader=SimpleNamespace(poll=poll))
    monkeypatch.setitem(scope, "read_validated_log_tail", lambda *_args: b"PRIVATE_URL\n")
    monkeypatch.setitem(scope, "os", SimpleNamespace(geteuid=lambda: 0))
    monkeypatch.setitem(scope, "capture_track_failure_summary", lambda *_args: {"state": "unknown"})
    record(failure, observer, 100.0, 104.5, 7, 999, capture_observer=capture)
    assert requests == [(100.0, 104.5)]  # Reuse only existing observer samples.
    assert scope["SELF_TEST_FLOW_FAILURE"][0] is failure
    return scope, failure, polls


def progress(flow):
    return {
        "job_id": "test-job",
        "stage": "outage-normal",
        "elapsed_seconds": 200,
        "failure_flow": flow,
    }


def test_wait_window_excludes_prior_growth_later_growth_and_straddling_samples(monkeypatch):
    rows = [
        flow_sample(100.1, sink_bytes=1),
        flow_sample(100.99, sink_bytes=2),  # HTTP observation straddles wait entry.
        flow_sample(101.1, sink_bytes=3),
        flow_sample(103.9, sink_bytes=3),
        flow_sample(103.99, sink_bytes=4),  # HTTP observation straddles wait end.
        flow_sample(104.2, sink_bytes=5),
    ]
    state, _failure, polls = recorded(monkeypatch, rows)
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    assert flow["channels"]["sink"]["state"] == "growth"  # Original outage scope preserved.
    wait = flow["capture_wait"]
    assert wait == {
        "window_seconds": [1.0, 4.0],
        "recorder_at_collection": "alive",
        "growth_bytes": 12,
        "required_bytes": 16384,
        "elapsed_ms": 3000,
        "last_sample_age_ms": 50,
        "sink_state": "unchanged",
        "sink_samples": 2,
        "sink_sample_window_seconds": [1.1, 3.925],
    }
    assert polls == [True]
    safe = safe_self_test_progress(progress(flow), job_id="test-job")["failure_flow"]
    assert safe == flow and "PRIVATE" not in json.dumps(safe)
    safe["capture_wait"]["window_seconds"][0] = 99
    safe["capture_wait"]["sink_sample_window_seconds"][0] = 99
    assert wait["window_seconds"][0] == 1.0 and wait["sink_sample_window_seconds"][0] == 1.1


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"sink_bytes": 12346}, "growth"),
        ({"sink_bytes": 12345}, "unchanged"),
        ({"sink_bytes": 12344}, "unknown"),
        ({"sink_bytes": True}, "unknown"),
        ({"sink_bytes": 2**63}, "unknown"),
        ({"sink_metrics_ok": False}, "unknown"),
        ({"sink_ids": ["PRIVATE_OTHER"]}, "unknown"),
        ({"sink_ids": []}, "unknown"),
    ],
)
def test_sink_counter_evidence_requires_stable_identity_and_metrics(monkeypatch, updates, expected):
    state, _failure, _polls = recorded(
        monkeypatch,
        [
            flow_sample(101.1),
            flow_sample(103.9, **updates),
        ],
    )
    assert state["SELF_TEST_FLOW_FAILURE"][1]["capture_wait"]["sink_state"] == expected


@pytest.mark.parametrize("rows", [[], [flow_sample(102.0)]])
def test_sparse_observations_cannot_claim_sink_progress_or_stall(monkeypatch, rows):
    state, _failure, _polls = recorded(monkeypatch, rows)
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    assert flow["capture_wait"]["sink_state"] == "unknown"
    assert flow["capture_wait"]["sink_samples"] == len(rows)
    assert safe_self_test_progress(progress(flow), job_id="test-job")["failure_flow"] == flow


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"poll_result": 0}, "exited"),
        ({"poll_result": -9}, "exited"),
        ({"poll_error": OSError("PRIVATE")}, "unknown"),
    ],
)
def test_recorder_state_is_single_collection_poll_not_historical_claim(
    monkeypatch, options, expected
):
    state, _failure, polls = recorded(monkeypatch, [], **options)
    assert polls == [True]
    assert state["SELF_TEST_FLOW_FAILURE"][1]["capture_wait"]["recorder_at_collection"] == expected


def test_same_message_different_exception_does_not_attach_or_poll(monkeypatch):
    state, _failure, polls = recorded(monkeypatch, [], matching=False)
    assert "capture_wait" not in state["SELF_TEST_FLOW_FAILURE"][1] and polls == []


@pytest.mark.parametrize(
    "updates",
    [
        {"url": "PRIVATE"},
        {"recorder_at_collection": "PRIVATE"},
        {"sink_state": []},
        {"growth_bytes": True},
        {"growth_bytes": -1},
        {"growth_bytes": 16384},
        {"required_bytes": 0},
        {"required_bytes": 10**310},
        {"elapsed_ms": True},
        {"elapsed_ms": 2997},
        {"last_sample_age_ms": 3001},
        {"window_seconds": [1.0, float("nan")]},
        {"window_seconds": [1.0, float("inf")]},
        {"window_seconds": [1.0, 10**310]},
        {"window_seconds": [4.0, 1.0]},
        {"window_seconds": [True, 4.0]},
        {"sink_samples": True},
        {"sink_samples": 4097},
        {"sink_samples": 1, "sink_state": "growth"},
        {"sink_samples": 0},
        {"sink_sample_window_seconds": None},
        {"sink_sample_window_seconds": [0.9, 3.0]},
        {"sink_sample_window_seconds": [1.1, 4.1]},
    ],
)
def test_malformed_optional_evidence_is_rejected_not_partially_projected(monkeypatch, updates):
    state, _failure, _polls = recorded(monkeypatch, [flow_sample(101.1), flow_sample(103.9)])
    flow = deepcopy(state["SELF_TEST_FLOW_FAILURE"][1])
    flow["capture_wait"].update(updates)
    assert _safe_capture_wait(flow["capture_wait"], 4.5) is None
    assert safe_self_test_progress(progress(flow), job_id="test-job") == {"progress": "unavailable"}


def test_checkpoint_optional_budget_and_exception_identity_preserve_fatal(monkeypatch):
    state, failure, _polls = recorded(monkeypatch, [flow_sample(101.1), flow_sample(103.9)])
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    writes = []
    monkeypatch.setitem(state, "SELF_TEST_STAGE_FILE", "test-stage")
    monkeypatch.setitem(
        state,
        "SELF_TEST_LAST_PROGRESS",
        {
            "job_id": "test-job",
            "stage": "outage-normal",
            "elapsed_seconds": 200,
        },
    )
    monkeypatch.setitem(state, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(state, "atomic_json", lambda _path, value, **_kwargs: writes.append(value))
    state["persist_self_test_failure_progress"](failure)
    assert writes[-1]["failure_flow"]["capture_wait"] == flow["capture_wait"]
    state["persist_self_test_failure_progress"](state["TestFailure"](str(failure)))
    assert "failure_flow" not in writes[-1]
    # Construct a bounded existing flow near its checkpoint budget; no raw fields.
    state["SELF_TEST_LAST_PROGRESS"].update(job_id="f" * 36, elapsed_seconds=659.999)
    flow.update(
        elapsed_seconds=659.999,
        sample_count=4096,
        sample_window_seconds=[0.1, 659.999],
        max_observation_gap_seconds=659.999,
    )
    flow["channels"]["normalized_path"] = {"state": "unknown"}
    flow["markers"] = dict.fromkeys(state["MEDIA_DIAGNOSTIC_MARKERS"], 255)
    for name in flow["channels"]:
        flow["channels"][name] = {"state": "growth", "last_growth_seconds": [659.998, 659.999]}
    flow["capture_tracks"] = {
        "state": "known",
        "partial_tail": False,
        "sample_window_seconds": [1.1, 3.925],
        **{
            name: {"packets": 65536, "last_observed_seconds": 3.925, "pts_span_seconds": 659.999}
            for name in ("video", "audio")
        },
    }
    monkeypatch.setitem(
        state,
        "SELF_TEST_WAIT_FAILURE",
        (
            failure,
            dict.fromkeys(
                [
                    "live",
                    "normalized",
                    "path_ready",
                    "ingest_live",
                    "metrics_ok",
                    "core_alive",
                    "ingest_one",
                    "sink_one",
                    "sink_growth",
                    "state_ok",
                    "ingest_match",
                ],
                False,
            ),
            659.999,
        ),
    )
    source = "\n" * 19996 + (
        "def recurse(n):\n if n: return recurse(n-1)\n raise failure\nrecurse(6)\n"
    )
    with pytest.raises(state["TestFailure"]) as caught:
        exec(compile(source, str(SELF_TEST), "exec"), {"failure": failure})  # noqa: S102
    assert caught.value is failure
    state["persist_self_test_failure_progress"](failure)
    assert len(json.dumps(writes[-1], separators=(",", ":")).encode()) <= 2048
    assert safe_self_test_progress(writes[-1], job_id="f" * 36)["failure_flow"] == flow
    # Exercise the defensive size branch with deliberately oversized prior
    # metadata. This is not a valid external job ID or a real emitted fixture.
    state["SELF_TEST_LAST_PROGRESS"]["job_id"] = "f" * 300
    state["persist_self_test_failure_progress"](failure)
    saved = writes[-1]
    assert "capture_wait" not in saved["failure_flow"], len(
        json.dumps(saved, separators=(",", ":")).encode()
    )
    assert "capture_wait" in flow  # Never mutate retained evidence.
    assert len(saved["failure_lines"]) == 8 and saved["failure_flags"]["core_alive"] is False
    assert len(json.dumps(saved, separators=(",", ":")).encode()) <= 2048


def outage_growth_gate():
    tree = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Call)
        and isinstance(node.body[0].value.func, ast.Name)
        and node.body[0].value.func.id == "wait_slate_capture_growth"
    ]
    assert len(matches) == 1
    return compile(ast.Module(body=matches, type_ignores=[]), str(SELF_TEST), "exec")


@pytest.mark.parametrize("collect_error", [False, True])
def test_exact_outage_growth_catch_keeps_original_exception_and_scope(collect_error):
    namespace = load_self_test()
    failure = namespace["TestFailure"]("original timeout")
    calls = []

    def wait(*args):
        calls.append(("wait", args))
        raise failure

    def record(*args, **kwargs):
        calls.append(("record", args, kwargs))
        if collect_error:
            raise OSError("PRIVATE")

    state = {
        "wait_slate_capture_growth": wait,
        "index": 1,
        "detached": {},
        "TestFailure": namespace["TestFailure"],
        "contextlib": contextlib,
        "record_outage_flow_failure": record,
        "observer": object(),
        "outage_started": 100.0,
        "time": SimpleNamespace(monotonic=lambda: 104.5),
        "transition_log_descriptor": 7,
        "outage_reset_log_offset": 999,
        "capture_observer": object(),
    }
    with pytest.raises(namespace["TestFailure"]) as caught:
        exec(outage_growth_gate(), state)  # noqa: S102 - isolated original gate, no media.
    assert caught.value is failure
    assert calls[1] == (
        "record",
        (failure, state["observer"], 100.0, 104.5, 7, 999),
        {"capture_observer": state["capture_observer"]},
    )
