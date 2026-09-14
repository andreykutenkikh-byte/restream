from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"
SOURCE_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def diagnostic_type():
    return runpy.run_path(str(NORMALIZER), run_name="startup_diagnostic_test")["StartupDiagnostics"]


def read_record(capsys, diagnostic_type) -> dict:
    captured = capsys.readouterr()
    assert not captured.out
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(diagnostic_type.PREFIX)
    encoded = lines[0][len(diagnostic_type.PREFIX) :]
    assert len(encoded.encode("ascii")) <= diagnostic_type.MAX_PAYLOAD_BYTES
    record = json.loads(encoded)
    assert all(type(value) in (int, bool, type(None)) for value in record.values())
    assert SOURCE_ID not in encoded
    return record


def test_startup_timeout_discloses_only_bounded_timing_and_growth(capsys, diagnostic_type):
    diagnostic = diagnostic_type(10.0)
    diagnostic.spawn_finished(10.02)
    diagnostic.observe_output(True, None, 10.07, 10.10)
    diagnostic.observe_output(True, (SOURCE_ID, 100), 12.0, 12.04)
    diagnostic.observe_output(True, (SOURCE_ID, 200), 12.1, 12.15)
    diagnostic.observe_output(True, (SOURCE_ID, 300), 12.2, 12.25)
    assert not capsys.readouterr().err
    diagnostic.emit_timeout(16.0)
    record = read_record(capsys, diagnostic_type)
    assert record == {
        "version": 1,
        "clock_valid": True,
        "overflow": False,
        "reads": 4,
        "failed": 0,
        "absent": 1,
        "present": 3,
        "identities": 1,
        "regressions": 0,
        "growth": 2,
        "spawn_ms": 20,
        "elapsed_ms": 6000,
        "post_spawn_ms": 5980,
        "first_output_ms": 2040,
        "last_output_age_ms": 3750,
        "first_growth_ms": 2150,
        "last_growth_age_ms": 3750,
        "max_read_ms": 50,
        "video_frames": 0,
        "video_age_ms": None,
    }
    diagnostic.emit_timeout(17.0)
    assert not capsys.readouterr().err


@pytest.mark.parametrize("successful", [True, False])
def test_startup_timeout_distinguishes_no_publisher_from_failed_metrics(
    capsys, diagnostic_type, successful
):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    for index in range(6):
        diagnostic.observe_output(successful, None, index + 0.05, index + 0.15)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is True
    assert record["reads"] == 6
    assert record["absent"] == (6 if successful else 0)
    assert record["failed"] == (0 if successful else 6)
    assert record["present"] == record["growth"] == 0
    assert record["first_output_ms"] is None
    assert record["last_output_age_ms"] is None
    assert record["first_growth_ms"] is None
    assert record["last_growth_age_ms"] is None


def test_growth_never_crosses_missing_metrics_identity_or_counter_reset(capsys, diagnostic_type):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    observations = (
        (True, (SOURCE_ID, 100)),
        (True, None),
        (True, (SOURCE_ID, 200)),
        (False, None),
        (True, (SOURCE_ID, 300)),
        (True, ("different-opaque-id", 400)),
        (True, ("different-opaque-id", 10)),
        (True, ("different-opaque-id", 20)),
    )
    for index, (successful, sample) in enumerate(observations, 1):
        diagnostic.observe_output(successful, sample, index / 10, index / 10 + 0.02)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["growth"] == 1
    assert record["regressions"] == 1
    assert record["identities"] == 4
    assert record["first_growth_ms"] == 820


@pytest.mark.parametrize(
    "bad_time", [float("nan"), float("inf"), -float("inf"), True, "secret", 10**500]
)
def test_invalid_clock_never_serializes_untrusted_or_nonfinite_values(
    capsys, diagnostic_type, bad_time
):
    diagnostic = diagnostic_type(bad_time)
    diagnostic.spawn_finished(0.1)
    diagnostic.observe_output(True, (SOURCE_ID, 1), 0.2, 0.3)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is False
    assert all(value is None for name, value in record.items() if name.endswith("_ms"))


@pytest.mark.parametrize("bad_end", [0.19, 601.0, float("nan"), float("inf")])
def test_reversed_or_out_of_bound_read_times_fail_closed(capsys, diagnostic_type, bad_end):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.1)
    diagnostic.observe_output(True, (SOURCE_ID, 1), 0.2, bad_end)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is False
    assert all(value is None for name, value in record.items() if name.endswith("_ms"))


@pytest.mark.parametrize(
    "bad_sample",
    [
        "rtmps://example.invalid/live#secret",
        ("", 1),
        (SOURCE_ID, True),
        (SOURCE_ID, -1),
        (SOURCE_ID, 2**63),
        ("secret" * 100, 1),
        (SOURCE_ID, 1, "secret"),
        [SOURCE_ID, 1],
    ],
)
def test_invalid_sample_never_escapes_fixed_schema(capsys, diagnostic_type, bad_sample):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    diagnostic.observe_output(True, bad_sample, 0.1, 0.2)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is False
    assert record["present"] == record["growth"] == 0


def test_all_counters_and_payload_are_capped(capsys, diagnostic_type):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    for index in range(diagnostic_type.MAX_COUNT + 2):
        diagnostic.observe_output(True, (SOURCE_ID, index), 0.1, 0.1)
    diagnostic.emit_timeout(6.0)
    record = read_record(capsys, diagnostic_type)
    assert record["reads"] == record["present"] == record["growth"] == diagnostic_type.MAX_COUNT
    assert record["overflow"] is True
    assert record["clock_valid"] is True


def test_missing_or_duplicate_spawn_return_is_unknown(capsys, diagnostic_type):
    diagnostic = diagnostic_type(0.0)
    diagnostic.observe_output(True, None, 0.1, 0.2)
    diagnostic.emit_timeout(6.0)
    assert read_record(capsys, diagnostic_type)["clock_valid"] is False
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    diagnostic.spawn_finished(0.02)
    diagnostic.emit_timeout(6.0)
    assert read_record(capsys, diagnostic_type)["clock_valid"] is False


@pytest.mark.parametrize("frames", [1, 180, 1024, 10**500])
def test_video_progress_is_separate_bounded_evidence(capsys, diagnostic_type, frames):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    diagnostic.observe_output(True, (SOURCE_ID, 100), 1.0, 1.1)
    diagnostic.emit_timeout(6.0, video_frames=frames, video_last_growth=4.5)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is True
    assert record["video_frames"] == min(frames, diagnostic_type.MAX_COUNT)
    assert record["video_age_ms"] == 1500
    assert record["overflow"] is (frames > diagnostic_type.MAX_COUNT)


@pytest.mark.parametrize(
    ("frames", "last_growth"),
    [
        (True, 1.0),
        (-1, 1.0),
        ("secret", 1.0),
        (1, float("nan")),
        (1, float("inf")),
        (1, "secret"),
        (1, -1),
        (1, 6.1),
        (0, 1.0),
        (1, None),
    ],
)
def test_invalid_video_progress_is_unknown(capsys, diagnostic_type, frames, last_growth):
    diagnostic = diagnostic_type(0.0)
    diagnostic.spawn_finished(0.01)
    diagnostic.emit_timeout(6.0, video_frames=frames, video_last_growth=last_growth)
    record = read_record(capsys, diagnostic_type)
    assert record["clock_valid"] is False
    assert record["video_age_ms"] is None
