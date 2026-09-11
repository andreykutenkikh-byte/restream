"""Optional initial-window evidence is bounded and cannot change media acceptance."""

from __future__ import annotations

import ast
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test
from test_native_startup_failure_evidence import JOB_ID, checkpoint_payload
from test_native_startup_failure_evidence import record as startup_record_fixture

from scripts import ci_node_onboarding_smoke as smoke

SOURCE = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-2222-4333-8444-555555555555"
record = startup_record_fixture


def metrics(identity=SOURCE, value=100):
    if identity is None:
        return ""
    labels = f'{{id="{identity}",state="read",path="iphone-live",remoteAddr="127.0.0.1:1234"}}'
    return "\n".join(
        f"{name}{labels} {counter}"
        for name, counter in (
            ("rtsp_sessions", 1),
            ("rtsp_sessions_outbound_bytes", value),
            ("rtsp_sessions_outbound_rtp_packets", value),
        )
    )


def sample(t, counter=100, *, output=False, identity=SOURCE):
    return {
        "t": t,
        "finished": t + 0.05,
        "dut_metrics_ok": True,
        "ingest_live": True,
        "ingest_ids": [identity],
        "ingest_bytes": counter,
        "ingest_transport_bytes": counter,
        "normalized": output,
        "normalized_ids": [OTHER] if output else [],
    }


def evidence(clock=lambda: {"state": "unknown"}):
    return load_self_test()["StartupInputEvidence"](10.0, clock)


@pytest.fixture
def summary():
    value = evidence(lambda: {"state": "known", "ratio": 0.99, "max_gap": 0.1, "last_age": 0.02})
    for index in range(10):
        value.observe(
            sample(10 + index * 0.2, 100 + index, output=index == 9), metrics(value=100 + index)
        )
    return value.summary()


def test_first_epoch_growth_and_pre_output_clock_are_not_replaced_by_retry():
    clock = {"state": "known", "ratio": 0.5, "max_gap": 0.2, "last_age": 0.1}
    value = evidence(lambda: clock)
    value.observe(sample(10), metrics(value=100))
    value.observe(sample(10.2, 110), metrics(value=110))
    value.observe(sample(10.4, 120), metrics(identity=None))
    clock["ratio"] = 1.0
    value.observe(sample(10.6, 130), metrics(identity=OTHER, value=1000))
    value.observe(sample(10.8, 140, output=True), metrics(identity=OTHER, value=2000))
    clock["ratio"] = 4.0
    value.observe(sample(11, 150), metrics(value=9999))
    result = value.summary()
    assert result["complete"] is True  # Coverage, NOT evidence about an exact failed child.
    assert result["scope"] == "first-ingest-to-first-rtmp"
    assert result["channels"]["rtsp_rtp"] == {
        "state": "changed",
        "first_growth_ms": [0, 250],
        "last_growth_ms": [0, 250],
    }
    assert result["channels"]["srt"]["last_growth_ms"] == [600, 850]
    assert result["clock"]["adjacent_metrics_ms"] == [600, 650]
    assert result["clock"]["ratio"] == [0.5, 1.0]
    assert SOURCE not in json.dumps(result) and OTHER not in json.dumps(result)


@pytest.mark.parametrize(
    "bad", [None, "rtsp_sessions{path=bad} NaN", metrics() + "\n" + metrics(), metrics(value=-1)]
)
def test_optional_metrics_failure_only_downgrades_evidence(bad):
    value = evidence()
    original = sample(10)
    before = copy.deepcopy(original)
    value.observe(original, bad)
    value.observe(sample(10.2, 200, output=True), metrics(value=200))
    result = value.summary()
    assert original == before
    assert result["complete"] is False
    assert all(row["state"] == "unknown" for row in result["channels"].values())


@pytest.mark.parametrize("change", ["id", "regression"])
def test_source_counter_epoch_is_never_merged(change):
    value = evidence()
    value.observe(sample(10), metrics())
    value.observe(sample(10.2, 110), metrics(value=110))
    value.observe(
        sample(
            10.4, 0 if change == "regression" else 120, identity=OTHER if change == "id" else SOURCE
        ),
        metrics(),
    )
    value.observe(sample(10.6, 1000, output=True), metrics(value=1000))
    channel = value.summary()["channels"]["srt"]
    assert channel["state"] == ("changed" if change == "id" else "unknown")
    assert channel["last_growth_ms"] == ([0, 250] if change == "id" else None)


