import subprocess
from pathlib import Path

import scripts.check_repository as repository_policy
from scripts.check_repository import GIT_INSPECTION_ERROR, check


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


def test_repository_policy_rejects_encrypted_backup_in_public_source_repo(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "adojapan-restream-dr-test.tar.gz.age"
    artifact.write_bytes(b"encrypted")

    assert check(tmp_path) == [
        "adojapan-restream-dr-test.tar.gz.age: runtime data belongs outside the public source repo"
    ]


def test_repository_policy_rejects_plaintext_database_and_environment(tmp_path: Path) -> None:
    (tmp_path / "restream.db").write_bytes(b"sqlite")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    assert sorted(check(tmp_path)) == [
        ".env: runtime data belongs outside the public source repo",
        "restream.db: runtime data belongs outside the public source repo",
    ]


def test_repository_policy_allows_only_the_documented_environment_example(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.example").write_text("SETTING=replace-me\n", encoding="utf-8")
    (tmp_path / ".env.ci").write_text("SECRET=ephemeral-ci-value\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("SECRET=value\n", encoding="utf-8")

    assert sorted(check(tmp_path)) == [
        ".env.ci: runtime data belongs outside the public source repo",
        ".env.local: runtime data belongs outside the public source repo",
        ".env.production: runtime data belongs outside the public source repo",
    ]


def test_repository_policy_allows_only_gitignored_untracked_root_ci_environment(
    tmp_path: Path,
) -> None:
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
        check=True,
    )
    (tmp_path / ".gitignore").write_text(".env.*\n", encoding="utf-8")
    (tmp_path / ".env.ci").write_text("SECRET=ephemeral-ci-value\n", encoding="utf-8")

    assert check(tmp_path) == []

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".env.ci").write_text("SECRET=not-the-root-ci-file\n", encoding="utf-8")

    assert check(tmp_path) == [
        f"{Path('nested/.env.ci')}: runtime data belongs outside the public source repo"
    ]


def test_repository_policy_rejects_unignored_untracked_ci_environment(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
        check=True,
    )
    (tmp_path / ".env.ci").write_text("SECRET=ephemeral-ci-value\n", encoding="utf-8")

    assert check(tmp_path) == [".env.ci: runtime data belongs outside the public source repo"]


def test_repository_policy_fails_closed_when_git_inspection_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env.ci").write_text("SECRET=ephemeral-ci-value\n", encoding="utf-8")

    def fail_git(*_args, **_kwargs):
        raise OSError("simulated unavailable Git")

    monkeypatch.setattr(repository_policy.subprocess, "run", fail_git)

    assert check(tmp_path) == [GIT_INSPECTION_ERROR]


def test_repository_policy_checks_tracked_runtime_data_inside_ignored_directories(
    tmp_path: Path,
) -> None:
    artifacts = (
        tmp_path / ".env.ci",
        tmp_path / "data" / "runtime.db",
        tmp_path / "backups" / "snapshot.age",
        tmp_path / "logs" / ".env.production",
    )
    for artifact in artifacts:
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(b"runtime")
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
        check=True,
    )
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        [  # noqa: S607
            "git",
            "-C",
            str(tmp_path),
            "add",
            "--force",
            ".env.ci",
            "data",
            "backups",
            "logs",
        ],
        check=True,
    )

    assert sorted(check(tmp_path)) == [
        f"{Path('.env.ci')}: runtime data belongs outside the public source repo",
        f"{Path('backups/snapshot.age')}: runtime data belongs outside the public source repo",
        f"{Path('data/runtime.db')}: runtime data belongs outside the public source repo",
        f"{Path('logs/.env.production')}: runtime data belongs outside the public source repo",
    ]


def test_repository_policy_rejects_case_variant_environments(tmp_path: Path) -> None:
    for name in (".ENV", ".Env.production", ".ENV.EXAMPLE"):
        (tmp_path / name).write_text("SECRET=value\n", encoding="utf-8")

    assert check(tmp_path) == [
        f"{name}: runtime data belongs outside the public source repo"
        for name in (".ENV", ".ENV.EXAMPLE", ".Env.production")
    ]


