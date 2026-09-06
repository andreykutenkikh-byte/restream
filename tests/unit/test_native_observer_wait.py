"""Exercise generic observer timeouts without processes, clocks or live media."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_normalizer, load_self_test

from scripts.ci_node_onboarding_smoke import safe_self_test_progress


def sample(timestamp: float, sink_bytes: int) -> dict:
    return {
        "t": timestamp,
        "live": True,
        "normalized": True,
        "path_ready": True,
        "ingest_live": True,
        "dut_metrics_ok": True,
        "sink_metrics_ok": True,
        "dut_alive": True,
        "sink_alive": True,
        "reader_alive": True,
        "ingest_ids": ["PRIVATE_INGEST_ID"],
        "sink_ids": ["PRIVATE_SINK_ID"],
        "sink_bytes": sink_bytes,
        "forward": True,
        "transport_url": "rtmps://private.invalid/live#PRIVATE_KEY",
    }


def observer_wait(monkeypatch: pytest.MonkeyPatch, samples: list[dict]):
    namespace = load_self_test()
    observer_type = namespace["Observer"]
    state = observer_type.wait_sample.__globals__
    observer = observer_type.__new__(observer_type)
    clock = SimpleNamespace(now=100.0, sleeps=[], reads=0)

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now = round(clock.now + seconds, 6)

    def snapshot(_description):
        current = samples[min(clock.reads, len(samples) - 1)]
        clock.reads += 1
        return current

    observer.checked_snapshot = snapshot
    monkeypatch.setitem(state, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return observer, state, clock


def test_observer_timeout_retains_last_distinct_flags_and_safe_checkpoint(monkeypatch) -> None:
    samples = [sample(100.0, 12345), sample(100.05, 12346)]
    observer, state, clock = observer_wait(monkeypatch, samples)
    predicates = []
    health_checks = []

    def needs_slate(current):
        predicates.append(current)
        return not current["normalized"] and not current["live"] and current["path_ready"]

    with pytest.raises(state["TestFailure"]) as caught:
        observer.wait_sample(
            "PRIVATE_DESCRIPTION", needs_slate, 0.2, health_check=lambda: health_checks.append(1)
        )
    failure, flags, elapsed = state["SELF_TEST_WAIT_FAILURE"]
    assert failure is caught.value
    assert str(failure) == "timed out waiting for PRIVATE_DESCRIPTION"
    assert elapsed == 0.2
    assert clock.sleeps == [0.05] * 4
    assert predicates == [samples[0], samples[1], samples[1], samples[1]]
    assert len(health_checks) == 5
    assert flags == {
        "live": True,
        "normalized": True,
        "path_ready": True,
        "ingest_live": True,
        "metrics_ok": True,
        "core_alive": True,
        "ingest_one": True,
        "sink_one": True,
        "sink_growth": True,
        "state_ok": False,
    }

    checkpoints = []
    monkeypatch.setitem(state, "SELF_TEST_STAGE_FILE", "unused-fixture-stage")
    monkeypatch.setitem(
        state,
        "SELF_TEST_LAST_PROGRESS",
        {"job_id": "test-job", "stage": "outage-normal", "elapsed_seconds": 2.0},
    )
    monkeypatch.setitem(state, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(state, "atomic_json", lambda _path, value: checkpoints.append(value))
    state["persist_self_test_failure_progress"](failure)
    checkpoint = checkpoints[0]
    assert safe_self_test_progress(checkpoint, job_id="test-job") == {
        key: value for key, value in checkpoint.items() if key != "job_id"
    }
    assert checkpoint["failure_flags"] == flags
    assert checkpoint["failure_wait_seconds"] == elapsed
    assert "PRIVATE" not in json.dumps(checkpoint)
    assert "rtmps" not in json.dumps(checkpoint)
    assert "1234" not in json.dumps(checkpoint)


@pytest.mark.parametrize("missing_metrics", [True, False])
def test_observer_timeout_does_not_evaluate_incomplete_or_stale_samples(
    monkeypatch, missing_metrics
) -> None:
    current = sample(100.0 if missing_metrics else 99.0, 12345)
    if missing_metrics:
        current["dut_metrics_ok"] = False
    observer, state, _clock = observer_wait(monkeypatch, [current])

    def unexpected_predicate(_sample):
        pytest.fail("Incomplete or stale metrics must not evaluate the predicate")

    with pytest.raises(state["TestFailure"], match="^timed out waiting for fixture$"):
        observer.wait_sample("fixture", unexpected_predicate, 0.1)
    _failure, flags, elapsed = state["SELF_TEST_WAIT_FAILURE"]
    assert elapsed == 0.1
    assert flags["metrics_ok"] is not missing_metrics
    assert flags["state_ok"] is False
    assert flags["sink_growth"] is False


def test_observer_diagnostic_failure_cannot_replace_timeout(monkeypatch) -> None:
    observer, state, _clock = observer_wait(monkeypatch, [sample(100.0, 12345)])
    failures = []

    class RecordedFailure(state["TestFailure"]):
        def __init__(self, message):
            super().__init__(message)
            failures.append(self)

    def broken_diagnostic(*_args, **_kwargs):
        raise ValueError("PRIVATE_DIAGNOSTIC_FAILURE")

    monkeypatch.setitem(state, "TestFailure", RecordedFailure)
    monkeypatch.setitem(state, "safe_wait_failure_flags", broken_diagnostic)
    with pytest.raises(RecordedFailure) as caught:
        observer.wait_sample("fixture", lambda _sample: False, 0.1)
    assert failures == [caught.value]
    assert str(caught.value) == "timed out waiting for fixture"
    assert state["SELF_TEST_WAIT_FAILURE"] is None


@pytest.mark.parametrize("elapsed", [660.0, 660.001, float("inf")])
def test_observer_timeout_diagnostic_bounds_elapsed(monkeypatch, elapsed) -> None:
    observer, state, clock = observer_wait(monkeypatch, [sample(100.0, 12345)])
    state["time"].sleep = lambda _seconds: setattr(clock, "now", 100.0 + elapsed)
    with pytest.raises(state["TestFailure"], match="^timed out waiting for fixture$"):
        observer.wait_sample("fixture", lambda _sample: False, 1)
    if elapsed == 660:
        assert state["SELF_TEST_WAIT_FAILURE"][2] == 660
    else:
        assert state["SELF_TEST_WAIT_FAILURE"] is None


def test_observer_success_never_invokes_failure_diagnostics(monkeypatch) -> None:
    current = sample(100.0, 12345)
    observer, state, clock = observer_wait(monkeypatch, [current])

    def unexpected_diagnostic(*_args, **_kwargs):
        pytest.fail("Successful waits must not invoke failure diagnostics")

    monkeypatch.setitem(state, "safe_wait_failure_flags", unexpected_diagnostic)
    assert observer.wait_sample("fixture", lambda _sample: True, 1) is current
    assert clock.sleeps == []
    assert state["SELF_TEST_WAIT_FAILURE"] is None


def flow_sample(timestamp: float, **updates) -> dict:
    return {
        **sample(timestamp, 12345),
        "finished": timestamp + 0.025,
        "normalized_ids": ["PRIVATE_NORMALIZER_ID"],
        "ingest_bytes": 54321,
        "ingest_transport_bytes": 65432,
        "normalized_bytes": 76543,
        **updates,
    }


def record_flow(monkeypatch, samples, tail=b""):
    record = load_self_test()["record_outage_flow_failure"]
    state = record.__globals__
    requests = []

    def read_tail(descriptor, offset, uid):
        requests.append((descriptor, offset, uid))
        if isinstance(tail, Exception):
            raise tail
        return tail

    monkeypatch.setitem(state, "read_validated_log_tail", read_tail)
    monkeypatch.setitem(state, "os", SimpleNamespace(geteuid=lambda: 123))
    observer = SimpleNamespace(samples_between=lambda first, last: samples)
    failure = state["TestFailure"]("PRIVATE_FAILURE")
    record(failure, observer, 100.0, 104.5, 7, 999)
    return state, failure, requests


def test_outage_flow_distinguishes_buffered_growth_from_final_unchanged_pair(monkeypatch) -> None:
    samples = [
        flow_sample(100.1),
        flow_sample(101.0, ingest_bytes=54322),
        flow_sample(102.3, ingest_bytes=54322, normalized_bytes=76544, sink_bytes=12346),
        flow_sample(104.3, ingest_bytes=54322, normalized_bytes=76544, sink_bytes=12346),
    ]
    marker = b"moblin-relay-normalize:restart:verified-stall"
    state, failure, requests = record_flow(
        monkeypatch,
        samples,
        marker + b"\r\n" + marker + b" PRIVATE_URL\nPRIVATE_PREFIX" + marker + b"\n" + marker,
    )
    caught, flow = state["SELF_TEST_FLOW_FAILURE"]
    assert caught is failure
    assert requests == [(7, 999, 123)]  # The already-open event-local log, no new observer.
    assert flow == {
        "elapsed_seconds": 4.5,
        "sample_count": 4,
        "sample_window_seconds": [0.1, 4.325],
        "max_observation_gap_seconds": 2.025,
        "log_ok": True,
        "markers": {"verified-stall": 1},
        "channels": {
            "ingest_path": {"state": "growth", "last_growth_seconds": [0.1, 1.025]},
            "ingest_transport": {"state": "unchanged"},
            "normalized": {"state": "growth", "last_growth_seconds": [1.0, 2.325]},
            "sink": {"state": "growth", "last_growth_seconds": [1.0, 2.325]},
        },
    }
    assert not state["safe_wait_failure_flags"](
        samples[-1],
        samples[-2],
        state_ok=False,
        expected_ingest_ids=None,
    )["sink_growth"]
    payload = {
        "job_id": "test-job",
        "stage": "outage-normal",
        "elapsed_seconds": 179.56,
        "failure_flow": flow,
    }
    assert safe_self_test_progress(payload, job_id="test-job")["failure_flow"] == flow
    assert all(token not in json.dumps(flow) for token in ("PRIVATE", "12345", "54322", "76544"))


@pytest.mark.parametrize(
    "updates",
    [
        {"normalized_bytes": None},
        {"normalized_bytes": 0},
        {"normalized_bytes": True},
        {"normalized_bytes": 76543.0},
        {"normalized_bytes": 2**63},
        {"normalized_ids": []},
        {"normalized_ids": ["PRIVATE_CHANGED_ID"]},
    ],
)
def test_outage_flow_discards_entire_counter_history_on_untrusted_sample(monkeypatch, updates):
    samples = [flow_sample(100.1), flow_sample(101.0, **updates), flow_sample(102.0)]
    state, _failure, _requests = record_flow(monkeypatch, samples)
    channels = state["SELF_TEST_FLOW_FAILURE"][1]["channels"]
    assert channels["normalized"] == {"state": "unknown"}
    assert channels["ingest_transport"] == {"state": "unchanged"}


@pytest.mark.parametrize("samples", [[], [flow_sample(100.1)]])
def test_outage_flow_insufficient_samples_are_unknown_not_unchanged(monkeypatch, samples):
    state, _failure, _requests = record_flow(monkeypatch, samples)
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    assert flow["sample_count"] == len(samples)
    assert all(channel == {"state": "unknown"} for channel in flow["channels"].values())
    payload = {
        "job_id": "test-job",
        "stage": "outage-normal",
        "elapsed_seconds": 1,
        "failure_flow": flow,
    }
    assert "failure_flow" in safe_self_test_progress(payload, job_id="test-job")


@pytest.mark.parametrize(
    "updates",
    [
        {"finished": 99.0},
        {"finished": 105.0},
        {"finished": float("nan")},
        {"t": True},
        {"t": 100.0},
        {"finished": "PRIVATE_TIME"},
    ],
)
def test_outage_flow_rejects_unordered_or_unbounded_sample_intervals(monkeypatch, updates):
    samples = [flow_sample(100.1), flow_sample(101.0, **updates)]
    state, _failure, requests = record_flow(monkeypatch, samples)
    assert state["SELF_TEST_FLOW_FAILURE"] is None
    assert not requests


def test_outage_flow_missing_metrics_and_unreadable_log_never_reuse_evidence(monkeypatch):
    samples = [flow_sample(100.1), flow_sample(101.0, dut_metrics_ok=False), flow_sample(102.0)]
    state, _failure, _requests = record_flow(monkeypatch, samples, OSError("PRIVATE_LOG"))
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    assert not flow["log_ok"] and not flow["markers"]
    assert all(
        flow["channels"][name] == {"state": "unknown"}
        for name in ("ingest_path", "ingest_transport", "normalized")
    )
    assert flow["channels"]["sink"] == {"state": "unchanged"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda f: f.update(sample_count=True),
        lambda f: f.update(sample_count=4097),
        lambda f: f.update(sample_window_seconds=[1.0, 0.0]),
        lambda f: f.update(max_observation_gap_seconds=float("inf")),
        lambda f: f.update(log_ok=False, markers={"verified-stall": 1}),
        lambda f: f.update(markers={"PRIVATE_MARKER": 1}),
        lambda f: f.update(PRIVATE_URL="rtmps://private.invalid/live"),
        lambda f: f["channels"]["sink"].update(last_growth_seconds=[0, 1]),
        lambda f: f["channels"]["sink"].update(state="PRIVATE_STATE"),
        lambda f: f["channels"]["sink"].update(state="growth", last_growth_seconds=[0, 999]),
        lambda f: f["channels"].update(PRIVATE_COUNTER={"state": "unknown"}),
    ],
)
def test_outage_flow_projection_rejects_lookalikes_and_inconsistent_evidence(monkeypatch, mutation):
    state, _failure, _requests = record_flow(monkeypatch, [flow_sample(100.1), flow_sample(101.0)])
    flow = deepcopy(state["SELF_TEST_FLOW_FAILURE"][1])
    mutation(flow)
    payload = {
        "job_id": "test-job",
        "stage": "outage-normal",
        "elapsed_seconds": 1,
        "failure_flow": flow,
    }
    assert safe_self_test_progress(payload, job_id="test-job") == {"progress": "unavailable"}


def test_outage_flow_checkpoint_keeps_fixed_two_kib_limit_and_exception_identity(monkeypatch):
    samples = [
        flow_sample(100.1),
        flow_sample(
            104.4,
            ingest_bytes=999999,
            sink_bytes=999999,
            normalized_bytes=999999,
            ingest_transport_bytes=999999,
        ),
    ]
    state, failure, _requests = record_flow(monkeypatch, samples)
    flow = state["SELF_TEST_FLOW_FAILURE"][1]
    flow.update(
        elapsed_seconds=659.999,
        sample_count=4096,
        sample_window_seconds=[659.998, 659.999],
        max_observation_gap_seconds=659.999,
    )
    for channel in flow["channels"].values():
        channel["last_growth_seconds"] = [659.998, 659.999]
    flow["markers"] = dict.fromkeys(state["MEDIA_DIAGNOSTIC_MARKERS"], 255)
    checkpoints = []
    monkeypatch.setitem(state, "SELF_TEST_STAGE_FILE", "unused-fixture-stage")
    monkeypatch.setitem(
        state,
        "SELF_TEST_LAST_PROGRESS",
        {
            "job_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "stage": "outage-normal",
            "elapsed_seconds": 659.999,
        },
    )
    monkeypatch.setitem(
        state,
        "SELF_TEST_WAIT_FAILURE",
        (
            failure,
            {
                key: False
                for key in state["safe_wait_failure_flags"](
                    samples[-1],
                    samples[0],
                    state_ok=False,
                    expected_ingest_ids=["PRIVATE_ID"],
                )
            },
            659.999,
        ),
    )
    monkeypatch.setitem(state, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(state, "atomic_json", lambda _path, value: checkpoints.append(value))
    state["persist_self_test_failure_progress"](failure)
    checkpoint = checkpoints[-1]
    checkpoint["failure_lines"] = [20000] * 8
    assert len((json.dumps(checkpoint, indent=2) + "\n").encode("utf-8")) <= 2048
    assert "failure_flow" in safe_self_test_progress(checkpoint, job_id=checkpoint["job_id"])
    state["persist_self_test_failure_progress"](ValueError("PRIVATE_OTHER_FAILURE"))
    assert "failure_flow" not in checkpoints[-1]


def bind_slate_wait(samples, *, now=100.2):
    """Execute the repository's nested deadline/predicate with a finite sample driver."""
    namespace = load_self_test()
    source = ast.parse(Path(namespace["__file__"]).read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "wait_slate_with_live_srt"
    ]
    assert len(functions) == 1
    calls = []

    def wait(_description, predicate, timeout, *, not_before):
        calls.append((timeout, not_before))
        for current in samples:
            # Mirror the existing Observer's complete/fresh eligibility gate;
            # the actual nested predicate must enforce the immutable cutoff.
            if (
                namespace["complete_metrics_sample"](current)
                and current["t"] >= max(now, not_before)
                and predicate(current)
            ):
                return current
        raise namespace["TestFailure"]("timed out waiting for fixture")

    namespace.update(
        observer=SimpleNamespace(wait_sample=wait), time=SimpleNamespace(monotonic=lambda: now)
    )
    module = ast.Module(body=[deepcopy(functions[0])], type_ignores=[])
    exec(compile(module, str(namespace["__file__"]), "exec"), namespace)  # noqa: S102
    return namespace["wait_slate_with_live_srt"], namespace, calls


