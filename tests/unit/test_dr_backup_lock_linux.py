"""Real Linux flock/process tests; never substitute an in-memory lock on Windows."""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import dr_backup

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux flock/process contract")
ROOT = Path(__file__).resolve().parents[2]
DEADLINE = 8
HOLDER = """
import json, os, sys
from pathlib import Path
from scripts.dr_backup import exclusive_repository_lock
with exclusive_repository_lock(Path(sys.argv[1])) as lock:
    print(json.dumps({'event': 'ready', 'inode': os.fstat(lock.descriptor).st_ino,
                      'inheritable': os.get_inheritable(lock.descriptor)}), flush=True)
    sys.stdin.readline()
print(json.dumps({'event': 'released'}), flush=True)
"""
CONTENDER = """
import sys
from pathlib import Path
from scripts.dr_backup import exclusive_repository_lock
try:
    with exclusive_repository_lock(Path(sys.argv[1])):
        print('acquired')
except RuntimeError as error:
    print(str(error))
    raise SystemExit(3)
"""
# Only synthetic children are executed. The actual production age/_git helpers
# must explicitly forward the lock descriptor before these processes can hold it.
CRITICAL_PARENT = """
import json, os, subprocess, sys
from pathlib import Path
from scripts import dr_backup
repo, mode = Path(sys.argv[1]), sys.argv[2]
release, status = int(sys.argv[3]), int(sys.argv[4])
child_source = '''
import json, os, sys
lockfd, release, status = map(int, sys.argv[1:4])
expected = tuple(map(int, sys.argv[4:6]))
try:
    info = os.fstat(lockfd)
    inherited = (info.st_dev, info.st_ino) == expected
except OSError:
    inherited = False
if sys.argv[6] == 'age':
    sys.stdin.buffer.read()
    sys.stdout.buffer.write(b'synthetic-ciphertext')
    sys.stdout.buffer.flush()
os.write(status, (json.dumps({'event':'child-ready','pid':os.getpid(),
                            'inherited':inherited})+chr(10)).encode())
assert os.read(release, 1) == b'x'
'''
with dr_backup.exclusive_repository_lock(repo) as lock:
    info = os.fstat(lock.descriptor)
    command = [sys.executable, '-c', child_source, str(lock.descriptor), str(release),
               str(status), str(info.st_dev), str(info.st_ino), mode]
    def child_options(options):
        inherited = options.get('pass_fds', ())
        assert inherited == (lock.descriptor,)
        options['pass_fds'] = (*inherited, release, status)
        return options
    real_popen = subprocess.Popen
    def spawn_child(options):
        child = real_popen(command, **options)
        os.write(status, (json.dumps({'event':'child-spawned','pid':child.pid})+chr(10)).encode())
        return child
    if mode == 'age':
        def fake_age(arguments, **options):
            assert arguments[0] == 'age'
            return spawn_child(child_options(options))
        dr_backup.subprocess.Popen = fake_age
        dr_backup.create_encrypted_snapshot(
            [dr_backup.ArchiveSource('control-plane/environment', repo/'input', 1024, True)],
            release_commit='a'*40, recipient_file=repo/'recipient',
            snapshot_directory=repo/'snapshots', repository_lock=lock)
    elif mode == 'git':
        def fake_git(arguments, **options):
            assert arguments[3] == 'commit'
            options.pop('check')
            options.pop('capture_output')
            child = spawn_child(dict(child_options(options), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE))
            stdout, stderr = child.communicate()
            return subprocess.CompletedProcess(arguments, child.returncode, stdout, stderr)
        dr_backup._git(repo, ['commit', '-m', 'synthetic'], run=fake_git, repository_lock=lock)
    elif mode == 'close':
        options = child_options(lock.child_options())
        spawn_child(dict(options, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL))
    else:
        child = spawn_child({'pass_fds':(release, status)})
        assert child.wait() == 0
"""


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "dr"
    repository.mkdir(mode=0o700)
    (repository / ".git").mkdir(mode=0o700)
    return repository


def start(source: str, *arguments: str, pass_fds: tuple[int, ...] = ()) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 -- fixed synthetic fixture scripts only
        [sys.executable, "-u", "-c", source, *arguments],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
    )


