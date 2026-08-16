from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "node-agent-image.yml"


def workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8")))


def test_node_agent_image_publish_triggers_and_permissions_are_narrow() -> None:
    document = workflow()
    triggers = document["on"]

    assert set(triggers) == {"workflow_dispatch", "push"}
    assert triggers["push"] == {"tags": ["node-v*"]}
    assert "pull_request" not in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert document["permissions"] == {"contents": "read", "packages": "write"}


def test_node_agent_image_actions_are_immutable_and_build_the_repository_context() -> None:
    document = workflow()
    steps = document["jobs"]["publish"]["steps"]
    uses = [str(step["uses"]) for step in steps if "uses" in step]

    assert {entry.split("@", 1)[0] for entry in uses} == {
        "actions/checkout",
        "docker/setup-buildx-action",
        "docker/login-action",
        "docker/metadata-action",
        "docker/build-push-action",
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", entry) for entry in uses)

    build = next(step for step in steps if step.get("id") == "build")
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

    assert metadata["images"] == "ghcr.io/${{ github.repository_owner }}/restream-node"
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


def test_node_agent_image_digest_is_reported_without_secret_expansion() -> None:
    document = workflow()
    report = next(
        step
        for step in document["jobs"]["publish"]["steps"]
        if step.get("name") == "Report immutable image digest"
    )

    assert report["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert report["env"]["IMAGE"] == ("ghcr.io/${{ github.repository_owner }}/restream-node")
    assert "GITHUB_STEP_SUMMARY" in report["run"]
    assert "secrets." not in report["run"]
