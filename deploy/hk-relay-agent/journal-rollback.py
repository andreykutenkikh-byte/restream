#!/usr/bin/python3
"""Prepare and restore a strict legacy-v1 relay command journal rollback point."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import pwd
except ImportError:  # pragma: no cover - the installed helper runs only on Linux
    pwd = None  # type: ignore[assignment]

LIVE_DIRECTORY = Path("/var/lib/adojapan-relay-agent")
LIVE_JOURNAL = LIVE_DIRECTORY / "commands.json"
ROLLBACK_DIRECTORY = Path("/etc/adojapan-relay-agent")
ROLLBACK_JOURNAL = ROLLBACK_DIRECTORY / "commands.v1.rollback.json"

MAX_JOURNAL_BYTES = 64 * 1024
MAX_JOURNAL_ENTRIES = 64
LEGACY_VERSION = 1
CURRENT_VERSION = 2

_LEGACY_ACTIONS = frozenset(
    {
        "STATUS",
        "START",
        "STOP",
        "CONFIGURE_YOUTUBE",
        "CLEAR_YOUTUBE",
        "REVEAL_MOBLIN_URL",
    }
)
_CURRENT_ACTIONS = _LEGACY_ACTIONS | {"CONFIGURE_YOUTUBE_KEY"}
_ENTRY_FIELDS = frozenset({"id", "action", "status", "completed_at", "safe_result"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "service_state",
        "enabled",
        "main_process",
        "srt_listener",
        "source",
        "youtube_forward",
        "overall",
        "youtube_url_configured",
        "youtube_key_configured",
        "healthy",
        "portrait_profile",
        "error_code",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "relay_active",
        "youtube_not_configured",
        "relayctl_failed",
        "invalid_configuration",
        "command_expired",
        "unsupported_command",
        "internal_error",
    }
)
_SERVICE_UNITS = (
    "adojapan-relay-agent.service",
    "adojapan-relay-broker.service",
)


class JournalRollbackError(Exception):
    """A deliberately detail-free, operator-safe rollback failure."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JournalRollbackError
        result[key] = value
    return result


def _strict_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _strict_snapshot(value: object, *, allow_bitrate: bool) -> None:
    if not isinstance(value, dict):
        raise JournalRollbackError
    expected_fields = _SNAPSHOT_FIELDS
    if set(value) != expected_fields and not (
        allow_bitrate and set(value) == expected_fields | {"input_bitrate_bps"}
    ):
        raise JournalRollbackError
    service_state = value["service_state"]
    main_process = value["main_process"]
    srt_listener = value["srt_listener"]
    source = value["source"]
    youtube_forward = value["youtube_forward"]
    overall = value["overall"]
    if not isinstance(service_state, str) or service_state not in {
        "active",
        "inactive",
        "failed",
        "unknown",
    }:
        raise JournalRollbackError
    if not isinstance(main_process, str) or main_process not in {
        "running",
        "stopped",
        "failed",
        "unknown",
    }:
        raise JournalRollbackError
    if not isinstance(srt_listener, str) or srt_listener not in {
        "listening",
        "closed",
        "failed",
        "unknown",
    }:
        raise JournalRollbackError
    if not isinstance(source, str) or source not in {"SLATE", "LIVE", "NONE", "UNKNOWN"}:
        raise JournalRollbackError
    if not isinstance(youtube_forward, str) or youtube_forward not in {
        "active",
        "inactive",
        "connecting",
        "failed",
        "unknown",
    }:
        raise JournalRollbackError
    if not isinstance(overall, str) or overall not in {
        "ok",
        "healthy",
        "degraded",
        "failed",
        "offline",
        "unknown",
    }:
        raise JournalRollbackError
    for field in (
        "enabled",
        "youtube_url_configured",
        "youtube_key_configured",
        "healthy",
        "portrait_profile",
    ):
        if not isinstance(value[field], bool):
            raise JournalRollbackError
    error_code = value["error_code"]
    if error_code is not None and (
        not isinstance(error_code, str) or error_code not in _SAFE_ERROR_CODES
    ):
        raise JournalRollbackError
    if "input_bitrate_bps" in value:
        bitrate = value["input_bitrate_bps"]
        if (
            source != "LIVE"
            or not isinstance(bitrate, int)
            or isinstance(bitrate, bool)
            or not 0 <= bitrate <= 1_000_000_000
        ):
            raise JournalRollbackError