def ready(descriptor: int) -> dict:
    deadline = time.monotonic() + DEADLINE
    line = bytearray()
    while len(line) < 1024:
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([descriptor], [], [], remaining)[0], (
            "READY deadline exceeded"
        )
        value = os.read(descriptor, 1)
        assert value, "Child exited without READY"
        line.extend(value)
        if value == b"\n":
            return json.loads(line)
    pytest.fail("Oversized child message")


def finish(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=DEADLINE)


def contend(repository: Path, *, acquired: bool) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed synthetic contender; no Git or network
        [sys.executable, "-c", CONTENDER, str(repository)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )
    assert result.returncode == (0 if acquired else 3), result.stderr
    assert result.stdout.strip() == (
        "acquired" if acquired else "Another disaster-recovery backup is already running"
    )


@pytest.mark.parametrize("crash", [False, True])
def test_process_contention_release_and_sigkill_reacquire(repository: Path, crash: bool) -> None:
    owner = start(HOLDER, str(repository))
    try:
        assert owner.stdout is not None and owner.stdin is not None
        message = ready(owner.stdout.fileno())
        assert message["event"] == "ready" and message["inheritable"] is False
        contend(repository, acquired=False)
        if crash:
            owner.send_signal(signal.SIGKILL)
            assert owner.wait(timeout=DEADLINE) == -signal.SIGKILL
        else:
            owner.stdin.write("release\n")
            owner.stdin.flush()
            assert ready(owner.stdout.fileno())["event"] == "released"
            assert owner.wait(timeout=DEADLINE) == 0
        path = repository / ".git" / dr_backup.REPOSITORY_LOCK_NAME
        assert path.stat().st_ino == message["inode"]
        for _ in range(3):
            contend(repository, acquired=True)
            assert path.stat().st_ino == message["inode"]
    finally:
        finish(owner)


def test_existing_file_exception_and_handle_lifetime(repository: Path) -> None:
    path = repository / ".git" / dr_backup.REPOSITORY_LOCK_NAME
    path.touch(mode=0o600)
    inode = path.stat().st_ino
    with (
        pytest.raises(OSError, match="body failure"),
        dr_backup.exclusive_repository_lock(repository) as lock,
    ):
        assert lock.child_options()["pass_fds"] == (lock.descriptor,)
        raise OSError("body failure")
    assert path.stat().st_ino == inode
    with pytest.raises(RuntimeError, match="not owned"):
        lock.child_options()
    contend(repository, acquired=True)


@pytest.mark.parametrize("mode", ["git", "age", "unrelated", "close"])
def test_parent_death_retains_only_explicit_critical_child_lock(
    repository: Path, mode: str
) -> None:
    (repository / "input").write_bytes(b"synthetic-input")
    (repository / "input").chmod(0o600)
    (repository / "recipient").write_text("synthetic-public-recipient\n")
    (repository / "snapshots").mkdir()
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    parent = start(
        CRITICAL_PARENT,
        str(repository),
        mode,
        str(release_read),
        str(status_write),
        pass_fds=(release_read, status_write),
    )
    child_pidfd = None
    child_pid = None
    try:
        os.close(release_read)
        os.close(status_write)
        events = set()
        for _ in range(2):
            message = ready(status_read)
            events.add(message["event"])
            if child_pidfd is None:
                child_pid = message["pid"]
                child_pidfd = os.pidfd_open(child_pid)
            assert message["pid"] == child_pid
            if message["event"] == "child-ready":
                assert message["inherited"] is (mode != "unrelated")
        assert events == {"child-spawned", "child-ready"}
        if mode == "close":
            assert parent.wait(timeout=DEADLINE) == 0
        else:
            parent.send_signal(signal.SIGKILL)
            assert parent.wait(timeout=DEADLINE) == -signal.SIGKILL
        contend(repository, acquired=(mode == "unrelated"))
        os.write(release_write, b"x")
        assert select.select([child_pidfd], [], [], DEADLINE)[0], "Owned child exit deadline"
        contend(repository, acquired=True)
    finally:
        if child_pidfd is not None:
            if not select.select([child_pidfd], [], [], 0)[0]:
                signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
                assert select.select([child_pidfd], [], [], DEADLINE)[0]
            os.close(child_pidfd)
        finish(parent)
        os.close(release_write)
        os.close(status_read)


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "fifo", "permissions", "hardlink"])
def test_unsafe_lock_and_legacy_directory_fail_closed(repository: Path, unsafe: str) -> None:
    path = repository / ".git" / dr_backup.REPOSITORY_LOCK_NAME
    other = repository / "other"
    other.touch(mode=0o600)
    if unsafe == "symlink":
        path.symlink_to(other)
    elif unsafe == "directory":
        path.mkdir()
    elif unsafe == "fifo":
        os.mkfifo(path, 0o600)
    elif unsafe == "permissions":
        path.touch(mode=0o644)
        path.chmod(0o644)
    else:
        os.link(other, path)
    before = path.lstat()
    expected = "Legacy disaster-recovery directory lock" if unsafe == "directory" else "unsafe"
    with (
        pytest.raises(ValueError, match=expected),
        dr_backup.exclusive_repository_lock(repository),
    ):
        pytest.fail("Unsafe lock admitted a publisher")
    after = path.lstat()
    assert (before.st_ino, before.st_mode, before.st_uid) == (
        after.st_ino,
        after.st_mode,
        after.st_uid,
    )


