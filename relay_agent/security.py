"""Credential and private-file primitives shared by the native agent."""

from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from relay_agent.errors import RelayAgentError

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,512}\Z")
_MAX_TOKEN_BYTES = 513


def effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if callable(getter) else 0


@dataclass(frozen=True, slots=True, repr=False)
class SensitiveToken:
    """Bearer credential whose normal string representations are always redacted."""

    _value: str

    @classmethod
    def parse(cls, value: str) -> SensitiveToken:
        if not _TOKEN_PATTERN.fullmatch(value):
            raise RelayAgentError("invalid_credential")
        return cls(value)

    def reveal_for_authorization_header(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SensitiveToken('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


def _open_regular_nofollow(path: Path, flags: int) -> tuple[int, os.stat_result]:
    open_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        fd = os.open(path, open_flags)
        opened = os.fstat(fd)
    except (FileNotFoundError, OSError) as exc:
        raise RelayAgentError("credential_unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(fd)
        raise RelayAgentError("unsafe_credential")
    return fd, opened


def read_private_token(path: Path, *, expected_uid: int | None = None) -> SensitiveToken:
    """Read a permanent token only from an owned, regular, exact-mode-0600 file."""

    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise RelayAgentError("credential_unavailable") from exc
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
        raise RelayAgentError("unsafe_credential")
    fd, metadata = _open_regular_nofollow(path, os.O_RDONLY)
    try:
        owner = effective_uid() if expected_uid is None else expected_uid
        if os.name == "posix" and (metadata.st_uid != owner or metadata.st_mode & 0o777 != 0o600):
            raise RelayAgentError("unsafe_credential")
        if not 0 < metadata.st_size <= _MAX_TOKEN_BYTES:
            raise RelayAgentError("invalid_credential")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(_MAX_TOKEN_BYTES + 1)
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise RelayAgentError("invalid_credential") from exc
        return SensitiveToken.parse(value)
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_private_directory(path: Path, *, expected_uid: int | None = None) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise RelayAgentError("private_storage_unavailable") from exc
    owner = effective_uid() if expected_uid is None else expected_uid
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (os.name == "posix" and metadata.st_uid != owner)
        or (os.name == "posix" and metadata.st_mode & 0o077)
    ):
        raise RelayAgentError("unsafe_private_storage")


def atomic_write_private(path: Path, payload: bytes) -> None:
    """Atomically replace a bounded private file without following links."""

    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise RelayAgentError("private_storage_unavailable") from exc
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
