from __future__ import annotations

import json
import runpy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy/moblin-relay"


@pytest.fixture
def runner():
    return runpy.run_path(str(BUNDLE / "test-native-startup.py"), run_name="_crash_target_test")


def test_arm_precedes_original_constructor_and_stop_follows_all_crash_stages(runner, monkeypatch):
    calls = []

    class Failure(Exception):
        pass

    class OriginalDiagnostics:
        def __init__(self, *args, **kwargs):
            calls.append(("original-constructor", args, kwargs))

    api = {
        "mark_self_test_stage": lambda name, **kwargs: calls.append(("mark", name)),
        "write_configs": lambda: None,
        "MediaFailureDiagnostics": OriginalDiagnostics,
        "TestFailure": Failure,
    }
    stage = PurePosixPath("/tmp/adojapan-ci-startup-abcdefgh")  # noqa: S108 - no filesystem use
    monkeypatch.setitem(
        runner["install_prefix"].__globals__,
        "save_new",
        lambda path, data: calls.append(("arm", path, data)),
    )
    state = runner["install_prefix"](api, stage, stage / "mediamtx", stage / "stage")
    for name in ("live-normalize", "auth-exclusive", "stuck-cont"):
        api["mark_self_test_stage"](name)
    assert state["initial_completed"] and not state["target_armed"]
    api["MediaFailureDiagnostics"]("stuck", stage, ignored_supervisor=None)
    assert not state["target_armed"]
    api["mark_self_test_stage"]("crash-death")
    api["MediaFailureDiagnostics"]("crash", stage, ignored_supervisor=321)
    assert calls[-2:] == [
        ("arm", stage / "normalizer-capture.arm", b"crash-first-child\n321\n"),
        ("original-constructor", ("crash", stage), {"ignored_supervisor": 321}),
    ]
    assert state["target_armed"] and not state["target_completed"]
    with pytest.raises(runner["DiagnosticFailure"], match="ARM_PRECONDITION"):
        api["MediaFailureDiagnostics"]("crash", stage, ignored_supervisor=321)
    for name in ("crash-live", "crash-cont"):
        api["mark_self_test_stage"](name)
        assert not state["target_completed"]
    with pytest.raises(Failure, match=runner["STOP"]):
        api["mark_self_test_stage"]("reset-start")
    assert calls[-1] == ("mark", "reset-start") and state["target_completed"]
    api["mark_self_test_stage"]("reset-start")  # Original failure checkpoint may repeat.


@pytest.mark.parametrize(
    "armed,pid,expected",
    [
        (None, 123, False),
        (b"crash-first-child\n123\n", 123, False),
        (b"crash-first-child\n0\n", 123, "invalid"),
        (b"crash-first-child\n2147483648\n", 123, "invalid"),
        (b"initial-first-child\n12\n", 123, "invalid"),
        (b"crash-first-child\n12\nPRIVATE", 123, "invalid"),
    ],
)
def test_unarmed_old_or_malformed_target_cannot_create_claim(monkeypatch, armed, pid, expected):
    helper = runpy.run_path(str(BUNDLE / "test-native-startup-normalizer.py"))

    def trusted(path, mode, limit):
        assert path.name == "normalizer-capture.arm" and mode == 0o600 and limit == 64
        if armed is None:
            raise FileNotFoundError
        return armed

    scope = helper["claim_capture"].__globals__
    monkeypatch.setitem(scope, "trusted_file", trusted)
    monkeypatch.setitem(scope, "os", SimpleNamespace(getpid=lambda: pid))
    if expected == "invalid":
        with pytest.raises(ValueError, match="ARM_INVALID"):
            helper["claim_capture"](PurePosixPath("/private"))
    else:
        assert helper["claim_capture"](PurePosixPath("/private")) is False


def test_target_report_distinguishes_early_failure_and_crash_failure(runner):
    result = {"failure": "PRIVATE", "workdir_removed": True, "secret_configs_wiped": 4}
    for armed in (False, True):
        state = {"initial_completed": armed, "target_armed": armed, "target_completed": False}
        report = runner["prefix_summary"](result, {}, state, 1)
        assert report["target_phase"] == "crash-first-child"
        assert report["target_armed"] is armed and not report["target_completed"]
        assert report["status"] == "ORIGINAL_PREFIX_FAILURE"
        assert "PRIVATE" not in json.dumps(report)
    assert runner["WORK_SECONDS"] == 480


def test_original_scenario_order_and_media_deadlines_are_not_replaced():
    source = (BUNDLE / "self-test").read_text(encoding="utf-8")
    context = source.index('"crash", work, dut_log_path')
    clock = source.index("supervisor_crash_started =", context)
    fault = source.index("if not signal_test_normalizer_supervisor(", clock)
    live = source.index('mark_self_test_stage("crash-live")', fault)
    continuation = source.index('mark_self_test_stage("crash-cont")', live)
    outcome = source.index('result["supervisor_crash_recovery"] =', continuation)
    stop = source.index('mark_self_test_stage("reset-start")', outcome)
    assert context < clock < fault < live < continuation < outcome < stop
    runner = (BUNDLE / "test-native-startup.py").read_text(encoding="utf-8")
    assert 'api["wait_healthy_live"]' not in runner
    assert 'api["signal_test_normalizer_supervisor"]' not in runner
    assert "SUPERVISOR_RESTART_TIMEOUT_SECONDS" not in runner
