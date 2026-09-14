"""Encrypt a control-plane recovery snapshot and publish only ciphertext to Git.

The command intentionally accepts a transactionally consistent SQLite backup, not the live
database.  Production creates that input in a root-only tmpfs directory with ``scripts/backup.py``.
No plaintext tar archive is ever written: tar/gzip output is streamed directly to ``age``.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote

ARCHIVE_SCHEMA = 1
PRIVATE_REPOSITORY_CONFIG = "adojapan-restream.dr-private-confirmed"
PRIVATE_REPOSITORY_VALUE = "true"
SNAPSHOT_PREFIX = "adojapan-restream-dr-"
SNAPSHOT_SUFFIX = ".tar.gz.age"
MAX_GIT_SNAPSHOT_BYTES = 95 * 1024 * 1024
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")
SSH_REMOTE_PATTERN = re.compile(r"(?:git@[^:]+:[^\s]+|ssh://git@[^/\s]+/[^\s]+)")
REPOSITORY_LOCK_NAME = "adojapan-restream-dr.lock"


@dataclass(frozen=True)
class ArchiveSource:
    archive_name: str
    path: Path
    maximum_size: int
    private: bool


class HashingReader(io.RawIOBase):
    def __init__(self, source: IO[bytes]) -> None:
        self._source = source
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        payload = self._source.read(size)
        if not isinstance(payload, bytes):
            raise TypeError("Recovery inputs must be opened in binary mode")
        self._digest.update(payload)
        self.bytes_read += len(payload)
        return payload

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _resolved_regular_file(path: Path, *, private: bool, maximum_size: int) -> Path:
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic-link input: {path}")
    resolved = path.resolve(strict=True)
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    if details.st_size < 1 or details.st_size > maximum_size:
        raise ValueError(f"Unexpected input size: {path}")
    if private and os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError(f"Sensitive input must not be group/world accessible: {path}")
    return resolved


def _verify_sqlite_backup(path: Path) -> None:
    encoded = quote(path.as_posix(), safe="/")
    with sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise ValueError("SQLite backup did not pass quick_check")


def _tar_info(name: str, size: int, timestamp: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o600
    member.uid = 0
    member.gid = 0
    member.uname = "root"
    member.gname = "root"
    member.mtime = timestamp
    return member


def write_recovery_stream(
    output: IO[bytes],
    sources: Sequence[ArchiveSource],
    *,
    release_commit: str,
    created_at: datetime,
) -> dict[str, Any]:
    """Write one compressed recovery tar stream and return its embedded manifest."""
    if not COMMIT_PATTERN.fullmatch(release_commit):
        raise ValueError("release commit must be a full hexadecimal Git object ID")
    timestamp = int(created_at.timestamp())
    entries: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        for source in sources:
            if source.archive_name in seen_names or source.archive_name.startswith("/"):
                raise ValueError("Archive member names must be unique and relative")
            if ".." in Path(source.archive_name).parts:
                raise ValueError("Archive member names must not traverse directories")
            seen_names.add(source.archive_name)
            path = _resolved_regular_file(
                source.path,
                private=source.private,
                maximum_size=source.maximum_size,
            )
            before = path.stat()
            with path.open("rb") as raw:
                reader = HashingReader(raw)
                archive.addfile(_tar_info(source.archive_name, before.st_size, timestamp), reader)
            after = path.stat()
            stable_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            current_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if reader.bytes_read != before.st_size or current_identity != stable_identity:
                raise RuntimeError(f"Input changed while it was archived: {source.archive_name}")
            entries.append(
                {
                    "name": source.archive_name,
                    "sha256": reader.hexdigest,
                    "size": reader.bytes_read,
                }
            )

        manifest: dict[str, Any] = {
            "schema": ARCHIVE_SCHEMA,
            "created_at": created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "release_commit": release_commit,
            "files": entries,
        }
        manifest_payload = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        archive.addfile(
            _tar_info("manifest.json", len(manifest_payload), timestamp),
            io.BytesIO(manifest_payload),
        )
    return manifest


def _safe_process_environment() -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "SSH_AUTH_SOCK", "TZ")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def create_encrypted_snapshot(
    sources: Sequence[ArchiveSource],
    *,
    release_commit: str,
    recipient_file: Path,
    snapshot_directory: Path,
    created_at: datetime | None = None,
    age_binary: str = "age",
    repository_lock: RepositoryLock | None = None,
) -> Path:
    """Atomically create an age-encrypted tar/gzip snapshot."""
    recipient = _resolved_regular_file(recipient_file, private=False, maximum_size=64 * 1024)
    destination = snapshot_directory.resolve(strict=True)
    when = (created_at or datetime.now(UTC)).astimezone(UTC)
    filename = f"{SNAPSHOT_PREFIX}{when.strftime('%Y%m%dT%H%M%SZ')}{SNAPSHOT_SUFFIX}"
    final_path = destination / filename
    temporary_path = destination / f".{filename}.tmp-{os.getpid()}"
    if final_path.exists() or temporary_path.exists():
        raise FileExistsError(f"Snapshot already exists for this timestamp: {filename}")

    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(descriptor, "wb") as encrypted_output:
            process = subprocess.Popen(  # noqa: S603 - no shell; every argument is separated
                [age_binary, "--encrypt", "--recipients-file", str(recipient)],
                stdin=subprocess.PIPE,
                stdout=encrypted_output,
                stderr=subprocess.PIPE,
                env=_safe_process_environment(),
                **_critical_child_options(repository_lock),
            )
            if process.stdin is None:
                raise RuntimeError("age stdin was not created")
            try:
                write_recovery_stream(
                    process.stdin,
                    sources,
                    release_commit=release_commit,
                    created_at=when,
                )
            finally:
                process.stdin.close()
                process.stdin = None
            _, stderr = process.communicate()
            if process.returncode != 0:
                reason = stderr.decode("utf-8", errors="replace").strip()[:2048]
                raise RuntimeError(f"age encryption failed: {reason or 'no diagnostic'}")
            encrypted_output.flush()
            os.fsync(encrypted_output.fileno())
            encrypted_size = os.fstat(encrypted_output.fileno()).st_size
            if encrypted_size < 1 or encrypted_size > MAX_GIT_SNAPSHOT_BYTES:
                raise ValueError("Encrypted snapshot exceeds the bounded Git artifact size")
        os.chmod(temporary_path, 0o600)
        os.link(temporary_path, final_path)
        temporary_path.unlink()
        if os.name == "posix":
            directory_fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return final_path
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary_path.unlink(missing_ok=True)
        raise


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _check_lock_directory(details: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise ValueError("Disaster-recovery repository directory is unsafe")


def _check_lock_file(details: os.stat_result) -> None:
    if stat.S_ISDIR(details.st_mode):
        raise ValueError(
            "Legacy disaster-recovery directory lock requires an operator migration; "
            "first verify that no older backup publisher is running"
        )
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise ValueError("Disaster-recovery lock file is unsafe")


@dataclass
class RepositoryLock:
    """Explicit inheritance authority for this publisher's critical children only."""

    repository: Path
    repository_fd: int
    git_fd: int
    descriptor: int
    owner_pid: int
    active: bool = True

    def check_identity(self) -> None:
        if not self.active or os.getpid() != self.owner_pid:
            raise RuntimeError("Disaster-recovery repository lock is not owned by this publisher")
        repository = os.fstat(self.repository_fd)
        git_directory = os.fstat(self.git_fd)
        file = os.fstat(self.descriptor)
        for details in (repository, git_directory):
            _check_lock_directory(details)
        _check_lock_file(file)
        if (
            not _same_file(repository, self.repository.lstat())
            or not _same_file(
                git_directory, os.stat(".git", dir_fd=self.repository_fd, follow_symlinks=False)
            )
            or not _same_file(
                file, os.stat(REPOSITORY_LOCK_NAME, dir_fd=self.git_fd, follow_symlinks=False)
            )
        ):
            raise ValueError("Disaster-recovery lock namespace changed while acquiring ownership")

    def child_options(self) -> dict[str, Any]:
        self.check_identity()
        # pass_fds intentionally shares the flock's open file description. The
        # descriptor remains non-inheritable for every other subprocess.
        return {"pass_fds": (self.descriptor,), "close_fds": True}


