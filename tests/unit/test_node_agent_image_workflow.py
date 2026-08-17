from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "node-agent-image.yml"
VERIFIER_PATH = ROOT / "scripts" / "verify_node_agent_image_pull.sh"
IMAGE = "ghcr.io/andreykutenkikh-byte/restream-node"
EXACT_REFERENCE = f"{IMAGE}@sha256:{'a' * 64}"


def workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8")))


def test_node_agent_image_publish_triggers_and_permissions_are_narrow() -> None:
    document = workflow()
    triggers = document["on"]
    publish = document["jobs"]["publish"]

    assert set(triggers) == {"workflow_dispatch", "push"}
    assert triggers["push"] == {"tags": ["node-v*"]}
    assert "pull_request" not in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert document["permissions"] == {}
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert publish["outputs"] == {"digest": "${{ steps.build.outputs.digest }}"}


def test_node_agent_image_actions_are_immutable_and_build_the_repository_context() -> None:
    document = workflow()
    all_steps = [step for job in document["jobs"].values() for step in job["steps"]]
    uses = [str(step["uses"]) for step in all_steps if "uses" in step]

    assert {entry.split("@", 1)[0] for entry in uses} == {
        "actions/checkout",
        "docker/setup-buildx-action",
        "docker/login-action",
        "docker/metadata-action",
        "docker/build-push-action",
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", entry) for entry in uses)

    build = next(step for step in document["jobs"]["publish"]["steps"] if step.get("id") == "build")
    assert build["with"] == {
        "context": ".",
        "file": "./Dockerfile.node",
        "platforms": "linux/amd64",
        "push": True,
        "tags": "${{ steps.metadata.outputs.tags }}",
        "labels": "${{ steps.metadata.outputs.labels }}",
    }


def test_node_agent_image_metadata_has_no_mutable_latest_tag() -> None:
    document = workflow()
    steps = document["jobs"]["publish"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "metadata")["with"]

    assert metadata["images"] == IMAGE
    assert metadata["flavor"] == "latest=false"
    assert metadata["tags"].splitlines() == [
        "type=ref,event=tag",
        "type=sha,format=long,prefix=sha-",
    ]
    assert "type=raw,value=latest" not in metadata["tags"]
    assert ":latest" not in metadata["tags"]
    assert "org.opencontainers.image.revision=${{ github.sha }}" in metadata["labels"]
    assert "{{commit_date 'YYYY-MM-DDTHH:mm:ss[Z]'}}" in metadata["labels"]
    assert "{{date" not in metadata["labels"]


def test_anonymous_pull_is_a_separate_fail_closed_job_without_registry_credentials() -> None:
    document = workflow()
    anonymous = document["jobs"]["verify-anonymous-pull"]
    serialized = yaml.safe_dump(anonymous, sort_keys=True)

    assert anonymous["needs"] == "publish"
    assert anonymous["runs-on"] == "ubuntu-latest"
    assert anonymous["permissions"] == {"contents": "read", "packages": "none"}
    assert "docker/login-action" not in serialized
    assert "docker login" not in serialized
    assert "secrets." not in serialized
    assert "continue-on-error" not in serialized
    assert ":latest" not in serialized

    checkout = next(step for step in anonymous["steps"] if "uses" in step)
    assert checkout["with"] == {"persist-credentials": False}

    verifier = next(
        step
        for step in anonymous["steps"]
        if step.get("name") == "Verify anonymous exact-digest pull"
    )
    assert verifier["env"]["IMAGE_REFERENCE"] == (
        f"{IMAGE}@${{{{ needs.publish.outputs.digest }}}}"
    )
    assert 'sh scripts/verify_node_agent_image_pull.sh "$IMAGE_REFERENCE"' in verifier["run"]
    assert "Anonymous pull of the exact digest: passed." in verifier["run"]
    assert "GITHUB_STEP_SUMMARY" in verifier["run"]

    diagnostic = next(
        step
        for step in anonymous["steps"]
        if step.get("name") == "Explain public-package requirement"
    )
    assert diagnostic["if"] == "failure()"
    assert "visibility to public" in diagnostic["run"]
    assert "exit 1" in diagnostic["run"]


def test_digest_is_reported_without_secret_expansion() -> None:
    document = workflow()
    report = next(
        step
        for step in document["jobs"]["publish"]["steps"]
        if step.get("name") == "Report immutable image digest"
    )

    assert report["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert report["env"]["IMAGE"] == IMAGE
    assert "secrets." not in report["run"]


def test_anonymous_pull_verifier_is_exact_secret_free_and_cleans_up() -> None:
    source = VERIFIER_PATH.read_text(encoding="utf-8")

    assert 'if [ "$#" -ne 1 ]' in source
    assert "[0-9a-f]{64}" in source
    assert 'mktemp -d "$RUNNER_TEMP/' in source
    assert "trap cleanup 0 HUP INT TERM" in source
    assert 'rm -rf -- "$docker_config"' in source
    assert 'docker pull "$image_reference"' in source
    assert "docker image inspect --format" in source
    assert 'grep -Fqx "$image_reference"' in source
    assert "docker login" not in source
    assert "credential" not in source.lower()
    assert "password" not in source.lower()
    assert "token" not in source.lower()


def _write_fake_docker(bin_dir: Path) -> Path:
    executable = bin_dir / "docker"
    executable.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$DOCKER_CONFIG" > "$FAKE_DOCKER_CONFIG_LOG"
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "${1:-}" = pull ] && [ "$#" -eq 2 ]; then
  test -z "$(find "$DOCKER_CONFIG" -mindepth 1 -print -quit)"
  exit 0
fi
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
  for last_argument do :; done
  printf '%s\n' "$last_argument"
  exit 0
fi
exit 64
""",
        encoding="utf-8",
        newline="\n",
    )
    executable.chmod(0o755)
    return executable


def _run_verifier(tmp_path: Path, reference: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX shell is unavailable on this unit-test host")
    bin_dir = tmp_path / "bin"
    runner_temp = tmp_path / "runner-temp"
    bin_dir.mkdir()
    runner_temp.mkdir()
    _write_fake_docker(bin_dir)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "FAKE_DOCKER_CONFIG_LOG": str(tmp_path / "docker-config.log"),
        }
    )
    return subprocess.run(  # noqa: S603 - fixed local verifier and controlled test argument
        [shell, str(VERIFIER_PATH), reference],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
        env=environment,
    )


def test_anonymous_pull_verifier_accepts_exact_digest_and_removes_docker_config(
    tmp_path: Path,
) -> None:
    result = _run_verifier(tmp_path, EXACT_REFERENCE)

    assert result.returncode == 0
    assert f"Anonymous exact-digest pull verified: {EXACT_REFERENCE}" in result.stdout
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"pull {EXACT_REFERENCE}"
    assert calls[1].startswith("image inspect --format ")
    assert calls[1].endswith(EXACT_REFERENCE)
    docker_config = Path((tmp_path / "docker-config.log").read_text(encoding="utf-8").strip())
    assert docker_config.parent == tmp_path / "runner-temp"
    assert not docker_config.exists()


@pytest.mark.parametrize(
    "reference",
    [
        f"{IMAGE}:node-v1",
        f"{IMAGE}@sha256:{'A' * 64}",
        f"{IMAGE}@sha256:{'a' * 63}",
        f"ghcr.io/another-owner/restream-node@sha256:{'a' * 64}",
    ],
)
def test_anonymous_pull_verifier_rejects_mutable_or_malformed_references(
    tmp_path: Path, reference: str
) -> None:
    result = _run_verifier(tmp_path, reference)

    assert result.returncode == 2
    assert not (tmp_path / "docker.log").exists()


def test_anonymous_pull_verifier_does_not_interpret_shell_metacharacters(tmp_path: Path) -> None:
    injected = tmp_path / "injected"
    reference = f"{EXACT_REFERENCE};touch {injected}"

    result = _run_verifier(tmp_path, reference)

    assert result.returncode == 2
    assert not injected.exists()
    assert not (tmp_path / "docker.log").exists()
