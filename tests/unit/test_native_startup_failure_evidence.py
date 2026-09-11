"""Startup failure evidence survives cleanup without exposing arbitrary log text."""

from __future__ import annotations

import ast
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_normalizer, load_self_test

from scripts import ci_node_onboarding_smoke as smoke

PREFIX = b"moblin-relay-normalize:startup:"
MARKER = b"moblin-relay-normalize:restart:output-start-timeout\n"
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
JOB_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


@pytest.fixture
def record():
    diagnostic = load_normalizer()["StartupDiagnostics"](10.0)
    diagnostic.spawn_finished(10.02)
    diagnostic.observe_output(True, None, 10.05, 10.1)
    diagnostic.observe_output(True, (SOURCE_ID, 10), 11.0, 11.1)
    diagnostic.observe_output(True, (SOURCE_ID, 20), 11.2, 11.3)
    diagnostic.observe_output(True, (SOURCE_ID, 30), 11.4, 11.5)
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        diagnostic.emit_timeout(16.0)
    encoded = captured.getvalue().encode("ascii")
    assert encoded.startswith(PREFIX) and encoded.endswith(b"\n")
    return json.loads(encoded[len(PREFIX) :])


def log_record(record):
    return PREFIX + json.dumps(record, separators=(",", ":")).encode("ascii") + b"\n"


def checkpoint_payload(record):
    return {
        "job_id": JOB_ID,
        "stage": "live-normalize",
        "elapsed_seconds": 103.185,
        "failure_initial_live_reason": "output-start-timeout",
        "failure_startup": record,
    }


def test_runtime_schema_matches_independent_self_test_and_ci_projections(record):
    namespace = load_self_test()
    assert namespace["safe_startup_failure"](record) == record
    assert smoke.safe_startup_failure(record) == record
    assert namespace["initial_live_startup_diagnostic"](log_record(record) + MARKER) == record
    payload = checkpoint_payload(record)
    projected = smoke.safe_self_test_progress(payload, job_id=JOB_ID)
    assert projected["failure_startup"] == record
    assert projected["failure_startup"] is not record
    assert "PRIVATE" not in json.dumps(projected)
    assert SOURCE_ID not in json.dumps(projected)
    assert len(json.dumps(payload, separators=(",", ":")).encode()) < 2048


def test_projection_schema_implementations_remain_identical():
    root = Path(__file__).resolve().parents[2]
    definitions = []
    for path in (
        root / "deploy/moblin-relay/self-test",
        root / "scripts/ci_node_onboarding_smoke.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions.append(
            next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "safe_startup_failure"
            )
        )
    assert ast.dump(definitions[0], include_attributes=False) == ast.dump(
        definitions[1], include_attributes=False
    )


@pytest.mark.parametrize(
    "gate", ["wait_initial_live_bridge_active", "require_initial_live_log_clean"]
)
def test_original_fatal_gate_binds_record_to_same_exception_and_checkpoint(
    monkeypatch, record, gate
):
    namespace = load_self_test()
    function = namespace[gate]
    scope = function.__globals__
    tail = b"PRIVATE unrelated log\n" + log_record(record) + MARKER
    monkeypatch.setitem(scope, "read_validated_log_tail", lambda *_args: tail)
    with pytest.raises(namespace["TestFailure"]) as caught:
        function(1, 2, 0)
    assert str(caught.value) == "initial LIVE log gate failed: output-start-timeout"
    assert scope["SELF_TEST_INITIAL_LIVE_FAILURE"] == (caught.value, "output-start-timeout")
    assert scope["SELF_TEST_STARTUP_FAILURE"] == (caught.value, record)
    checkpoints = []
    monkeypatch.setitem(scope, "SELF_TEST_STAGE_FILE", "unused-fixture-stage")
    monkeypatch.setitem(
        scope,
        "SELF_TEST_LAST_PROGRESS",
        {
            "job_id": JOB_ID,
            "stage": "live-normalize",
            "elapsed_seconds": 103.185,
        },
    )
    monkeypatch.setitem(scope, "mark_self_test_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        scope, "atomic_json", lambda _path, value, **kwargs: checkpoints.append((value, kwargs))
    )
    namespace["persist_self_test_failure_progress"](caught.value)
    checkpoint, options = checkpoints[-1]
    assert checkpoint["failure_startup"] == record and options == {"compact": True}
    assert len(json.dumps(checkpoint, separators=(",", ":")).encode()) < 2048
    assert smoke.safe_self_test_progress(checkpoint, job_id=JOB_ID)["failure_startup"] == record
    assert "PRIVATE" not in json.dumps(checkpoint)
    namespace["persist_self_test_failure_progress"](namespace["TestFailure"](str(caught.value)))
    assert "failure_startup" not in checkpoints[-1][0]
    scope["SELF_TEST_LAST_PROGRESS"]["stage"] = "outage-normal"
    namespace["persist_self_test_failure_progress"](caught.value)
    assert "failure_startup" not in checkpoints[-1][0]


