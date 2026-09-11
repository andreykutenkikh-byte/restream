from __future__ import annotations

import copy
import json
import runpy
import shlex
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "deploy/moblin-relay/test-native-startup.py"
STAGE = PurePosixPath("/tmp/adojapan-ci-startup-abcdefgh")  # noqa: S108


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    monkeypatch.setitem(sys.modules, "resource", ModuleType("resource"))
    source = runpy.run_path(str(ROOT / "deploy/moblin-relay/self-test"), run_name="_hook_source")
    api = source["write_configs"].__globals__
    runner = runpy.run_path(str(RUNNER), run_name="_hook_runner")
    writes, files = [], {}

    def atomic(path, value):
        writes.append((path, copy.deepcopy(value)))
        files[path] = json.dumps(value).encode()

    api["atomic_json"] = atomic
    monkeypatch.setitem(runner["install_prefix"].__globals__, "read_private", files.__getitem__)
    return runner, api, writes, files


def install(runner, api, stage=STAGE):
    runner["install_prefix"](
        api, stage, PurePosixPath("/pinned/mediamtx"), PurePosixPath("/run/private.stage")
    )


def test_real_generated_configs_change_only_the_hook_and_keep_normalizer_identity(fixture):
    runner, api, writes, files = fixture
    original = api["write_configs"]
    install(runner, api)
    args = (STAGE / "work", "synthetic", "synthetic-password", "synthetic-passphrase", b"token")
    expected_paths = original(*args)
    expected = copy.deepcopy(writes)
    writes.clear()
    assert api["write_configs"](*args) == expected_paths
    assert len(writes) == 3
    assert writes[:2] == expected
    changed = json.loads(files[expected_paths[1]])
    hook = changed["paths"][api["INGEST_PATH"]]["runOnAvailable"]
    assert shlex.split(hook) == ["/usr/bin/python3", str(STAGE / "wrapper.py")]
    changed["paths"][api["INGEST_PATH"]]["runOnAvailable"] = str(STAGE / "wrapper.py")
    assert changed == expected[1][1]
    assert api["NORMALIZER"] == STAGE / "wrapper.py"


@pytest.mark.parametrize(
    "stage",
    ["/tmp/adojapan-ci-startup-bad;command", "/var/tmp/abcdefgh"],  # noqa: S108 - rejected fixtures
)
def test_hook_rejects_untrusted_stage_before_generating_files(fixture, stage):
    runner, api, writes, _ = fixture
    install(runner, api, PurePosixPath(stage))
    with pytest.raises(runner["DiagnosticFailure"], match="PRIVATE_HOOK_STAGE_PATH"):
        api["write_configs"]()
    assert writes == []


def test_hook_rejects_changed_original_anchor_without_rewriting_config(fixture):
    runner, api, writes, _ = fixture
    original = api["write_configs"]

    def changed(*args):
        api["NORMALIZER"] = PurePosixPath("/different/normalizer")
        return original(*args)

    api["write_configs"] = changed
    install(runner, api)
    with pytest.raises(runner["DiagnosticFailure"], match="PRIVATE_HOOK_ANCHOR_CHANGED"):
        api["write_configs"](STAGE / "work", "synthetic", "password", "passphrase", b"token")
    assert len(writes) == 2


def test_report_records_actual_mount_bit_and_fixed_interpreter_variant():
    source = RUNNER.read_text(encoding="utf-8")
    assert "stage_noexec = bool(os.statvfs(stage).f_flag & os.ST_NOEXEC)" in source
    assert 'report["stage_noexec"] = stage_noexec' in source
    assert 'report["hook_launch"] = "python-interpreter-noexec-compatible"' in source
