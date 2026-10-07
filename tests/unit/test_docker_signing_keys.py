"""Execute the generated key gate with Docker's actual public signing keys."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_bootstrap_worker_installer import detected_facts

from bootstrap_worker.installer import AptDockerAdapter, DnfDockerAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/docker-keys"


@pytest.mark.skipif(os.name == "nt" or not shutil.which("gpg"), reason="Linux GPG/shell gate")
@pytest.mark.parametrize("family", ["apt", "rpm"])
@pytest.mark.parametrize("matching", [True, False])
def test_generated_key_gate_accepts_only_its_actual_vendor_key(
    tmp_path: Path,
    family: str,
    matching: bool,
) -> None:
    adapter = AptDockerAdapter() if family == "apt" else DnfDockerAdapter()
    facts = (
        detected_facts("ubuntu", "24.04")
        if family == "apt"
        else detected_facts("almalinux", "8.10")
    )
    gate = next(
        s.command for s in adapter.install_plan(facts) if s.command.startswith("fingerprints=")
    )
    prefix = "/etc/apt/keyrings" if family == "apt" else "/etc/pki/rpm-gpg"
    filename = "docker.asc" if family == "apt" else "docker-ce.asc"
    key = "ubuntu" if (family == "apt") == matching else "rhel"
    (tmp_path / (filename + ".adojapan-tmp")).write_bytes((FIXTURES / (key + ".asc")).read_bytes())
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    gate = gate.replace(prefix, str(tmp_path)).replace("gpg2 ", "gpg ")
    result = subprocess.run(  # noqa: S603 - generated fixed shell, local public fixtures
        [shutil.which("sh") or "/bin/sh", "-c", gate],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "GNUPGHOME": str(home)},
    )
    assert (result.returncode == 0) == matching
    if family == "apt":
        assert (tmp_path / filename).exists() == matching
    assert result.stdout == ""