@pytest.mark.parametrize("mode", ["time", "count", "gap", "single", "no-ingest", "clock-exception"])
def test_collection_bounds_and_unknown_are_explicit(mode):
    def broken():
        raise RuntimeError("PRIVATE")

    value = evidence(broken if mode == "clock-exception" else lambda: {"state": "unknown"})
    if mode == "no-ingest":
        value.observe({**sample(10), "ingest_live": False}, metrics())
    else:
        value.observe(sample(10, output=mode == "single"), metrics())
        if mode == "time":
            value.observe(sample(30.01), metrics())
        elif mode == "count":
            for index in range(1, 130):
                value.observe(sample(10 + index * 0.06), metrics())
        elif mode == "gap":
            value.observe(sample(14, output=True), metrics())
    result = value.summary()
    assert result["complete"] is False
    assert result["samples"] <= 128
    assert result["window_clipped"] is (mode in {"time", "count"})
    assert result["clock"] == {"state": "unknown"}
    assert load_self_test()["safe_startup_input_failure"](result) == result


def test_strict_schema_is_identical_and_fits_combined_checkpoint(summary, record):
    root = Path(__file__).resolve().parents[2]
    nodes = []
    for path in (
        root / "deploy/moblin-relay/self-test",
        root / "scripts/ci_node_onboarding_smoke.py",
    ):
        nodes.append(
            next(
                node
                for node in ast.parse(path.read_text(encoding="utf-8")).body
                if isinstance(node, ast.FunctionDef) and node.name == "safe_startup_input_failure"
            )
        )
    assert ast.dump(nodes[0], include_attributes=False) == ast.dump(
        nodes[1], include_attributes=False
    )
    payload = {
        **checkpoint_payload(record),
        "failure_lines": [20000] * 8,
        "failure_startup_input": summary,
    }
    assert len(json.dumps(payload, separators=(",", ":")).encode()) < 2048
    assert smoke.safe_self_test_progress(payload, job_id=JOB_ID)["failure_startup_input"] == summary


@pytest.mark.parametrize(
    "bad", [[], {}, "PRIVATE", True, -1, 129, 10**310, float("inf"), float("nan")]
)
def test_hostile_fields_are_total_and_optional_only(summary, record, bad):
    namespace = load_self_test()
    damaged_values = [{**summary, name: bad} for name in summary]
    damaged_values += [
        {
            **summary,
            "channels": {
                **summary["channels"],
                "srt": {**summary["channels"]["srt"], "state": bad},
            },
        }
    ]
    damaged_values += [{**summary, "clock": {**summary["clock"], "ratio": [bad, bad]}}]
    for damaged in damaged_values:
        # Legitimate bool replacements are still permitted for the two boolean fields.
        result = namespace["safe_startup_input_failure"](damaged)
        assert smoke.safe_startup_input_failure(damaged) == result
        projected = smoke.safe_self_test_progress(
            {**checkpoint_payload(record), "failure_startup_input": damaged}, job_id=JOB_ID
        )
        assert projected["failure_startup"] == record
        assert "PRIVATE" not in json.dumps(projected)