def decode_journal(payload: bytes, *, versions: frozenset[int]) -> dict[str, Any]:
    """Decode a journal using the exact v1 shape and the additive v2 contract."""

    if not 0 < len(payload) <= MAX_JOURNAL_BYTES:
        raise JournalRollbackError
    try:
        decoded = json.loads(payload.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JournalRollbackError from None
    if not isinstance(decoded, dict) or set(decoded) != {"version", "entries"}:
        raise JournalRollbackError
    version = decoded["version"]
    entries = decoded["entries"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in versions
        or not isinstance(entries, list)
        or len(entries) > MAX_JOURNAL_ENTRIES
    ):
        raise JournalRollbackError
    allowed_actions = _LEGACY_ACTIONS if version == LEGACY_VERSION else _CURRENT_ACTIONS
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise JournalRollbackError
        command_id = entry["id"]
        action = entry["action"]
        status = entry["status"]
        if (
            not isinstance(command_id, str)
            or command_id in seen_ids
            or not isinstance(action, str)
            or action not in allowed_actions
            or not isinstance(status, str)
            or status not in {"ok", "failed", "conflict"}
            or not _strict_timestamp(entry["completed_at"])
        ):
            raise JournalRollbackError
        seen_ids.add(command_id)
        _strict_snapshot(entry["safe_result"], allow_bitrate=version == CURRENT_VERSION)
    return decoded


def project_to_legacy_v1(payload: bytes | None) -> bytes:
    """Return a canonical payload accepted by the pre-upgrade strict v1 journal reader."""

    if payload is None:
        decoded: dict[str, Any] = {"version": LEGACY_VERSION, "entries": []}
    else:
        decoded = decode_journal(payload, versions=frozenset({LEGACY_VERSION, CURRENT_VERSION}))
    legacy_entries: list[dict[str, Any]] = []
    for entry in decoded["entries"]:
        if entry["action"] == "CONFIGURE_YOUTUBE_KEY":
            continue
        safe_result = dict(entry["safe_result"])
        safe_result.pop("input_bitrate_bps", None)
        legacy_entries.append(
            {
                "id": entry["id"],
                "action": entry["action"],
                "status": entry["status"],
                "completed_at": entry["completed_at"],
                "safe_result": safe_result,
            }
        )
    result = json.dumps(
        {"version": LEGACY_VERSION, "entries": legacy_entries},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    decode_journal(result, versions=frozenset({LEGACY_VERSION}))
    return result


def _account_ids() -> tuple[int, int]:
    if pwd is None:
        raise JournalRollbackError
    try:
        account = pwd.getpwnam("restream-agent")
    except KeyError:
        raise JournalRollbackError from None
    return account.pw_uid, account.pw_gid


def _validate_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise JournalRollbackError from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise JournalRollbackError


def _path_exists_nofollow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise JournalRollbackError from None
    return True


def _read_private_file(path: Path, *, uid: int, gid: int) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        raise JournalRollbackError from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != uid
        or before.st_gid != gid
        or stat.S_IMODE(before.st_mode) != 0o600
        or not 0 < before.st_size <= MAX_JOURNAL_BYTES
    ):
        raise JournalRollbackError
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != uid
            or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not 0 < opened.st_size <= MAX_JOURNAL_BYTES
        ):
            raise JournalRollbackError
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_JOURNAL_BYTES + 1)
        if len(payload) != opened.st_size or len(payload) > MAX_JOURNAL_BYTES:
            raise JournalRollbackError
        return payload
    except JournalRollbackError:
        raise
    except OSError:
        raise JournalRollbackError from None
    finally:
        if fd >= 0:
            os.close(fd)