def slate_sample(timestamp=107.8, **updates):
    return flow_sample(timestamp, live=False, normalized=False, **updates)


def test_hard_cut_accepts_complete_slate_strictly_before_existing_idle_lower_bound():
    current = slate_sample(finished=107.999)
    wait, namespace, calls = bind_slate_wait([current])
    assert wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True) is current
    assert calls == [(pytest.approx(7.8), 100.0)]
    assert namespace["SRT_IDLE_LOWER_BOUND_SECONDS"] == 8
    assert namespace["SRT_IDLE_UPPER_BOUND_SECONDS"] == 13
    assert namespace["LIVE_TO_SLATE_DEADLINE_SECONDS"] == 4.5
    assert namespace["CAPTURE_NO_GROWTH_LIMIT_SECONDS"] == 3


@pytest.mark.parametrize(
    "finished", [108.0, 108.001, None, True, float("nan"), float("inf"), 107.0]
)
def test_hard_cut_rejects_late_incomplete_or_invalid_sample_even_when_start_is_early(finished):
    wait, namespace, _calls = bind_slate_wait([slate_sample(finished=finished)])
    with pytest.raises(namespace["TestFailure"], match="timed out"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True)


@pytest.mark.parametrize(
    "updates",
    [
        {"t": 99.9, "finished": 100.3},
        {"t": 100.1, "finished": 100.3},
        {"dut_metrics_ok": False},
        {"sink_metrics_ok": False},
        {"path_ready": False},
    ],
)
def test_hard_cut_rejects_stale_or_incomplete_slate_evidence(updates):
    current = slate_sample()
    current.update(updates)
    wait, namespace, _calls = bind_slate_wait([current])
    with pytest.raises(namespace["TestFailure"], match="timed out"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True)


