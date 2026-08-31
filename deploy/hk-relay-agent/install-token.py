#!/usr/bin/python3
"""Hidden-input, atomic installer for the permanent relay-agent node token."""

from __future__ import annotations

import getpass
import os
import pwd
import re
import secrets
import stat
import sys
from contextlib import suppress
from pathlib import Path

TARGET = Path("/etc/adojapan-relay-agent/node.token")
AGENT_USER = "restream-agent"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,512}\Z")


def main() -> int:
    if sys.argv[1:] or os.geteuid() != 0:
        return 2
    account = pwd.getpwnam(AGENT_USER)
    parent = TARGET.parent
    metadata = parent.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != account.pw_gid
        or metadata.st_mode & 0o777 != 0o750
    ):
        return 1
    try:
        value = getpass.getpass("Permanent relay node token: ", stream=None).strip()
    except (EOFError, KeyboardInterrupt):
        return 1
    if not TOKEN_PATTERN.fullmatch(value):
        return 1
    temporary = parent / f".node.token.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.fchown(fd, account.pw_uid, account.pw_gid)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(value.encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, TARGET)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        value = ""
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            temporary.unlink()
    print("Relay agent credential saved successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
