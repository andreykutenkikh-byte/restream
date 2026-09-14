"""Diagnostic opt-in must not alter strict capture success or failure semantics."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml
from test_moblin_relay_bundle import ROOT, load_self_test

from scripts import ci_native_reader_evidence as staging


def test_unconfigured_loader_does_not_read_or_execute_helper(monkeypatch):
    api = load_self_test()
    loader = api["install_ci_reader_evidence"]
    scope = loader.__globals__
    monkeypatch.setitem(
        scope,
        "Path",
        lambda _path: SimpleNamespace(
            exists=lambda: False,
            is_symlink=lambda: False,
        ),
    )
    monkeypatch.setitem(scope, "exec", lambda *_args: pytest.fail("must not execute"))
    assert loader() is False


@pytest.mark.parametrize("begin_raises", [False, True])
def test_optional_diagnostic_failure_never_replaces_original_timeout(
    monkeypatch, tmp_path, begin_raises
):
    api = load_self_test()
    capture = api["capture_final_sink_media_segment"]
    scope = capture.__globals__
    calls = []

    def begin(path, index):
        assert index == 4
        if begin_raises:
            raise RuntimeError("diagnostic begin failed")

    def retain(path, exc):
        calls.append(str(exc))
        raise RuntimeError("diagnostic retain failed")

    diagnostic = SimpleNamespace(begin_capture=begin, retain_failure=retain, active=True)

    def reader(command, actual_diagnostic, *, timeout):
        assert timeout == 15 and actual_diagnostic is diagnostic
        assert command[command.index("-frames:v") + 1] == "90"
        assert command[command.index("-c") + 1] == "copy"
        (tmp_path / "sink-proof-004.flv").write_bytes(b"incomplete")
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setitem(scope, "run_capture_reader", reader)
    with pytest.raises(api["TestFailure"], match="strict RTMP sink media read timed out"):
        capture(tmp_path, 4, lambda _command: None, reader_diagnostic=diagnostic)
    assert calls == ["strict RTMP sink media read timed out"]
    assert not (tmp_path / "sink-proof-004.flv").exists()
    if begin_raises:
        assert diagnostic.active is False


def test_staging_refuses_non_ci_without_commands(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(staging, "run", lambda *_args, **_kwargs: pytest.fail("no commands"))
    assert staging.main() == 2
    assert "isolated GitHub CI only" in capsys.readouterr().out


def test_staging_payload_binds_exact_sources_and_has_no_media(monkeypatch):
    monkeypatch.setattr(staging, "run", lambda args: b"a" * 40 if args[-1] == "HEAD" else b"b" * 40)
    value = json.loads(staging.payload())
    assert value.keys() == {"manifest", "helper.py", "postmortem.py"}
    assert value["manifest"]["source_sha"] == "a" * 40
    assert value["manifest"]["source_tree"] == "b" * 40
    assert len(value["manifest"]["self_test_sha256"]) == 64


def test_workflow_keeps_main_failure_and_runs_postmortem_outside_measured_step():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    preparation = next(
        i for i, step in enumerate(steps) if step.get("id") == "native_reader_evidence"
    )
    original = steps[preparation + 1]
    postmortem_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("run", "").endswith("ci_native_reader_evidence.py collect")
    )
    postmortem = steps[postmortem_index]
    assert steps[preparation + 2]["name"] == "Strict native reader clock counterfactual"
    assert steps[postmortem_index - 1]["name"] == "Post-onboarding runtime limits"
    assert original["id"] == "native_onboarding"
    assert original["run"] == "uv run --locked python scripts/ci_node_onboarding_smoke.py"
    assert "continue-on-error" not in original
    assert postmortem["run"].endswith("ci_native_reader_evidence.py collect")
    assert "steps.native_reader_evidence.outcome == 'success'" in postmortem["if"]
    assert "continue-on-error" not in postmortem
