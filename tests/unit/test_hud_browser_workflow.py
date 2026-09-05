"""Keep real browser acceptance required and separate from native media CI."""

from pathlib import Path

import yaml


def test_browser_ci_installs_both_engines_and_requires_real_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    job = jobs["hud-browser"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 15
    assert "if" not in job and "needs" not in job and "continue-on-error" not in job
    steps = job["steps"]
    assert all("continue-on-error" not in step and "if" not in step for step in steps)
    commands = [step.get("run", "") for step in steps]
    assert "uv lock --check" in commands
    assert "uv sync --locked --group browser" in commands
    assert "uv pip check" in commands
    assert (
        "uv run --locked --group browser python -m playwright install --with-deps chromium webkit"
        in commands
    )
    smoke = next(step for step in steps if step.get("name") == "Real HTTPS HUD browser smoke")
    assert smoke["env"] == {"ADOJAPAN_HUD_BROWSER_SMOKE": "1"}
    assert smoke["run"] == "uv run --locked --group browser pytest tests/browser -q"
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    native_commands = [step.get("run", "") for step in jobs["test"]["steps"]]
    assert "uv run --locked python scripts/ci_output_smoke.py" in native_commands
    assert "uv run --locked python scripts/ci_node_onboarding_smoke.py" in native_commands
