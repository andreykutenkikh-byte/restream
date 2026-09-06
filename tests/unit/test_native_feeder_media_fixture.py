from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-feeder-media.py"


def test_clock_probe_stages_live_tmpfs_through_exec_not_docker_archive():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        item
        for item in workflow["jobs"]["test"]["steps"]
        if item.get("name") == "Native fixture media clock under scheduler jitter"
    )
    script = step["run"]
    assert "exec -T ci-ssh-target python3 -c" in script
    assert "target.write_bytes(data)" in script
    assert "target.read_bytes() == data" in script
    assert "< deploy/moblin-relay/self-test" in script
    assert "cp deploy/moblin-relay/self-test" not in script


@pytest.mark.parametrize("missing", ["staged self-test", "FFmpeg", "FFprobe"])
def test_clock_probe_reports_missing_prerequisite_without_media_work(monkeypatch, missing):
    namespace = runpy.run_path(str(HELPER), run_name="_clock_fixture_test")
    names = {
        namespace["SELF_TEST"]: "staged self-test",
        Path("/usr/bin/ffmpeg"): "FFmpeg",
        Path(namespace["FFPROBE"]): "FFprobe",
    }
    monkeypatch.setenv("CI_NATIVE_FEEDER_CLOCK", "isolated-fixture")
    monkeypatch.setattr(Path, "is_file", lambda path: names[path] != missing)
    with pytest.raises(
        namespace["ProbeFailure"], match=f"^isolated fixture prerequisite missing: {missing}$"
    ):
        namespace["main"]()
