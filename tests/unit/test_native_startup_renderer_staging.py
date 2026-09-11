from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest
from test_moblin_relay_bundle import load_self_test

ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "deploy/moblin-relay/moblin-relay-render-config"
RUNNER = ROOT / "deploy/moblin-relay/test-native-startup.py"
STAGE = PurePosixPath("/tmp/adojapan-ci-startup-abcdefgh")  # noqa: S108 - pure fake path


@pytest.fixture
def runner():
    return runpy.run_path(str(RUNNER), run_name="_private_renderer_staging_test")


def test_actual_selftest_renderer_contract_fails_before_fix_and_passes_after(
    runner, monkeypatch, tmp_path
):
    original = RENDERER.read_bytes()
    staged = runner["renderer_for_stage"](original, STAGE)
    assert original != staged
    assert RENDERER.read_bytes() == original
    source = tmp_path / "renderer.py"
    source.write_bytes(original)
    api = load_self_test()["validate_preview_renderer_contract"].__globals__
    api.update(RENDERER=source, SLATE=STAGE / "slate.mp4", NORMALIZER=STAGE / "wrapper.py")
    # Import-only substitutes on Windows; the real renderer contract uses neither API.
    monkeypatch.setitem(sys.modules, "grp", ModuleType("grp"))
    monkeypatch.setitem(sys.modules, "pwd", ModuleType("pwd"))
    with pytest.raises(
        api["TestFailure"], match="^YouTube forward is not attached directly to relay-output$"
    ):
        api["validate_preview_renderer_contract"]()
    source.write_bytes(staged)
    result = api["validate_preview_renderer_contract"]()
    assert result["forward_path"] == "relay-output"
    assert result["video_mode"] == "copy"
    assert result["recovery_permission"] == "api-only"


def test_renderer_derivation_changes_exactly_one_assignment_and_preserves_line_endings(runner):
    original = RENDERER.read_bytes()
    for value in (
        original.replace(b"\r\n", b"\n"),
        original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"),
    ):
        staged = runner["renderer_for_stage"](value, STAGE)
        replacement = ("SLATE_FILE = " + repr(str(STAGE / "slate.mp4"))).encode("ascii")
        assert (
            staged.replace(replacement, b'SLATE_FILE = "/var/lib/moblin-relay/slate.mp4"') == value
        )
        assert b'NORMALIZER = "/opt/moblin-relay/libexec/moblin-relay-normalize"' in staged


@pytest.mark.parametrize(
    "data", [b"no slate assignment", b'SLATE_FILE = "/var/lib/moblin-relay/slate.mp4"\n' * 2]
)
def test_renderer_derivation_refuses_changed_or_ambiguous_anchor(runner, data):
    with pytest.raises(runner["DiagnosticFailure"], match="SLATE_ANCHOR_CHANGED"):
        runner["renderer_for_stage"](data, STAGE)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/another-stage",  # noqa: S108 - rejected fake path
        "/tmp/adojapan-ci-startup-short",  # noqa: S108 - rejected fake path
        "/tmp/adojapan-ci-startup-abcdefgh\nanything",  # noqa: S108 - rejected fake path
        "/elsewhere/adojapan-ci-startup-abcdefgh",
    ],
)
def test_renderer_derivation_refuses_untrusted_stage_names(runner, path):
    with pytest.raises(runner["DiagnosticFailure"], match="PRIVATE_RENDERER_STAGE_PATH"):
        runner["renderer_for_stage"](RENDERER.read_bytes(), PurePosixPath(path))


def test_stage_retains_exact_renderer_source_and_reports_original_and_derived_hashes(
    runner, monkeypatch
):
    original = RENDERER.read_bytes()
    writes = {}
    api = runner["stage_sources"].__globals__
    source_paths = {path: name for name, path in runner["STAGED"].items()}

    def read(path):
        return original if source_paths[path] == "renderer.py" else source_paths[path].encode()

    def save(path, data, mode=0o600):
        assert path not in writes
        writes[path] = (data, mode)

    monkeypatch.setitem(api, "read_private", read)
    monkeypatch.setitem(api, "save_new", save)
    hashes = runner["stage_sources"](STAGE)
    staged, mode = writes[STAGE / "renderer.py"]
    assert writes[STAGE / "renderer-source.py"] == (original, 0o600)
    assert mode == 0o600
    assert hashes["renderer.py"] == hashlib.sha256(original).hexdigest()
    assert hashes["renderer-staged.py"] == hashlib.sha256(staged).hexdigest()
    assert hashes["renderer.py"] != hashes["renderer-staged.py"]
    assert writes[STAGE / "wrapper.py"][1] == 0o755


def test_pre_secret_setup_failure_is_distinct_from_cleanup_failure(runner):
    result = {"failure": "setup failure", "workdir_removed": True, "secret_configs_wiped": 0}
    state = {"initial_completed": False, "last_stage": "assets"}
    summary = runner["prefix_summary"](result, {}, state, 1)
    assert summary["status"] == "ORIGINAL_PREFIX_FAILURE"
    assert summary["cleanup_passed"] is True
    for change in (
        {"workdir_removed": False},
        {"cleanup_failure": ["failure"]},
        {"secret_configs_wiped": -1},
        {"secret_configs_wiped": True},
    ):
        summary = runner["prefix_summary"](result | change, {}, state, 1)
        assert summary["cleanup_passed"] is False
    for changed_state in (
        state | {"initial_completed": True},
        state | {"last_stage": "live-normalize"},
    ):
        summary = runner["prefix_summary"](result, {}, changed_state, 1)
        assert summary["cleanup_passed"] is False