def _critical_child_options(repository_lock: RepositoryLock | None) -> dict[str, Any]:
    return repository_lock.child_options() if repository_lock is not None else {}


@contextmanager
def exclusive_repository_lock(repository: Path) -> Iterator[RepositoryLock]:
    """Hold a persistent Linux flock; closing the last inherited fd releases it."""
    if sys.platform != "linux":
        raise RuntimeError("Disaster-recovery repository locking requires Linux flock")
    # Keep portable archive/Git helpers importable without pretending to lock on Windows.
    import fcntl

    repository_fd = git_fd = descriptor = None
    ownership: RepositoryLock | None = None
    entered = False
    try:
        target = repository.resolve(strict=True)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        repository_fd = os.open(target, directory_flags)
        _check_lock_directory(os.fstat(repository_fd))
        git_fd = os.open(".git", directory_flags, dir_fd=repository_fd)
        _check_lock_directory(os.fstat(git_fd))
        try:
            before = os.stat(REPOSITORY_LOCK_NAME, dir_fd=git_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None:
            _check_lock_file(before)
        descriptor = os.open(
            REPOSITORY_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=git_fd,
        )
        opened = os.fstat(descriptor)
        _check_lock_file(opened)
        if before is not None and not _same_file(before, opened):
            raise ValueError("Disaster-recovery lock file changed while opening")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("Another disaster-recovery backup is already running") from error
            raise RuntimeError("Unable to acquire disaster-recovery repository flock") from error
        ownership = RepositoryLock(target, repository_fd, git_fd, descriptor, os.getpid())
        ownership.check_identity()
        entered = True
        yield ownership
    except OSError as error:
        if entered:
            raise
        raise RuntimeError(
            "Unable to open or validate disaster-recovery repository lock"
        ) from error
    finally:
        if ownership is not None:
            ownership.active = False
        # Do not LOCK_UN: an owned age/Git child may still hold this same open
        # file description after an exception or parent death. Never unlink it.
        for opened_fd in (descriptor, git_fd, repository_fd):
            if opened_fd is not None:
                os.close(opened_fd)


def _git(
    repository: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
    run: RunCommand = subprocess.run,
    repository_lock: RepositoryLock | None = None,
) -> str:
    result = run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=_safe_process_environment(),
        **_critical_child_options(repository_lock),
    )
    if check and result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()[:2048]
        raise RuntimeError(f"Git command failed: {diagnostic or 'no diagnostic'}")
    return result.stdout.strip()