@pytest.mark.parametrize(
    "replacement",
    ["PRIVATE_KEY", {"key": "PRIVATE_KEY"}, ["PRIVATE_KEY"], float("inf"), float("nan")],
)
def test_every_field_rejects_strings_containers_and_nonfinite_values(record, replacement):
    namespace = load_self_test()
    for field in record:
        damaged = {**record, field: replacement}
        assert namespace["safe_startup_failure"](damaged) is None
        assert smoke.safe_startup_failure(damaged) is None
        assert namespace["initial_live_startup_diagnostic"](log_record(damaged) + MARKER) is None
        assert smoke.safe_self_test_progress(checkpoint_payload(damaged), job_id=JOB_ID) == {
            "progress": "unavailable"
        }


@pytest.mark.parametrize(
    "updates",
    [
        {"unexpected_secret": "PRIVATE_KEY"},
        {"version": True},
        {"version": 2},
        {"reads": True},
        {"reads": -1},
        {"reads": 1025},
        {"reads": 2.0},
        {"spawn_ms": True},
        {"spawn_ms": -1},
        {"spawn_ms": 600001},
        {"video_frames": True},
        {"video_frames": -1},
        {"video_frames": 1025},
        {"clock_valid": False},
        {"overflow": 0},
    ],
)
def test_invalid_schema_or_bounds_are_rejected(record, updates):
    damaged = {**record, **updates}
    namespace = load_self_test()
    assert namespace["safe_startup_failure"](damaged) is None
    assert smoke.safe_startup_failure(damaged) is None
    assert namespace["initial_live_startup_diagnostic"](log_record(damaged) + MARKER) is None


def test_unknown_clock_can_be_preserved_only_with_null_durations(record):
    unknown = {name: None if name.endswith("_ms") else value for name, value in record.items()}
    unknown["clock_valid"] = False
    unknown["video_frames"] = None
    assert load_self_test()["safe_startup_failure"](unknown) == unknown
    assert smoke.safe_startup_failure(unknown) == unknown


@pytest.mark.parametrize(
    "stage,reason",
    [("live-normalize", "child-exit"), ("norm-publish", "output-start-timeout"), ("cleanup", None)],
)
def test_ci_rejects_unrelated_stage_or_failure(record, stage, reason):
    payload = {**checkpoint_payload(record), "stage": stage, "failure_initial_live_reason": reason}
    assert smoke.safe_self_test_progress(payload, job_id=JOB_ID) == {"progress": "unavailable"}


def test_parser_requires_complete_exact_lines_before_first_timeout(record):
    parse = load_self_test()["initial_live_startup_diagnostic"]
    valid = log_record(record)
    for tail in (
        valid,
        MARKER + valid,
        valid + MARKER.rstrip(b"\n"),
        valid + b"PRIVATE " + MARKER,
        b"PRIVATE " + valid + MARKER,
        valid + MARKER.rstrip(b"\n") + b" PRIVATE\n",
        PREFIX + b" " * 769 + b"\n" + MARKER,
        PREFIX + b"{invalid}\n" + MARKER,
        PREFIX + b'{"version":"PRIVATE",' + valid[len(PREFIX) + 1 :] + MARKER,
        b"x" * (1024 * 1024 + 1) + valid + MARKER,
    ):
        assert parse(tail) is None
    assert parse(valid + MARKER + log_record({**record, "video_frames": 20}) + MARKER) == record
    # A malformed latest record is not silently replaced with a previous valid one.
    assert parse(valid + PREFIX + b"PRIVATE\n" + MARKER) is None


def test_optional_parser_exception_never_changes_original_failure(monkeypatch, record):
    namespace = load_self_test()
    function = namespace["initial_live_gate_failure"]

    def broken(*_args):
        raise RuntimeError("PRIVATE")

    monkeypatch.setitem(function.__globals__, "initial_live_startup_diagnostic", broken)
    failure = function("output-start-timeout", tail=log_record(record) + MARKER)
    assert str(failure) == "initial LIVE log gate failed: output-start-timeout"
    assert function.__globals__["SELF_TEST_STARTUP_FAILURE"] is None


@pytest.mark.parametrize("valid", [True, False])
def test_ci_reader_transfers_only_projected_record(monkeypatch, capsys, record, valid):
    candidate = record if valid else {**record, "video_frames": "PRIVATE_KEY"}

    def compose(*args, **kwargs):
        assert "'failure_startup'" in args[-1]
        assert "before.st_size <= 2048" in args[-1]
        assert kwargs["max_capture_bytes"] == 4096
        return SimpleNamespace(stdout=json.dumps(checkpoint_payload(candidate)))

    monkeypatch.setattr(smoke, "compose", compose)
    smoke.print_self_test_progress(JOB_ID)
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    assert ('"failure_startup"' in output) is valid