def _write_temporary(directory: Path, payload: bytes, *, uid: int, gid: int) -> Path:
    temporary = directory / f".journal-rollback.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise JournalRollbackError from None
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise JournalRollbackError
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except JournalRollbackError:
        raise
    except OSError:
        raise JournalRollbackError from None


def _atomic_create(path: Path, payload: bytes, *, uid: int, gid: int) -> bool:
    temporary = _write_temporary(path.parent, payload, uid=uid, gid=gid)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    except OSError:
        raise JournalRollbackError from None
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, payload: bytes, *, uid: int, gid: int) -> None:
    temporary = _write_temporary(path.parent, payload, uid=uid, gid=gid)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        raise JournalRollbackError from None
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _systemd_property(unit: str, property_name: str) -> str:
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
        )
    except OSError:
        raise JournalRollbackError from None
    if result.returncode != 0 or len(result.stdout) > 32:
        raise JournalRollbackError
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        raise JournalRollbackError from None
    if not value or not value.isascii():
        raise JournalRollbackError
    return value


def _require_quiescent_services() -> None:
    for unit in _SERVICE_UNITS:
        active_state = _systemd_property(unit, "ActiveState")
        main_pid = _systemd_property(unit, "MainPID")
        if (
            active_state not in {"inactive", "failed"}
            or not main_pid.isdecimal()
            or int(main_pid) != 0
        ):
            raise JournalRollbackError


def prepare_rollback_point() -> None:
    _require_quiescent_services()
    agent_uid, agent_gid = _account_ids()
    _validate_directory(ROLLBACK_DIRECTORY, uid=0, gid=agent_gid, mode=0o750)
    _validate_directory(LIVE_DIRECTORY, uid=agent_uid, gid=agent_gid, mode=0o700)
    if _path_exists_nofollow(ROLLBACK_JOURNAL):
        existing = _read_private_file(ROLLBACK_JOURNAL, uid=0, gid=0)
        decode_journal(existing, versions=frozenset({LEGACY_VERSION}))
        return
    live_payload: bytes | None = None
    if _path_exists_nofollow(LIVE_JOURNAL):
        live_payload = _read_private_file(LIVE_JOURNAL, uid=agent_uid, gid=agent_gid)
    legacy_payload = project_to_legacy_v1(live_payload)
    _require_quiescent_services()
    if not _atomic_create(ROLLBACK_JOURNAL, legacy_payload, uid=0, gid=0):
        existing = _read_private_file(ROLLBACK_JOURNAL, uid=0, gid=0)
        decode_journal(existing, versions=frozenset({LEGACY_VERSION}))


def restore_legacy_journal() -> None:
    _require_quiescent_services()
    agent_uid, agent_gid = _account_ids()
    _validate_directory(ROLLBACK_DIRECTORY, uid=0, gid=agent_gid, mode=0o750)
    _validate_directory(LIVE_DIRECTORY, uid=agent_uid, gid=agent_gid, mode=0o700)
    payload = _read_private_file(ROLLBACK_JOURNAL, uid=0, gid=0)
    decode_journal(payload, versions=frozenset({LEGACY_VERSION}))
    if _path_exists_nofollow(LIVE_JOURNAL):
        _read_private_file(LIVE_JOURNAL, uid=agent_uid, gid=agent_gid)
    _require_quiescent_services()
    _atomic_replace(LIVE_JOURNAL, payload, uid=agent_uid, gid=agent_gid)


def main() -> int:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        sys.stderr.write("Run this relay journal helper as root.\n")
        return 2
    arguments = sys.argv[1:]
    executable_name = Path(sys.argv[0]).name
    prepare = arguments == ["--prepare"]
    restore = not arguments and executable_name == "adojapan-relay-restore-v1-journal"
    if not prepare and not restore:
        sys.stderr.write("Invalid relay journal helper invocation.\n")
        return 2
    try:
        if prepare:
            prepare_rollback_point()
        else:
            restore_legacy_journal()
    except Exception:  # noqa: BLE001 - never expose unexpected journal or filesystem details
        sys.stderr.write("Relay journal rollback operation failed safely.\n")
        return 1
    if restore:
        sys.stdout.write("Legacy relay journal restored successfully.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