def validate_private_git_repository(
    repository: Path,
    source_repository: Path,
    *,
    branch: str,
    remote: str,
    run: RunCommand = subprocess.run,
) -> Path:
    """Fail closed unless this is a separate, clean, explicitly attested DR repository."""
    target = repository.resolve(strict=True)
    source = source_repository.resolve(strict=True)
    if target == source:
        raise ValueError("Disaster-recovery snapshots require a separate repository")
    if not (target / ".git").exists():
        raise ValueError("Disaster-recovery destination is not a Git worktree")
    confirmation = _git(
        target,
        ["config", "--local", "--get", PRIVATE_REPOSITORY_CONFIG],
        run=run,
    )
    if confirmation != PRIVATE_REPOSITORY_VALUE:
        raise ValueError("Disaster-recovery repository has not been confirmed private")
    remote_url = _git(target, ["remote", "get-url", "--push", remote], run=run)
    if not SSH_REMOTE_PATTERN.fullmatch(remote_url):
        raise ValueError("Disaster-recovery Git remote must use key-authenticated SSH")
    source_remote = _git(
        source,
        ["remote", "get-url", "--push", "origin"],
        check=False,
        run=run,
    )
    if source_remote and remote_url == source_remote:
        raise ValueError("Disaster-recovery remote must differ from the public source remote")
    current_branch = _git(target, ["branch", "--show-current"], run=run)
    if current_branch != branch:
        raise ValueError(f"Expected disaster-recovery branch {branch!r}")
    if _git(target, ["status", "--porcelain", "--untracked-files=all"], run=run):
        raise ValueError("Disaster-recovery repository must be clean before backup")
    tracked = _git(target, ["ls-files", "-z"], run=run)
    tracked_names = [name for name in tracked.split("\0") if name]
    if any(not name.endswith(SNAPSHOT_SUFFIX) for name in tracked_names):
        raise ValueError("Disaster-recovery repository may track only encrypted snapshots")
    return target