def test_binding_is_exact_exception_and_budget_fallback_preserves_fatal(
    monkeypatch, summary, record
):
    namespace = load_self_test()
    scope = namespace["persist_self_test_failure_progress"].__globals__
    failure = namespace["TestFailure"]("original fatal")
    namespace["bind_startup_input_failure"](failure, SimpleNamespace(summary=lambda: summary))
    saved = []
    monkeypatch.setitem(scope, "SELF_TEST_STAGE_FILE", "fixture")
    monkeypatch.setitem(scope, "SELF_TEST_LAST_PROGRESS", checkpoint_payload(record))
    monkeypatch.setitem(scope, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(scope, "atomic_json", lambda _path, value, **_kwargs: saved.append(value))
    namespace["persist_self_test_failure_progress"](failure)
    assert saved[-1]["failure_startup_input"] == summary
    namespace["persist_self_test_failure_progress"](namespace["TestFailure"]("original fatal"))
    assert "failure_startup_input" not in saved[-1]
    scope["SELF_TEST_LAST_PROGRESS"] = {**checkpoint_payload(record), "fixture_padding": "x" * 1300}
    namespace["persist_self_test_failure_progress"](failure)
    assert "failure_startup_input" not in saved[-1]
    assert saved[-1]["failure_startup"] == record
    assert len(json.dumps(saved[-1], separators=(",", ":")).encode()) < 2048
    scope["SELF_TEST_LAST_PROGRESS"] = {**checkpoint_payload(record), "stage": "outage-normal"}
    namespace["persist_self_test_failure_progress"](failure)
    assert "failure_startup_input" not in saved[-1]


def test_ci_reader_rejects_duplicate_keys_and_transfers_only_safe_projection(
    monkeypatch, capsys, summary, record
):
    def compose(*args, **kwargs):
        code = args[-1]
        assert "object_pairs_hook=unique_pairs" in code
        tree = ast.parse(code)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
        namespace = {}
        exec(  # noqa: S102 - execute only the fixed local reader's pure duplicate-key validator
            compile(ast.Module(body=[function], type_ignores=[]), "fixture", "exec"), namespace
        )
        with pytest.raises(ValueError):
            json.loads(
                '{"clock":{"state":"unknown","state":"PRIVATE"}}',
                object_pairs_hook=namespace["unique_pairs"],
            )
        assert "'failure_startup_input'" in code and "before.st_size <= 2048" in code
        assert kwargs["max_capture_bytes"] == 4096
        return SimpleNamespace(
            stdout=json.dumps({**checkpoint_payload(record), "failure_startup_input": summary})
        )

    monkeypatch.setattr(smoke, "compose", compose)
    smoke.print_self_test_progress(JOB_ID)
    output = capsys.readouterr().out
    assert '"failure_startup_input"' in output and "PRIVATE" not in output


def test_observer_optional_callback_failure_does_not_change_metrics_or_fetches(monkeypatch):
    namespace = load_self_test()
    observer_class = namespace["Observer"]
    scope = observer_class.run.__globals__
    results = []
    for callback_enabled in (False, True):
        observer = object.__new__(observer_class)
        observer.dut = observer.sink = observer.reader = SimpleNamespace(poll=lambda: 0)
        observer.lock = threading.Lock()
        observer.capture = SimpleNamespace(exists=lambda: False)
        observer.samples = []
        stopped = [False]
        observer.stop_event = SimpleNamespace(
            is_set=lambda stopped=stopped: stopped[0],
            wait=lambda _delay, stopped=stopped: stopped.__setitem__(0, True),
        )
        calls = []
        monkeypatch.setitem(
            scope, "fetch_metrics", lambda port, calls=calls: calls.append(port) or metrics()
        )
        monkeypatch.setattr(scope["time"], "monotonic", lambda: 10.0)

        def broken(sample_value, payload, observer=observer):
            assert observer.samples[-1] is sample_value and payload == metrics()
            raise RuntimeError("PRIVATE optional failure")

        observer.startup_input_observer = broken if callback_enabled else None
        observer.run()
        results.append((observer.samples, calls))
    assert results[0] == results[1]
    assert len(results[0][1]) == 2  # Existing DUT + sink requests only.
    assert results[0][0][0]["dut_metrics_ok"] is True


def test_unknown_keys_and_legacy_clock_interval_are_rejected(summary):
    for field in ("PRIVATE", "last_ms"):
        damaged = copy.deepcopy(summary)
        damaged["clock"][field] = "PRIVATE_KEY"
        assert load_self_test()["safe_startup_input_failure"](damaged) is None
        assert smoke.safe_startup_input_failure(damaged) is None


def test_optional_binding_and_persistence_exceptions_do_not_replace_fatal(monkeypatch):
    namespace = load_self_test()
    original = namespace["TestFailure"]("original fatal")

    def broken(*_args):
        raise RuntimeError("PRIVATE")

    scope = namespace["bind_startup_input_failure"].__globals__
    namespace["bind_startup_input_failure"](original, SimpleNamespace(summary=broken))
    assert scope["SELF_TEST_STARTUP_INPUT_FAILURE"] is None
    monkeypatch.setitem(scope, "SELF_TEST_STARTUP_INPUT_FAILURE", (original, {}))
    monkeypatch.setitem(scope, "safe_startup_input_failure", broken)
    monkeypatch.setitem(scope, "SELF_TEST_STAGE_FILE", "fixture")
    monkeypatch.setitem(scope, "SELF_TEST_LAST_PROGRESS", {"stage": "live-normalize"})
    monkeypatch.setitem(scope, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    saved = []
    monkeypatch.setitem(scope, "atomic_json", lambda _path, value, **_kwargs: saved.append(value))
    namespace["persist_self_test_failure_progress"](original)
    assert saved == [{"stage": "live-normalize", "failure_lines": []}]