def test_repository_policy_rejects_database_sidecars_and_age_temporary_files(
    tmp_path: Path,
) -> None:
    names = (
        "restream.db-wal",
        "restream.db-shm",
        "restream.db-journal",
        "restream.sqlite-wal",
        "restream.sqlite3-shm",
        "snapshot.tar.gz.age.tmp",
        "snapshot.tar.gz.age.tmp-123",
        "snapshot.age.part",
    )
    for name in names:
        (tmp_path / name).write_bytes(b"runtime")

    assert check(tmp_path) == [
        f"{name}: runtime data belongs outside the public source repo" for name in sorted(names)
    ]


def test_repository_policy_rejects_generic_pem_private_keys(tmp_path: Path) -> None:
    prefixes = ("", "ENCRYPTED ", "OPENSSH ", "RSA ", "EC ")
    for index, prefix in enumerate(prefixes):
        marker = "-----BEGIN " + prefix + "PRIVATE KEY-----"
        (tmp_path / f"identity-{index}.pem").write_text(
            marker + "\nnot-a-real-key\n",
            encoding="utf-8",
        )

    assert check(tmp_path) == [
        f"identity-{index}.pem: forbidden committed private key" for index in range(len(prefixes))
    ]


def test_repository_policy_rejects_age_secret_identities(tmp_path: Path) -> None:
    marker = "AGE-SECRET-" + "KEY-1TESTIDENTITY"
    (tmp_path / "recovery.txt").write_text(marker + "\n", encoding="utf-8")

    assert check(tmp_path) == ["recovery.txt: forbidden committed age secret identity"]


def test_repository_policy_scans_checker_source_for_secret_material(tmp_path: Path) -> None:
    checker = tmp_path / "scripts" / "check_repository.py"
    checker.parent.mkdir()
    pem_marker = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    age_marker = "AGE-SECRET-" + "KEY-1CHECKERIDENTITY"
    checker.write_text(f"{pem_marker}\n{age_marker}\n", encoding="utf-8")

    assert check(tmp_path) == [
        f"{Path('scripts/check_repository.py')}: forbidden committed private key",
        f"{Path('scripts/check_repository.py')}: forbidden committed age secret identity",
    ]


def test_repository_policy_scans_staged_blobs_after_worktree_replacement_or_deletion(
    tmp_path: Path,
) -> None:
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
        check=True,
    )
    staged_key = tmp_path / "overwritten-key.txt"
    staged_identity = tmp_path / "deleted-age.txt"
    staged_key.write_text(
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nindexed-secret\n",
        encoding="utf-8",
    )
    staged_identity.write_text(
        "AGE-SECRET-" + "KEY-1INDEXEDIDENTITY\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - fixed local Git fixture
        ["git", "-C", str(tmp_path), "add", "--", staged_key.name, staged_identity.name],  # noqa: S607
        check=True,
    )
    staged_key.write_text("benign worktree replacement\n", encoding="utf-8")
    staged_identity.unlink()

    assert check(tmp_path) == [
        "deleted-age.txt: forbidden committed age secret identity",
        "overwritten-key.txt: forbidden committed private key",
    ]


def test_gitignore_covers_runtime_environments_but_keeps_example() -> None:
    root = Path(__file__).resolve().parents[1]
    rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in rules
    assert ".env.*" in rules
    assert ".[eE][nN][vV]" in rules
    assert ".[eE][nN][vV].*" in rules
    assert "!.env.example" in rules
    assert rules.index(".env.*") < rules.index("!.env.example")
    for pattern in (
        "*.age.*",
        "*.age-*",
        "*.db-*",
        "*.sqlite-*",
        "*.sqlite3-*",
    ):
        assert pattern in rules


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