@pytest.mark.parametrize(
    "updates",
    [
        {"ingest_ids": ["PRIVATE_REPLACEMENT"]},
        {"ingest_ids": []},
        {"ingest_live": False},
    ],
)
def test_hard_cut_fails_on_first_observed_original_connection_loss(updates):
    current = slate_sample()
    current.update(updates)
    wait, namespace, _calls = bind_slate_wait([current, slate_sample()])
    with pytest.raises(namespace["TestFailure"], match="disappeared before SLATE"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True)


@pytest.mark.parametrize("identity", [None, [], ["a", "b"], [""]])
def test_hard_cut_requires_the_original_single_connection(identity):
    wait, namespace, calls = bind_slate_wait([slate_sample()])
    with pytest.raises(namespace["TestFailure"], match="requires the original SRT"):
        wait("fixture", 100.0, identity, hard_source_cut=True)
    assert not calls


def test_hard_cut_cutoff_never_slides_with_growth_or_wait_start():
    samples = [
        flow_sample(101.0),
        flow_sample(104.0, normalized_bytes=999999),
        flow_sample(107.5, normalized_bytes=1999999),
        slate_sample(108.1),
    ]
    wait, namespace, calls = bind_slate_wait(samples, now=104.0)
    with pytest.raises(namespace["TestFailure"], match="timed out"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True)
    assert calls == [(4.0, 100.0)]
    wait, namespace, calls = bind_slate_wait([slate_sample()], now=108.0)
    with pytest.raises(namespace["TestFailure"], match="exceeded the SLATE deadline"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"], hard_source_cut=True)
    assert not calls


def test_non_hard_cut_faults_keep_the_original_four_point_five_second_bound():
    wait, namespace, calls = bind_slate_wait([slate_sample(104.6)])
    with pytest.raises(namespace["TestFailure"], match="exceeded the SLATE deadline"):
        wait("fixture", 100.0, ["PRIVATE_INGEST_ID"])
    assert calls == [(pytest.approx(4.3), 100.0)]
    source = ast.parse(Path(namespace["__file__"]).read_text(encoding="utf-8"))
    selected = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "wait_slate_with_live_srt"
        and any(
            key.arg == "hard_source_cut" and ast.literal_eval(key.value) for key in node.keywords
        )
    ]
    assert len(selected) == 2
    assert {ast.unparse(node.args[1]) for node in selected} == {"outage_started", "final_started"}


