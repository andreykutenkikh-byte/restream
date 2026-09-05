import hashlib
import io
import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.dr_backup import (
    PRIVATE_REPOSITORY_CONFIG,
    ArchiveSource,
    _release_commit,
    create_encrypted_snapshot,
    exclusive_repository_lock,
    publish_snapshot,
    validate_private_git_repository,
    write_recovery_stream,
)

RELEASE_COMMIT = "a" * 40


def run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def initialise_repository(path: Path, remote: str) -> None:
    path.mkdir()
    run_git(path, "init", "--initial-branch=main")
    run_git(path, "config", "user.name", "DR test")
    run_git(path, "config", "user.email", "dr-test@example.invalid")
    run_git(path, "remote", "add", "origin", remote)


def private_file(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_recovery_stream_contains_only_fixed_inputs_and_encrypted_manifest_data(
    tmp_path: Path,
) -> None:
    database = private_file(tmp_path / "database.db", b"database-payload")
    environment = private_file(tmp_path / "environment", b"SECRET=value\n")
    stream = io.BytesIO()
    created_at = datetime(2026, 9, 4, 3, 2, 1, tzinfo=UTC)

    manifest = write_recovery_stream(
        stream,
        (
            ArchiveSource("control-plane/restream.db", database, 1024, True),
            ArchiveSource("control-plane/environment", environment, 1024, True),
        ),
        release_commit=RELEASE_COMMIT,
        created_at=created_at,
    )

    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        assert archive.getnames() == [
            "control-plane/restream.db",
            "control-plane/environment",
            "manifest.json",
        ]
        embedded = json.loads(archive.extractfile("manifest.json").read())
        assert archive.extractfile("control-plane/environment").read() == b"SECRET=value\n"
        for member in archive.getmembers():
            assert member.mode == 0o600
            assert member.uid == member.gid == 0

    assert embedded == manifest
    assert embedded["created_at"] == "2026-09-04T03:02:01Z"
    assert embedded["release_commit"] == RELEASE_COMMIT
    assert embedded["files"][1] == {
        "name": "control-plane/environment",
        "sha256": hashlib.sha256(b"SECRET=value\n").hexdigest(),
        "size": len(b"SECRET=value\n"),
    }


def test_recovery_stream_rejects_symlink_and_permissive_secret(tmp_path: Path) -> None:
    secret = private_file(tmp_path / "secret", b"secret")
    link = tmp_path / "link"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(ValueError, match="symbolic-link"):
        write_recovery_stream(
            io.BytesIO(),
            (ArchiveSource("control-plane/environment", link, 1024, True),),
            release_commit=RELEASE_COMMIT,
            created_at=datetime.now(UTC),
        )

    if os.name == "posix":
        secret.chmod(0o644)
        with pytest.raises(ValueError, match="group/world"):
            write_recovery_stream(
                io.BytesIO(),
                (ArchiveSource("control-plane/environment", secret, 1024, True),),
                release_commit=RELEASE_COMMIT,
                created_at=datetime.now(UTC),
            )


def test_encrypted_snapshot_is_atomic_and_age_receives_tar_only_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = private_file(tmp_path / "source", b"not-plaintext-at-rest")
    recipient = tmp_path / "recipients.txt"
    recipient.write_text("age1publicrecipient\n", encoding="utf-8")
    snapshot_directory = tmp_path / "snapshots"
    snapshot_directory.mkdir()
    observed_arguments: list[str] = []

    class FakeAge:
        def __init__(self, arguments: list[str], **kwargs: object) -> None:
            observed_arguments.extend(arguments)
            self.stdin = io.BytesIO()
            self.returncode: int | None = None
            self._output = kwargs["stdout"]

        def communicate(self) -> tuple[bytes, bytes]:
            assert self.stdin is None
            self._output.write(b"age-encrypted-payload")
            self.returncode = 0
            return b"", b""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self) -> int:
            return self.returncode or 0

    monkeypatch.setattr("scripts.dr_backup.subprocess.Popen", FakeAge)

    snapshot = create_encrypted_snapshot(
        (ArchiveSource("control-plane/environment", source, 1024, True),),
        release_commit=RELEASE_COMMIT,
        recipient_file=recipient,
        snapshot_directory=snapshot_directory,
        created_at=datetime(2026, 9, 4, 3, 2, 1, tzinfo=UTC),
    )

    assert observed_arguments == [
        "age",
        "--encrypt",
        "--recipients-file",
        str(recipient.resolve()),
    ]
    assert snapshot.name == "adojapan-restream-dr-20260904T030201Z.tar.gz.age"
    assert snapshot.read_bytes() == b"age-encrypted-payload"
    assert not list(snapshot_directory.glob("*.tmp-*"))


