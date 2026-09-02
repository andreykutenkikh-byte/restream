#!/usr/bin/python3
"""Hidden-input, atomic installer for the loopback HLS reader credential."""

from __future__ import annotations

import getpass
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

TARGET = Path("/etc/adojapan-relay-agent/preview-reader.token")
AGENT_USER = "restream-agent"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,512}\Z")
SERVICE_UNITS = ("moblin-relay.service", "adojapan-relay-agent.service")


def _systemd_property(unit: str, property_name: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed utility and allowlisted arguments
            (
                "/usr/bin/systemctl",
                "show",
                f"--property={property_name}",
                "--value",
                unit,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        value = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    return value if value else None


def _services_are_quiescent() -> bool:
    for unit in SERVICE_UNITS:
        active_state = _systemd_property(unit, "ActiveState")
        main_pid = _systemd_property(unit, "MainPID")
        if (
            active_state not in {"inactive", "failed"}
            or main_pid is None
            or not main_pid.isdecimal()
            or int(main_pid) != 0
        ):
            return False
    return True


def main() -> int:
    arguments = sys.argv[1:]
    if arguments not in ([], ["--generate"]) or os.geteuid() != 0:
        return 2
    if not _services_are_quiescent():
        sys.stderr.write(
            "Stop moblin-relay.service and adojapan-relay-agent.service before rotation.\n"
        )
        return 1
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
    if arguments == ["--generate"]:
        value = secrets.token_urlsafe(48)
    else:
        try:
            value = getpass.getpass("Loopback HLS reader token: ", stream=None).strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not TOKEN_PATTERN.fullmatch(value):
        return 1
    temporary = parent / f".preview-reader.token.{secrets.token_hex(8)}.tmp"
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
    print("Preview reader credential saved successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
