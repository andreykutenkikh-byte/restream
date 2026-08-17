from pathlib import Path

from scripts.check_repository import check


def test_repository_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    assert check(root) == []
    assert "restream.adojapan.ru" in (root / "README.md").read_text(encoding="utf-8")


def test_repository_policy_rejects_direct_firewall_and_daemon_changes(tmp_path: Path) -> None:
    worker = tmp_path / "bootstrap_worker"
    worker.mkdir()
    (worker / "bad.py").write_text(
        "\n".join(
            (
                "command = 'uf" + "w allow 22'",
                "command2 = 'ip" + "tables -A INPUT'",
                "command3 = 'ip6" + "tables -A INPUT'",
                "command4 = 'nf" + "t add table inet bad'",
                "command5 = 'firewall-" + "cmd --add-port=22/tcp'",
                "config = '/etc/docker/" + "daemon.json'",
            )
        ),
        encoding="utf-8",
    )

    errors = check(tmp_path)

    assert sum("direct firewall management" in error for error in errors) == 5
    assert any("Docker daemon/firewall configuration" in error for error in errors)


def test_repository_policy_rejects_direct_selinux_changes(tmp_path: Path) -> None:
    worker = tmp_path / "bootstrap_worker"
    worker.mkdir()
    (worker / "bad.py").write_text(
        "command = 'set" + "enforce 0'\nconfig = '/etc/selinux/" + "config'\n",
        encoding="utf-8",
    )

    errors = check(tmp_path)

    assert any("direct SELinux management" in error for error in errors)
    assert any("SELinux host configuration" in error for error in errors)


def test_repository_policy_rejects_remote_agent_host_ports_and_host_network(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "bootstrap_worker"
    worker.mkdir()
    host_network = "network_" + "mode: host"
    (worker / "installer.py").write_text(
        "def render_agent_compose():\n"
        "    return '''services:\n"
        "  agent:\n"
        "    ports:\n"
        "      - 9000:9000\n"
        f"    {host_network}\n"
        "'''\n",
        encoding="utf-8",
    )

    errors = check(tmp_path)

    assert any("must not publish host ports" in error for error in errors)
    assert any("must not set network_mode" in error for error in errors)
