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
    state = {"initial_completed": True, "target_armed": True, "target_completed": True}
    value = API["prefix_summary"](original, {}, state, 1)
    assert value["status"] == "TARGET_CRASH_PREFIX_COMPLETED"
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
    assert value["status"] == "ORIGINAL_PREFIX_FAILURE" and value["initial_start_timeout"]
    assert value["initial_startup"]["elapsed_ms"] == 6026
    assert value["initial_startup"]["reads"] == value["initial_startup"]["absent"] == 119
    assert not value["target_armed"] and not value["target_completed"]


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


def test_failure_checkpoint_preserves_exact_code_location_without_exception_text():
    result = API["failure_location"](
        {"stage": "assets", "failure_lines": [5211, 583], "failure": "PRIVATE"},
        {"assets", "live-normalize"},
    )
    assert result == {"stage": "assets", "failure_lines": [5211, 583]}
    for value in (
        {"stage": "PRIVATE", "failure_lines": [True]},
        {"stage": [], "failure_lines": ["PRIVATE"]},
        {"failure_lines": list(range(1, 10))},
        {"failure_lines": [0, 20001]},
    ):
        assert API["failure_location"](value, {"assets"}) == {"stage": None, "failure_lines": []}


def test_prefix_preserves_initial_stages_and_original_cleanup(tmp_path):
    calls = []
    test_root = tmp_path / "tests"
    test_root.mkdir()

    class Failure(Exception):
        pass

    fixture = {
        "mark_self_test_stage": lambda name, **kwargs: calls.append((name, kwargs)),
        "write_configs": lambda *args, **kwargs: None,
        "MediaFailureDiagnostics": object,
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
    fixture["mark_self_test_stage"]("auth-exclusive")
    assert calls[-1][0] == "auth-exclusive" and state["initial_completed"]
    assert not state["target_armed"] and not state["target_completed"]
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


def test_ci_diagnostic_is_independent_contained_and_retains_original_main_gates():
    import yaml

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow)["jobs"]
    main = jobs["test"]
    diagnostic = jobs["native-startup-diagnostic"]
    assert "needs" not in diagnostic and "if" not in diagnostic
    assert diagnostic["runs-on"] == main["runs-on"] == "ubuntu-latest"
    assert diagnostic["timeout-minutes"] == 12
    assert diagnostic["steps"][0] == main["steps"][0]
    main_steps = {step.get("name"): step for step in main["steps"]}
    assert "First-child native startup diagnostic" not in main_steps
    assert main_steps["SSH bootstrap and native Moblin Relay end-to-end smoke"] == {
        "name": "SSH bootstrap and native Moblin Relay end-to-end smoke",
        "id": "native_onboarding",
        "run": "uv run --locked python scripts/ci_node_onboarding_smoke.py",
    }
    reader = main_steps["Strict native reader clock counterfactual"]
    assert reader["if"] == (
        "${{ !cancelled() && (success() || steps.native_onboarding.outcome == 'failure') }}"
    )
    assert "CI_NATIVE_READER_CLOCK=isolated-fixture" in reader["run"]
    assert "test-native-reader-clock.py" in reader["run"]
    assert main_steps["Post-onboarding runtime limits"]["run"] == (
        "sh scripts/check_runtime_limits.sh"
    )
    assert main_steps["Stop CI project"]["if"] == "always()"

    steps = {step.get("name"): step for step in diagnostic["steps"]}
    assert steps["Build exact native fixture image"]["run"] == (
        'docker build --file ci/ssh-target/Dockerfile --tag "$DIAGNOSTIC_IMAGE" .'
    )
    start = steps["Start isolated native diagnostic container"]["run"]
    target = yaml.safe_load((ROOT / "compose.ci.yml").read_text(encoding="utf-8"))["services"][
        "ci-ssh-target"
    ]
    assert f"--cpus {target['cpus']}" in start
    assert f"--memory {target['mem_limit']}" in start
    assert f"--pids-limit {target['pids_limit']}" in start
    assert all(f"--tmpfs {mount}" in start for mount in target["tmpfs"])
    assert "--init --user 0:0 --network none --restart no --stop-timeout 10" in start
    assert "install -d -m 0755 /run/lock" in start
    assert "--privileged" not in start and "--cap-add" not in start
    assert "--publish" not in start and "--volume" not in start and "--mount" not in start
    assert "docker inspect --format" in start and "isolated resource profile: PASS" in start
    stage = steps["Stage exact diagnostic sources"]["run"]
    assert "< deploy/moblin-relay/self-test" in stage
    assert 'docker exec --interactive "$DIAGNOSTIC_CONTAINER" python3 -c' in stage
    assert "target.chmod(0o600)" in stage and "target.read_bytes() == data" in stage
    run = steps["Post-crash first-child native startup diagnostic"]
    assert "if" not in run and "continue-on-error" not in run
    assert "timeout --signal=TERM --kill-after=15s 480s" in run["run"]
    assert "CI_NATIVE_STARTUP=isolated-fixture" in run["run"]
    assert "< deploy/moblin-relay/test-native-startup.py" in run["run"]
    cleanup = steps["Remove exact native diagnostic container"]
    assert cleanup["if"] == "always()"
    assert "name=^/${DIAGNOSTIC_CONTAINER}$" in cleanup["run"]
    assert '.Config.Labels "adojapan.ci.native-startup"' in cleanup["run"]
    assert 'docker container rm --force "$container_id"' in cleanup["run"]
    assert 'test -z "$(docker container ls' in cleanup["run"]
    assert "continue-on-error" not in workflow
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