def publish_snapshot(
    repository: Path,
    snapshot: Path,
    *,
    branch: str,
    remote: str,
    run: RunCommand = subprocess.run,
    repository_lock: RepositoryLock | None = None,
) -> None:
    target = repository.resolve(strict=True)
    artifact = snapshot.resolve(strict=True)
    try:
        relative = artifact.relative_to(target)
    except ValueError as error:
        raise ValueError("Encrypted snapshot must be inside the DR repository") from error
    if not relative.as_posix().endswith(SNAPSHOT_SUFFIX):
        raise ValueError("Unexpected encrypted snapshot filename")
    _git(target, ["add", "--", relative.as_posix()], run=run, repository_lock=repository_lock)
    staged = _git(target, ["diff", "--cached", "--name-only", "-z"], run=run)
    if [name for name in staged.split("\0") if name] != [relative.as_posix()]:
        raise RuntimeError("Refusing to commit anything except the new encrypted snapshot")
    timestamp = artifact.name.removeprefix(SNAPSHOT_PREFIX).removesuffix(SNAPSHOT_SUFFIX)
    _git(
        target,
        ["commit", "-m", f"Encrypted recovery snapshot {timestamp}"],
        run=run,
        repository_lock=repository_lock,
    )
    _git(
        target,
        ["push", "--porcelain", remote, f"HEAD:refs/heads/{branch}"],
        run=run,
        repository_lock=repository_lock,
    )


def _is_tmpfs(path: Path) -> bool:
    if os.name != "posix":
        return False
    resolved = path.resolve(strict=True)
    best_match = Path("/")
    best_type = ""
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        after_fields = after.split()
        if len(fields) < 5 or not after_fields:
            continue
        mountpoint = Path(fields[4].replace("\\040", " "))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        if len(mountpoint.parts) >= len(best_match.parts):
            best_match = mountpoint
            best_type = after_fields[0]
    return best_type == "tmpfs"


def _sources_from_args(args: argparse.Namespace) -> tuple[ArchiveSource, ...]:
    database = _resolved_regular_file(
        args.database_backup,
        private=True,
        maximum_size=10 * 1024 * 1024 * 1024,
    )
    if not _is_tmpfs(database):
        raise ValueError("SQLite export must be staged on tmpfs, normally below /run")
    if not database.name.startswith("adojapan-restream-") or database.suffix != ".db":
        raise ValueError("Unexpected SQLite backup filename")
    _verify_sqlite_backup(database)
    return (
        ArchiveSource("control-plane/restream.db", database, 10 * 1024**3, True),
        ArchiveSource("control-plane/environment", args.environment, 1024**2, True),
        ArchiveSource(
            "control-plane/bootstrap-worker-secret", args.bootstrap_secret, 64 * 1024, True
        ),
        ArchiveSource(
            "control-plane/reverse-proxy-site.conf", args.proxy_config, 4 * 1024**2, False
        ),
    )


def _release_commit(source_repository: Path) -> str:
    source = source_repository.resolve(strict=True)
    if _git(source, ["status", "--porcelain", "--untracked-files=no"]):
        raise ValueError("Source repository has tracked changes outside its release commit")
    commit = _git(source, ["rev-parse", "HEAD"])
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("Unable to resolve the deployed release commit")
    return commit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-backup", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--bootstrap-secret", type=Path, required=True)
    parser.add_argument("--proxy-config", type=Path, required=True)
    parser.add_argument("--recipient-file", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--source-repository", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--consume-database-backup", action="store_true")
    args = parser.parse_args()

    get_effective_user_id = getattr(os, "geteuid", None)
    if os.name != "posix" or get_effective_user_id is None or get_effective_user_id() != 0:
        raise SystemExit("Run the disaster-recovery backup as root on Linux")
    with exclusive_repository_lock(args.repository) as repository_lock:
        repository = validate_private_git_repository(
            args.repository,
            args.source_repository,
            branch=args.branch,
            remote=args.remote,
        )
        snapshot_directory = repository / "snapshots"
        snapshot_directory.mkdir(mode=0o700, exist_ok=True)
        sources = _sources_from_args(args)
        try:
            snapshot = create_encrypted_snapshot(
                sources,
                release_commit=_release_commit(args.source_repository),
                recipient_file=args.recipient_file,
                snapshot_directory=snapshot_directory,
                repository_lock=repository_lock,
            )
        finally:
            if args.consume_database_backup:
                args.database_backup.resolve(strict=False).unlink(missing_ok=True)
        publish_snapshot(
            repository,
            snapshot,
            branch=args.branch,
            remote=args.remote,
            repository_lock=repository_lock,
        )
    print("Encrypted disaster-recovery snapshot pushed successfully")


if __name__ == "__main__":
    main()
