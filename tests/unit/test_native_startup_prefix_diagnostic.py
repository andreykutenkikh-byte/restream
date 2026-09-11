from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deploy/moblin-relay/test-native-startup.py"
API = runpy.run_path(str(SOURCE), run_name="_startup_prefix_unit")


def test_summary_requires_actual_planned_stop_and_cleanup():
    original = {"failure": API["STOP"], "workdir_removed": True, "secret_configs_wiped": 6}
    state = {"initial_completed": True}
    value = API["prefix_summary"](original, {}, state, 1)
    assert value["status"] == "PREFIX_OBSERVED_NO_STARTUP_FAILURE"
    assert value["acceptance"] is False
    for changes, code in (
        ({"failure": "media-failed"}, 1),
        ({"cleanup_failure": ["x"]}, 1),
        ({"workdir_removed": False}, 1),
        ({"secret_configs_wiped": True}, 1),
        ({}, 0),
    ):
        failed = API["prefix_summary"]({**original, **changes}, {}, state, code)
        assert failed["status"] == "ORIGINAL_PREFIX_FAILURE"


def test_original_timeout_cannot_be_hidden_by_later_recovery():
    result = {"failure": "initial LIVE failed", "workdir_removed": True, "secret_configs_wiped": 6}
    value = API["prefix_summary"](
        result,
        {
            "failure_initial_live_reason": "output-start-timeout",
            "failure_startup": {"elapsed_ms": 6026, "reads": 119, "absent": 119},
        },
        {"initial_completed": False},
        1,
    )
    assert value["status"] == "ORIGINAL_PREFIX_FAILURE" and value["first_start_timeout"]
    assert value["startup"]["elapsed_ms"] == 6026
    assert value["startup"]["reads"] == value["startup"]["absent"] == 119


@pytest.mark.parametrize(
    "value",
    [
        None,
        "SECRET",
        {"elapsed_ms": "SECRET", "reads": True},
        {"absent": -1, "present": 10**15, "raw": "rtmps://SECRET"},
    ],
)
def test_numeric_projection_never_exports_unknown_text_or_invalid_counts(value):
    result = API["numeric_startup"](value)
    assert all(item is None or type(item) is int for item in result.values())
    assert "SECRET" not in json.dumps(result) and "raw" not in result


def test_prefix_preserves_original_stage_call_and_stops_only_after_initial_live(tmp_path):
    calls = []
    test_root = tmp_path / "tests"
    test_root.mkdir()

    class Failure(Exception):
        pass

    fixture = {
        "mark_self_test_stage": lambda name, **kwargs: calls.append((name, kwargs)),
        "validate_test_root": lambda: None,
        "TEST_ROOT": test_root,
        "SELF_TEST_LOCK": "/run/lock/moblin-relay-self-test.lock",
        "TestFailure": Failure,
    }
    state = API["install_prefix"](
        fixture, tmp_path, Path("/pinned/mediamtx"), Path("/run/new.stage")
    )
    fixture["mark_self_test_stage"]("live-normalize")
    assert not state["initial_completed"]
    with pytest.raises(Failure, match=API["STOP"]):
        fixture["mark_self_test_stage"]("auth-exclusive")
    assert calls[-1][0] == "auth-exclusive" and state["initial_completed"]
    fixture["mark_self_test_stage"](
        "auth-exclusive"
    )  # Original failure checkpoint is not re-thrown.
    assert fixture["TEST_ROOT"] == test_root
    assert fixture["SELF_TEST_LOCK"] == "/run/lock/moblin-relay-self-test.lock"
    assert fixture["RESULT_FILE"].parent == tmp_path
    assert fixture["cleanup_stale_workdirs"]() == 0
    old = test_root / ".run-earlier-failed-attempt"
    old.mkdir()
    with pytest.raises(Failure, match="PRIOR_NATIVE_WORKDIR_REMAINS"):
        fixture["cleanup_stale_workdirs"]()
    assert old.is_dir()


def test_slate_profile_matches_native_installer_without_external_destination(tmp_path):
    command = API["slate_command"](tmp_path)
    for key, value in (
        ("-c:v", "libx264"),
        ("-profile:v", "main"),
        ("-pix_fmt", "yuv420p"),
        ("-r", "30"),
        ("-g", "60"),
        ("-t", "12"),
        ("-ar", "48000"),
        ("-ac", "2"),
    ):
        assert command[command.index(key) + 1] == value
    assert "color=c=0x111827:s=1080x1920:r=30:d=12" in command
    assert all("rtmp" not in item and "srt://" not in item for item in command)


def test_ci_adds_diagnostic_after_original_acceptance_and_retains_fatal_policy():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.index("SSH bootstrap and native") < workflow.index(
        "First-child native startup diagnostic"
    )
    assert "continue-on-error" not in workflow
    assert "timeout --signal=TERM --kill-after=15s 240s" in workflow
    assert "CI_NATIVE_STARTUP=isolated-fixture" in workflow
    code = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(code)
    install = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "install_prefix"
    )
    updated = {
        keyword.arg
        for node in ast.walk(install)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "update"
        for keyword in node.keywords
    }
    assert updated == {
        "NORMALIZER",
        "RENDERER",
        "SLATE",
        "MEDIAMTX",
        "RESULT_FILE",
        "SELF_TEST_PROGRESS_FILE",
        "SELF_TEST_STAGE_FILE",
    }
    assert "build_ffmpeg_argv" not in code and "OUTPUT_START_TIMEOUT_SECONDS" not in code
