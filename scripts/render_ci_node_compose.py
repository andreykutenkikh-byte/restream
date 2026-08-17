"""Render the secret-free Node Agent model for Docker Compose validation in CI."""

from __future__ import annotations

from uuid import UUID

from bootstrap_worker.installer import render_agent_compose
from bootstrap_worker.models import (
    BootstrapRequest,
    PackageManager,
    PlatformFamily,
    SELinuxMode,
    SystemFacts,
)

CI_AGENT_IMAGE = (
    "ghcr.io/adojapan/ci-node-agent@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)


def render() -> str:
    request = BootstrapRequest.model_validate(
        {
            "node_id": UUID("11111111-1111-4111-8111-111111111111"),
            "address": "ci-node.invalid",
            "username": "root",
            "password": "ci-render-only-placeholder",
            "control_url": "http://backend:8000",
            "node_agent_image": CI_AGENT_IMAGE,
            "node_agent_environment": "test",
        }
    )
    facts = SystemFacts(
        hostname="ci-rpm-node",
        os_name="almalinux",
        os_id="almalinux",
        os_version="8.10",
        os_major_version="8",
        id_like=("rhel", "centos", "fedora"),
        version_codename=None,
        architecture="amd64",
        platform_family=PlatformFamily.RHEL,
        package_manager=PackageManager.DNF,
        selinux_mode=SELinuxMode.ENFORCING,
        apt_get_available=False,
        dpkg_query_available=False,
        dnf_available=True,
        rpm_available=True,
        systemctl_available=True,
        cpu_count=2,
        memory_total_bytes=3 * 1024**3,
        memory_available_bytes=2 * 1024**3,
        disk_total_bytes=32 * 1024**3,
        disk_free_bytes=16 * 1024**3,
    )
    return render_agent_compose(request, facts)


def main() -> None:
    print(render(), end="")


if __name__ == "__main__":
    main()