def test_actual_watchdog_valid_metrics_can_outlive_old_source_cut_oracle():
    namespace = load_normalizer()
    fixture = load_self_test()
    final_output_growth = 2.0  # Buffered output after a hard network cut at zero.
    watchdog = namespace["MediaWatchdog"](("output", 100), final_output_growth)
    read_seconds = 0.190
    assert read_seconds < namespace["METRICS_REQUEST_TIMEOUT_SECONDS"]
    now = final_output_growth
    kept_until = []
    for _ in range(20):
        now += read_seconds
        keep, probe = watchdog.observe_output(True, ("output", 100), now)
        if keep and probe:
            started = now
            now += read_seconds
            keep = watchdog.observe_ingest(True, ("input", 100), started, now)
        if not keep:
            break
        kept_until.append(now)
        now += namespace["MEDIA_POLL_INTERVAL_SECONDS"]
    assert watchdog.joint_idle_since == pytest.approx(2.380)
    assert kept_until[-1] == pytest.approx(4.530)
    assert now == pytest.approx(4.770)
    assert watchdog.failure_reason == "output-fallback"
    # Kill/disconnect/Observer publication have not happened yet. The old
    # source-cut oracle has already failed despite unchanged correct policy.
    assert fixture["LIVE_TO_SLATE_DEADLINE_SECONDS"] < kept_until[-1]
    assert now < fixture["SRT_IDLE_LOWER_BOUND_SECONDS"]
    assert namespace["VERIFIED_STALL_TIMEOUT_SECONDS"] == 2.0
    assert namespace["OUTPUT_IDLE_FALLBACK_SECONDS"] == 2.5
