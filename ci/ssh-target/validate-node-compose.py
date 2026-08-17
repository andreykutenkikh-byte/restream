"""Reject-by-default validator for the fake CI Docker Compose surface."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

EXPECTED_IMAGE = (
    "ghcr.io/adojapan/ci-node-agent@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)


def require(condition: bool) -> None:
    if not condition:
        raise SystemExit(1)


def mapping(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict))
    return cast(dict[str, Any], value)


def validate_document(value: Any) -> None:
    """Validate the exact secret-free Compose document emitted by the worker."""

    model = mapping(value)
    require(set(model) == {"services"})
    services = mapping(model["services"])
    require(set(services) == {"agent"})
    agent = mapping(services["agent"])
    require(
        set(agent)
        == {
            "image",
            "user",
            "restart",
            "stop_grace_period",
            "read_only",
            "environment",
            "volumes",
            "tmpfs",
            "security_opt",
            "cap_drop",
            "cpus",
            "mem_limit",
            "pids_limit",
        }
    )
    require(agent["image"] == EXPECTED_IMAGE)
    require(agent["user"] == "10001:10001")
    require(agent["restart"] == "unless-stopped")
    require(agent["stop_grace_period"] == "45s")
    require(agent["read_only"] is True)
    require(agent["cpus"] == "0.25")
    require(agent["mem_limit"] == "256m")
    require(agent["pids_limit"] == 128)
    require(agent["cap_drop"] == ["ALL"])
    require(agent["security_opt"] == ["no-new-privileges:true"])
    require(agent["tmpfs"] == ["/tmp:size=32m,mode=1777"])  # noqa: S108
    require(
        agent["volumes"]
        == [
            {
                "type": "bind",
                "source": "./data",
                "target": "/var/lib/adojapan-node",
                "bind": {"create_host_path": False, "selinux": "Z"},
            }
        ]
    )
    environment = mapping(agent["environment"])
    expected_environment = {
        "NODE_CONTROL_URL": "http://backend:8000",
        "NODE_DATA_DIR": "/var/lib/adojapan-node",
        "NODE_OS_NAME": "debian",
        "NODE_OS_VERSION": "12",
        "NODE_ARCHITECTURE": "amd64",
        "NODE_AGENT_ENVIRONMENT": "test",
    }
    require(set(environment) == set(expected_environment) | {"NODE_HOSTNAME"})
    require(all(environment[key] == value for key, value in expected_environment.items()))
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", str(environment["NODE_HOSTNAME"]))
        is not None
    )
    require(re.fullmatch(r"[a-z0-9./:@_-]+", str(agent["image"])) is not None)


def main() -> None:
    require(len(sys.argv) == 2)
    path = Path(sys.argv[1])
    require(path == Path("/opt/adojapan-restream-node/compose.yml"))
    require(path.is_file() and not path.is_symlink())
    validate_document(yaml.safe_load(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
