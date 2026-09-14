"""Ready numeric capture evidence survives the isolated diagnostic report."""

from __future__ import annotations

import ast
import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import ci_node_onboarding_smoke as smoke

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "deploy/moblin-relay/test-native-startup.py"
API = runpy.run_path(str(RUNNER), run_name="_startup_projection_test")


def media():
    return {
        "scope": "capture",
        "elapsed_seconds": 15.003,
        "log_ok": True,
        "markers": {"active": 2},
        "first_seen": {"active": 1.0},
        "supervisor_count": 1,
        "child_count": 1,
        "supervisor_seen_seconds": 0.1,
        "child_seen_seconds": 0.2,
        "reader_input": True,
        "reader_output": True,
        "reader_frames": 17,
        "reader_input_seconds": 2.3,
        "reader_output_seconds": 2.4,
        "reader_first_frame_seconds": 3.0,
        "reader_last_frame_seconds": 7.1,
        "reader_probe_start_seconds": 0.02,
        "reader_probe_end_seconds": 2.2,
        "reader_media_seconds": 2.0,
        "reader_inspection_limited": True,
        "reader_nal_events": {
            "sps": {"count": 2, "first_seconds": 0.5, "last_seconds": 1.2},
            "idr": {"count": 1, "first_seconds": 1.1, "last_seconds": 1.1},
        },
        "source_clock": {
            "state": "known",
            "packets": 1000,
            "rebases": 0,
            "seconds": 15.0,
            "last_age": 0.003,
            "max_gap": 0.011,
            "discarded": 0.003,
            "ratio": 1.0,
        },
    }


def progress():
    return {
        "job_id": "test-job",
        "stage": "norm-flap",
        "elapsed_seconds": 160.0,
        "failure_lines": [6224, 5868, 5801, 5531, 4869],
        "failure_media": media(),
        "failure_flags": {"core_alive": True, "sink_growth": True, "state_ok": True},
        "failure_wait_seconds": 15.123,
    }


def test_compatibility_helpers_and_markers_cannot_drift_from_existing_ci_validators():
    actual = ast.parse(RUNNER.read_text(encoding="utf-8"))
    source = ast.parse((ROOT / "scripts/ci_node_onboarding_smoke.py").read_text(encoding="utf-8"))
    names = {"_diagnostic_seconds", "_safe_source_clock", "_safe_failure_media"}

    def bodies(tree):
        return {
            node.name: ast.dump(node, include_attributes=False)
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        }

    assert bodies(actual).keys() == names
    assert bodies(actual) == bodies(source)
    assert API["_MEDIA_DIAGNOSTIC_MARKERS"] == smoke._MEDIA_DIAGNOSTIC_MARKERS
    assert API["_MEDIA_FIRST_SEEN_MARKERS"] == smoke._MEDIA_FIRST_SEEN_MARKERS


def test_all_ready_media_fields_and_flags_survive_without_raw_values_or_mutation():
    value = progress()
    original = deepcopy(value)
    value.update(failure="PRIVATE_ERROR", sanitized_diagnostics={"raw": ["rtmps://PRIVATE"]})
    report = API["failure_location"](value, {"norm-flap"})
    expected = smoke.safe_self_test_progress(original, job_id="test-job")
    expected.pop("elapsed_seconds")
    assert report == expected
    assert "PRIVATE" not in json.dumps(report)
    assert value["failure_media"] == original["failure_media"]
    report["failure_media"]["markers"]["active"] = 99
    report["failure_media"]["reader_nal_events"]["sps"]["count"] = 99
    report["failure_flags"]["core_alive"] = False
    assert value["failure_media"] == original["failure_media"]
    assert value["failure_flags"] == original["failure_flags"]


@pytest.mark.parametrize(
    "changes",
    [
        {"reader_input": False, "reader_output": False, "reader_frames": 0},
        {"reader_input": True, "reader_output": False, "reader_frames": 0},
        {"reader_input": True, "reader_output": True, "reader_frames": 0},
    ],
)
def test_partial_timeout_progress_stays_distinct_from_missing_evidence(changes):
    evidence = {
        "scope": "capture",
        "elapsed_seconds": 15.0,
        "log_ok": False,
        "markers": {},
        "first_seen": {},
        **changes,
    }
    report = API["failure_location"]({**progress(), "failure_media": evidence}, {"norm-flap"})
    assert report["failure_media"] == smoke._safe_failure_media(evidence)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(raw="rtmps://PRIVATE"),
        lambda value: value.update(scope="PRIVATE"),
        lambda value: value.update(elapsed_seconds=10**310),
        lambda value: value.update(elapsed_seconds=float("nan")),
        lambda value: value.update(reader_frames=True),
        lambda value: value.update(reader_frames=10001),
        lambda value: value.update(reader_output=False),
        lambda value: value["markers"].update(PRIVATE=1),
        lambda value: value["source_clock"].update(ratio=float("inf")),
        lambda value: value["reader_nal_events"]["sps"].update(payload="PRIVATE"),
        lambda value: value["reader_nal_events"]["sps"].update(count=True),
        lambda value: value.update(reader_probe_end_seconds=16.0),
    ],
)
def test_malformed_media_is_omitted_but_original_failure_location_survives(change):
    value = progress()
    change(value["failure_media"])
    assert smoke._safe_failure_media(value["failure_media"]) is None
    report = API["failure_location"](value, {"norm-flap"})
    assert "failure_media" not in report
    assert report["stage"] == "norm-flap" and report["failure_lines"] == value["failure_lines"]
    assert report["failure_flags"] == value["failure_flags"]
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("flags", [None, {}, [], {"PRIVATE": True}, {"core_alive": 1}])
def test_malformed_flags_are_omitted_exactly_as_existing_schema_requires(flags):
    value = {**progress(), "failure_flags": flags}
    report = API["failure_location"](value, {"norm-flap"})
    assert "failure_flags" not in report
    assert report["failure_media"] == media()


@pytest.mark.parametrize("wait", [None, True, -1, 660.001, 10**310, float("nan"), "PRIVATE"])
def test_malformed_wait_is_omitted_without_changing_original_stage(wait):
    report = API["failure_location"]({**progress(), "failure_wait_seconds": wait}, {"norm-flap"})
    assert "failure_wait_seconds" not in report
    assert report["stage"] == "norm-flap" and report["failure_media"] == media()


def test_missing_invalid_stage_and_unrelated_capture_flow_never_reflect_values():
    value = progress()
    value.update(failure_flow={"capture_wait": {"raw": "PRIVATE"}}, capture_wait="PRIVATE")
    report = API["failure_location"](value, {"norm-flap"})
    assert "failure_flow" not in report and "capture_wait" not in report
    assert API["failure_location"]({**value, "stage": "PRIVATE"}, {"norm-flap"}) == {
        "stage": None,
        "failure_lines": value["failure_lines"],
    }
    assert API["failure_location"]({}, {"norm-flap"}) == {"stage": None, "failure_lines": []}


def test_diagnostic_report_path_uses_projection_without_new_io_or_waits():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "failure_location"
        for node in ast.walk(main)
    )
    for name in ("failure_location", "failure_evidence"):
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls <= {
            "type",
            "len",
            "all",
            "dict",
            "round",
            "_safe_failure_media",
            "_diagnostic_seconds",
            "failure_evidence",
        }