def test_lock_owner_is_checked(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = repository / ".git" / dr_backup.REPOSITORY_LOCK_NAME
    path.touch(mode=0o600)
    details = path.stat()
    monkeypatch.setattr(os, "geteuid", lambda: details.st_uid + 1)
    with pytest.raises(ValueError, match="lock file is unsafe"):
        dr_backup._check_lock_file(details)


def test_file_substitution_between_stat_and_open_is_rejected(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repository / ".git" / dr_backup.REPOSITORY_LOCK_NAME
    path.touch(mode=0o600)
    original = os.open

    def replaced(file: object, flags: int, *arguments: object, **options: object) -> int:
        if file == dr_backup.REPOSITORY_LOCK_NAME:
            path.rename(path.with_name("original-lock"))
            path.touch(mode=0o600)
        return original(file, flags, *arguments, **options)

    monkeypatch.setattr(os, "open", replaced)
    with (
        pytest.raises(ValueError, match="changed while opening"),
        dr_backup.exclusive_repository_lock(repository),
    ):
        pytest.fail("Replaced lock admitted a publisher")


@pytest.mark.parametrize("unsafe", ["symlink", "writable"])
def test_unsafe_git_directory_is_not_adopted(repository: Path, unsafe: str) -> None:
    git = repository / ".git"
    if unsafe == "symlink":
        target = repository / "other-git"
        git.rename(target)
        git.symlink_to(target, target_is_directory=True)
        expected = RuntimeError
    else:
        git.chmod(0o777)
        expected = ValueError
    with pytest.raises(expected), dr_backup.exclusive_repository_lock(repository):
        pytest.fail("Unsafe Git directory admitted a publisher")
    assert not (git / dr_backup.REPOSITORY_LOCK_NAME).exists()


@pytest.mark.parametrize("replace", ["lock", "git"])
def test_namespace_substitution_after_flock_never_enters(
    repository: Path, replace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    original = fcntl.flock
    git = repository / ".git"
    lock = git / dr_backup.REPOSITORY_LOCK_NAME

    def replaced(descriptor: int, operation: int) -> None:
        original(descriptor, operation)
        target = lock if replace == "lock" else git
        target.rename(target.with_name(target.name + ".old"))
        if replace == "lock":
            target.touch(mode=0o600)
        else:
            target.mkdir(mode=0o700)

    monkeypatch.setattr(fcntl, "flock", replaced)
    with (
        pytest.raises(ValueError, match="namespace changed"),
        dr_backup.exclusive_repository_lock(repository),
    ):
        pytest.fail("Replaced lock namespace admitted a publisher")


def test_open_and_flock_errors_are_not_reported_as_contention(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    def unavailable(*arguments: object) -> None:
        raise OSError(errno.ENOSYS, "synthetic unsupported filesystem")

    monkeypatch.setattr(fcntl, "flock", unavailable)
    with (
        pytest.raises(RuntimeError, match="Unable to acquire"),
        dr_backup.exclusive_repository_lock(repository),
    ):
        pytest.fail("Failed flock admitted a publisher")
    with (
        pytest.raises(RuntimeError, match="Unable to open"),
        dr_backup.exclusive_repository_lock(repository / "missing"),
    ):
        pytest.fail("Missing repository admitted a publisher")
