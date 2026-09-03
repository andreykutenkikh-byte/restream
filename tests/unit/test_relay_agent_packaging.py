from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from app.services.relays import RelayProvisionGrant

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "hk-relay-agent"


def test_systemd_units_keep_secrets_out_and_apply_expected_boundary() -> None:
    agent = (DEPLOY / "adojapan-relay-agent.service").read_text(encoding="utf-8")
    broker = (DEPLOY / "adojapan-relay-broker.service").read_text(encoding="utf-8")
    broker_socket = (DEPLOY / "adojapan-relay-broker.socket").read_text(encoding="utf-8")
    tmpfiles = (DEPLOY / "adojapan-relay-agent.tmpfiles").read_text(encoding="utf-8")
    combined = "\n".join((agent, broker, broker_socket))

    assert "User=restream-agent" in agent
    assert "SocketBindDeny=any" in agent
    assert "LimitCORE=0" in agent
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in agent
    assert "Environment=" not in combined
    assert "stream_key" not in combined.lower()
    assert "node.token" not in agent.split("ExecStart=", 1)[1].splitlines()[0]
    assert "PrivateNetwork=yes" not in broker
    assert "ProtectProc=invisible" not in broker
    assert "CapabilityBoundingSet=\n" in broker
    assert "IPAddressDeny=any" in broker
    assert "IPAddressAllow=localhost" in broker
    assert "LimitCORE=0" in broker
    assert "KillMode=control-group" in broker
    assert "TasksMax=32" in broker
    assert "AF_INET" in broker
    assert "AF_NETLINK" in broker
    assert "SocketMode=0660" in broker_socket
    assert "SocketUser=root" in broker_socket
    assert "SocketGroup=restream-agent" in broker_socket
    assert "ListenStream=/run/adojapan-relay/broker.sock" in broker_socket
    assert "d /run/lock/moblin-relay 0700 root root -" in tmpfiles


def test_installers_do_not_mutate_moblin_relay_or_use_secret_arguments() -> None:
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    uninstaller = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    token_installer = (DEPLOY / "install-token.py").read_text(encoding="utf-8")
    journal_helper = (DEPLOY / "journal-rollback.py").read_text(encoding="utf-8")
    for script in (installer, uninstaller):
        assert "systemctl start moblin-relay" not in script
        assert "systemctl stop moblin-relay" not in script
        assert "systemctl enable moblin-relay" not in script
        assert "systemctl disable moblin-relay" not in script
        assert "docker" not in script.lower()
        assert "iptables" not in script.lower()
    assert "sys.argv[1:]" in token_installer
    assert "getpass.getpass" in token_installer
    assert "os.fchmod(fd, 0o600)" in token_installer
    assert "os.fchown(fd, account.pw_uid, account.pw_gid)" in token_installer
    assert "os.replace(temporary, TARGET)" in token_installer
    assert "chown -R root:root" in installer
    assert "-type l -print -quit" in installer
    assert "relay_agent_ensure_config_dir /etc/sysusers.d" in installer
    assert "relay_agent_ensure_config_dir /etc/tmpfiles.d" in installer
    assert "[ -L \"$relay_agent_config_dir\" ]" in installer
    assert "stat -c '%u:%g'" in installer
    assert "?????w*|????????w*" in installer
    assert "Stop adojapan-relay-agent.service before install or update." in installer
    assert "Stop adojapan-relay-broker.service before install or update." in installer
    prepare_position = installer.index('"$relay_agent_journal_helper" --prepare')
    code_swap_position = installer.index("relay_agent.old")
    assert prepare_position < code_swap_position
    assert '-m 0700 "$relay_agent_journal_helper"' in installer
    assert "/usr/local/sbin/adojapan-relay-restore-v1-journal" in installer
    assert "rm -f /usr/local/sbin/adojapan-relay-restore-v1-journal" not in uninstaller
    assert "subprocess.run(  # noqa: S603" in journal_helper
    assert "shell=True" not in journal_helper
    assert "commands.v1.rollback.json" in journal_helper
    assert '"O_NOFOLLOW"' in journal_helper
    assert "os.replace(temporary, path)" in journal_helper


def test_agent_sources_have_no_shell_execution_or_secret_logging() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "relay_agent").glob("*.py"))
    )
    assert "shell=True" not in sources
    assert "set -x" not in sources
    assert "logger.debug" not in sources
    assert "request body" not in sources.lower()
    assert "authorization header" not in sources.lower()


def test_native_agent_syntax_is_python_310_compatible() -> None:
    for path in sorted((ROOT / "relay_agent").glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def test_native_agent_is_discoverable_by_the_locked_test_environment() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = configuration["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "relay_agent*" in includes


def test_relay_provision_grant_repr_never_contains_the_raw_token() -> None:
    token = "node_11111111-1111-4111-8111-111111111111_REPR_SECRET_CANARY"
    grant = RelayProvisionGrant(node_id="11111111-1111-4111-8111-111111111111", node_token=token)
    assert token not in repr(grant)
