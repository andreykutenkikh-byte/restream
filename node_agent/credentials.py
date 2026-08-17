"""File-only node credential lifecycle."""

from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from node_agent.errors import CredentialError

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,512}\Z")
_MAX_CREDENTIAL_BYTES = 513


@dataclass(frozen=True, slots=True, repr=False)
class SensitiveToken:
    """A token that cannot be exposed accidentally by repr or str."""

    _value: str

    @classmethod
    def parse(cls, value: str) -> SensitiveToken:
        if not _TOKEN_PATTERN.fullmatch(value):
            raise CredentialError("invalid_credential", "credential file contains an invalid token")
        return cls(value)

    def reveal(self) -> str:
        """Return the value only at the HTTP serialization boundary."""

        return self._value

    def __repr__(self) -> str:
        return "SensitiveToken('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CredentialError("unsafe_data_dir", "node data directory must be a real directory")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise CredentialError(
            "unsafe_data_dir", "node data directory permissions could not be secured"
        ) from exc


def atomic_write_private(path: Path, payload: bytes) -> None:
    """Atomically replace a small private file with mode 0600."""

    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise CredentialError("credential_write_failed", "credential could not be stored") from exc
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _read_private_token(path: Path, *, missing_code: str) -> SensitiveToken:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
    except FileNotFoundError as exc:
        if fd is not None:
            os.close(fd)
        raise CredentialError(missing_code, "required credential file is missing") from exc
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise CredentialError(
            "credential_read_failed", "credential file could not be inspected"
        ) from exc
    try:
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialError("unsafe_credential", "credential path must be a regular file")
        if os.name == "posix" and metadata.st_mode & 0o777 != 0o600:
            raise CredentialError("unsafe_credential", "credential file permissions must be 0600")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_CREDENTIAL_BYTES:
            raise CredentialError("invalid_credential", "credential file has an invalid size")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read(_MAX_CREDENTIAL_BYTES + 1)
        value = raw.decode("ascii").strip()
    except CredentialError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CredentialError(
            "credential_read_failed", "credential file could not be read"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    return SensitiveToken.parse(value)


class CredentialStore:
    """Loads file-only credentials and performs one-time enrollment promotion."""

    def __init__(self, enrollment_path: Path, node_path: Path) -> None:
        if enrollment_path.parent != node_path.parent:
            raise CredentialError(
                "invalid_credential_paths", "credential files must share one data directory"
            )
        self._enrollment_path = enrollment_path
        self._node_path = node_path
        _ensure_private_directory(enrollment_path.parent)

    def load_permanent(self) -> SensitiveToken | None:
        if not self._node_path.exists():
            return None
        token = _read_private_token(self._node_path, missing_code="node_token_missing")
        self._delete_consumed_enrollment()
        return token

    def load_enrollment(self) -> SensitiveToken:
        return _read_private_token(self._enrollment_path, missing_code="enrollment_token_missing")

    def promote(self, permanent_token: SensitiveToken) -> None:
        atomic_write_private(self._node_path, f"{permanent_token.reveal()}\n".encode("ascii"))
        self._delete_consumed_enrollment()

    def _delete_consumed_enrollment(self) -> None:
        try:
            self._enrollment_path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialError(
                "enrollment_cleanup_failed", "consumed enrollment credential could not be deleted"
            ) from exc