def test_encrypted_snapshot_rejects_oversized_git_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = private_file(tmp_path / "source", b"source")
    recipient = tmp_path / "recipients.txt"
    recipient.write_text("age1publicrecipient\n", encoding="utf-8")
    snapshot_directory = tmp_path / "snapshots"
    snapshot_directory.mkdir()

    class FakeAge:
        def __init__(self, arguments: list[str], **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.returncode: int | None = None
            self._output = kwargs["stdout"]

        def communicate(self) -> tuple[bytes, bytes]:
            self._output.write(b"oversized-ciphertext")
            self.returncode = 0
            return b"", b""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self) -> int:
            return self.returncode or 0

    monkeypatch.setattr("scripts.dr_backup.subprocess.Popen", FakeAge)
    monkeypatch.setattr("scripts.dr_backup.MAX_GIT_SNAPSHOT_BYTES", 10)

    with pytest.raises(ValueError, match="bounded Git artifact"):
        create_encrypted_snapshot(
            (ArchiveSource("control-plane/environment", source, 1024, True),),
            release_commit=RELEASE_COMMIT,
            recipient_file=recipient,
            snapshot_directory=snapshot_directory,
            created_at=datetime(2026, 9, 4, 3, 2, 1, tzinfo=UTC),
        )

    assert list(snapshot_directory.iterdir()) == []


def test_private_repository_guard_rejects_source_remote_and_plaintext(tmp_path: Path) -> None:
    source = tmp_path / "source"
    disaster_recovery = tmp_path / "dr"
    source_remote = "git@github.com:example/source.git"
    initialise_repository(source, source_remote)
    initialise_repository(disaster_recovery, source_remote)
    run_git(disaster_recovery, "config", "--local", PRIVATE_REPOSITORY_CONFIG, "true")

    with pytest.raises(ValueError, match="public source remote"):
        validate_private_git_repository(
            disaster_recovery,
            source,
            branch="main",
            remote="origin",
        )

    run_git(disaster_recovery, "remote", "set-url", "origin", "git@github.com:example/dr.git")
    (disaster_recovery / "README.md").write_text("plaintext", encoding="utf-8")
    run_git(disaster_recovery, "add", "README.md")
    run_git(disaster_recovery, "commit", "-m", "plaintext")
    with pytest.raises(ValueError, match="only encrypted"):
        validate_private_git_repository(
            disaster_recovery,
            source,
            branch="main",
            remote="origin",
        )


def test_private_repository_guard_accepts_clean_ciphertext_only_repo(tmp_path: Path) -> None:
    source = tmp_path / "source"
    disaster_recovery = tmp_path / "dr"
    initialise_repository(source, "git@github.com:example/source.git")
    initialise_repository(disaster_recovery, "git@github.com:example/dr.git")
    run_git(disaster_recovery, "config", "--local", PRIVATE_REPOSITORY_CONFIG, "true")
    encrypted = private_file(
        disaster_recovery / "adojapan-restream-dr-20260904T030201Z.tar.gz.age",
        b"ciphertext",
    )
    run_git(disaster_recovery, "add", encrypted.name)
    run_git(disaster_recovery, "commit", "-m", "ciphertext")

    assert (
        validate_private_git_repository(
            disaster_recovery,
            source,
            branch="main",
            remote="origin",
        )
        == disaster_recovery.resolve()
    )


def test_repository_lock_refuses_an_overlapping_backup(tmp_path: Path) -> None:
    repository = tmp_path / "dr"
    initialise_repository(repository, "git@github.com:example/dr.git")

    with (
        exclusive_repository_lock(repository),
        pytest.raises(RuntimeError, match="already running"),
        exclusive_repository_lock(repository),
    ):
        pass

    assert not (repository / ".git" / "adojapan-restream-dr.lock").exists()


def test_release_commit_rejects_tracked_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialise_repository(source, "git@github.com:example/source.git")
    tracked = source / "tracked.txt"
    tracked.write_text("released\n", encoding="utf-8")
    run_git(source, "add", tracked.name)
    run_git(source, "commit", "-m", "release")
    assert _release_commit(source) == run_git(source, "rev-parse", "HEAD")

    tracked.write_text("modified\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked changes"):
        _release_commit(source)


def test_publish_snapshot_pushes_only_selected_ciphertext(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", str(remote)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    repository = tmp_path / "dr"
    initialise_repository(repository, str(remote))
    snapshots = repository / "snapshots"
    snapshots.mkdir()
    snapshot = private_file(
        snapshots / "adojapan-restream-dr-20260904T030201Z.tar.gz.age",
        b"ciphertext",
    )

    publish_snapshot(repository, snapshot, branch="main", remote="origin")

    assert run_git(repository, "status", "--porcelain") == ""
    assert run_git(repository, "ls-files") == "snapshots/" + snapshot.name
    remote_files = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "--git-dir",
            str(remote),
            "ls-tree",
            "--name-only",
            "-r",
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert remote_files == ["snapshots/" + snapshot.name]
