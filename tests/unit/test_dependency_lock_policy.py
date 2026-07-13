import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_lock_contains_exact_versions_for_the_complete_dependency_graph() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]

    assert packages
    assert all(package.get("version") for package in packages)

    project = next(package for package in packages if package["name"] == "adojapan-restream")
    assert project["dependencies"]
    assert project["dev-dependencies"]["dev"]


def test_docker_and_ci_enforce_the_same_locked_uv_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required_uv = pyproject["tool"]["uv"]["required-version"]
    uv_version = required_uv.removeprefix("==")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert required_uv == f"=={uv_version}"
    assert f'pip install "uv=={uv_version}"' in dockerfile
    assert f'pip install "uv=={uv_version}"' in workflow
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "uv sync --locked" in workflow
    assert "uv lock --check" in workflow
    assert "pip install ." not in dockerfile
    assert "pip install -e" not in workflow
