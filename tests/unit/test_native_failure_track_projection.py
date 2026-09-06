"""Failure-only packet evidence stays bounded, correlated and identity-free."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_native_observer_wait import flow_sample, record_flow

from scripts.ci_node_onboarding_smoke import safe_self_test_progress


def tracks():
    return {
        "state": "known",
        "partial_tail": False,
        "sample_window_seconds": [0.1, 4.4],
        "video": {"packets": 60, "last_observed_seconds": 2.1, "pts_span_seconds": 1.967},
        "audio": {"packets": 200, "last_observed_seconds": 4.3, "pts_span_seconds": 4.245},
    }


def payload(monkeypatch):
    state, _failure, _requests = record_flow(
        monkeypatch,
        [
            flow_sample(100.1, normalized_media_bytes=100),
            flow_sample(104.4, normalized_media_bytes=200),
        ],
    )
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    flow["capture_tracks"] = tracks()
    return state, {
        "job_id": "test-job",
        "stage": "outage-normal",
        "elapsed_seconds": 200,
        "failure_flow": flow,
    }


def test_path_media_and_capture_packets_remain_distinct_from_connection_bytes(monkeypatch):
    _state, value = payload(monkeypatch)
    safe = safe_self_test_progress(value, job_id="test-job")["failure_flow"]
    assert safe["channels"]["normalized"]["state"] == "unchanged"
    assert safe["channels"]["normalized_path"]["state"] == "growth"
    assert safe["capture_tracks"] == tracks()
    assert "PRIVATE" not in json.dumps(safe)
    assert safe_self_test_progress(value, job_id="wrong-job") == {"progress": "unavailable"}


@pytest.mark.parametrize(
    "track_value",
    [
        {"state": "unknown"},
        {
            "state": "known",
            "partial_tail": False,
            "sample_window_seconds": [0.1, 4.4],
            "video": {"packets": 0},
            "audio": {"packets": 0},
        },
    ],
)
def test_zero_packets_and_unknown_are_separate_results(monkeypatch, track_value):
    _state, value = payload(monkeypatch)
    value["failure_flow"]["capture_tracks"] = track_value
    assert (
        safe_self_test_progress(value, job_id="test-job")["failure_flow"]["capture_tracks"]
        == track_value
    )


def test_failure_checkpoint_selects_compact_serialization(monkeypatch):
    state, _failure, _requests = record_flow(monkeypatch, [flow_sample(100.1), flow_sample(104.4)])
    failure = state["SELF_TEST_FLOW_FAILURE"][0]
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
    writes = []
    monkeypatch.setitem(
        state, "atomic_json", lambda _path, value, **kwargs: writes.append((value, kwargs))
    )
    state["persist_self_test_failure_progress"](failure)
    assert writes[0][1] == {"compact": True}
    assert "failure_flow" in writes[0][0]


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(url="PRIVATE_URL"),
        lambda x: x.update(state="PRIVATE_STATE"),
        lambda x: x.update(sample_window_seconds=[4.4, 0.1]),
        lambda x: x.update(sample_window_seconds=[0.1, 5.0]),
        lambda x: x["video"].update(packets=True),
        lambda x: x["video"].update(packets=65537),
        lambda x: x["video"].update(packets=0),
        lambda x: x["audio"].update(last_observed_seconds=4.5),
        lambda x: x["audio"].update(last_observed_seconds=float("nan")),
        lambda x: x["audio"].update(pts_span_seconds=661),
        lambda x: x["audio"].update(pts_span_seconds=-1),
        lambda x: x["audio"].update(pts_span_seconds=float("inf")),
        lambda x: x["video"].pop("last_observed_seconds"),
    ],
)
def test_unbounded_mixed_or_lookalike_capture_evidence_is_rejected(monkeypatch, change):
    _state, value = payload(monkeypatch)
    change(value["failure_flow"]["capture_tracks"])
    assert safe_self_test_progress(value, job_id="test-job") == {"progress": "unavailable"}


def test_complete_worst_case_failure_is_atomically_written_under_existing_two_kib(
    monkeypatch, tmp_path
):
    state, value = payload(monkeypatch)
    flow = value["failure_flow"]
    value.update(
        job_id="f" * 36,
        elapsed_seconds=659.999,
        failure_lines=[20000] * 8,
        failure_wait_seconds=659.999,
        failure_flags=dict.fromkeys(
            (
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
            ),
            False,
        ),
    )
    flow.update(
        elapsed_seconds=659.999,
        sample_count=4096,
        sample_window_seconds=[659.998, 659.999],
        max_observation_gap_seconds=659.999,
        markers=dict.fromkeys(state["MEDIA_DIAGNOSTIC_MARKERS"], 255),
    )
    for channel in flow["channels"].values():
        channel.update(state="growth", last_growth_seconds=[659.998, 659.999])
    captured = flow["capture_tracks"]
    captured["sample_window_seconds"] = [659.998, 659.999]
    for name in ("video", "audio"):
        captured[name] = {
            "packets": 65536,
            "last_observed_seconds": 659.999,
            "pts_span_seconds": 660,
        }
    monkeypatch.setitem(state, "os", os)
    target = tmp_path / "progress.json"
    if os.name == "nt":
        # Exercise the actual serializer/replace locally. Directory fsync/modes
        # are POSIX-only and execute without this narrow shim in Linux CI.
        portable_os = SimpleNamespace(**vars(os))
        portable_os.fchmod = lambda *_args: None
        portable_os.O_DIRECTORY = 0
        portable_os.open = lambda path, flags: (
            os.open(target, os.O_RDWR) if path == tmp_path else os.open(path, flags)
        )
        monkeypatch.setitem(state, "os", portable_os)
    original = deepcopy(value)
    state["atomic_json"](target, value, compact=True)
    encoded = target.read_bytes()
    assert len(encoded) <= 2048
    assert json.loads(encoded) == original
    assert safe_self_test_progress(original, job_id=original["job_id"])["failure_flow"] == flow
    assert list(tmp_path.iterdir()) == [target]
    assert value == original
