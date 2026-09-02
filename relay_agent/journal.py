"""Idempotency journal containing safe results only, never command payloads or URLs."""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from pathlib import Path
from typing import cast

from relay_agent.errors import RelayAgentError
from relay_agent.models import (
    Action,
    CompletionStatus,
    RelayCommand,
    RelayCompletion,
    RelaySnapshot,
)
from relay_agent.security import atomic_write_private, effective_uid, ensure_private_directory

JOURNAL_VERSION = 2
LEGACY_JOURNAL_VERSION = 1
MAX_JOURNAL_ENTRIES = 64
MAX_JOURNAL_BYTES = 64 * 1024

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
_LEGACY_SNAPSHOT_FIELDS = frozenset(
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


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RelayAgentError("invalid_journal")
        result[key] = value
    return result


class CommandJournal:
    def __init__(self, path: Path) -> None:
        self._path = path
        ensure_private_directory(path.parent)
        self._entries = self._load()

    def lookup(self, command: RelayCommand) -> RelayCompletion | None:
        entry = self._entries.get(command.command_id)
        if entry is None:
            return None
        action, completion = entry
        if action != command.action:
            raise RelayAgentError("journal_command_mismatch")
        self._entries.move_to_end(command.command_id)
        return completion

    def record(self, command: RelayCommand, completion: RelayCompletion) -> None:
        safe_completion = RelayCompletion(
            completion.status,
            completion.completed_at,
            completion.safe_result,
            None,
        )
        candidate = self._entries.copy()
        existing = candidate.get(command.command_id)
        if existing is not None and existing[0] != command.action:
            raise RelayAgentError("journal_command_mismatch")
        candidate[command.command_id] = (command.action, safe_completion)
        candidate.move_to_end(command.command_id)
        while len(candidate) > MAX_JOURNAL_ENTRIES:
            candidate.popitem(last=False)
        entries = [
            {
                "id": command_id,
                "action": action,
                "status": stored.status,
                "completed_at": stored.completed_at,
                "safe_result": stored.safe_result.to_json(),
            }
            for command_id, (action, stored) in candidate.items()
        ]
        payload = json.dumps(
            {"version": JOURNAL_VERSION, "entries": entries},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_JOURNAL_BYTES:
            raise RelayAgentError("journal_too_large")
        atomic_write_private(self._path, payload)
        self._entries = candidate

    def _load(self) -> OrderedDict[str, tuple[Action, RelayCompletion]]:
        entries: OrderedDict[str, tuple[Action, RelayCompletion]] = OrderedDict()
        raw_payload = self._read_payload()
        if raw_payload is None:
            return entries
        try:
            decoded = json.loads(raw_payload, object_pairs_hook=_reject_duplicate_keys)
        except RelayAgentError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RelayAgentError("invalid_journal") from exc
        if not isinstance(decoded, dict) or set(decoded) != {"version", "entries"}:
            raise RelayAgentError("invalid_journal")
        version = decoded["version"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version not in {LEGACY_JOURNAL_VERSION, JOURNAL_VERSION}
            or not isinstance(decoded["entries"], list)
            or len(decoded["entries"]) > MAX_JOURNAL_ENTRIES
        ):
            raise RelayAgentError("invalid_journal")
        allowed_actions = _LEGACY_ACTIONS if version == LEGACY_JOURNAL_VERSION else _CURRENT_ACTIONS
        for raw in decoded["entries"]:
            if not isinstance(raw, dict) or set(raw) != {
                "id",
                "action",
                "status",
                "completed_at",
                "safe_result",
            }:
                raise RelayAgentError("invalid_journal")
            command_id = raw["id"]
            action = raw["action"]
            status = raw["status"]
            if (
                not isinstance(command_id, str)
                or not isinstance(action, str)
                or action not in allowed_actions
                or not isinstance(status, str)
                or status not in {"ok", "failed", "conflict"}
                or command_id in entries
            ):
                raise RelayAgentError("invalid_journal")
            safe_result = raw["safe_result"]
            if version == LEGACY_JOURNAL_VERSION and (
                not isinstance(safe_result, dict) or set(safe_result) != _LEGACY_SNAPSHOT_FIELDS
            ):
                raise RelayAgentError("invalid_journal")
            completion = RelayCompletion(
                cast(CompletionStatus, status),
                cast(str, raw["completed_at"]),
                RelaySnapshot.parse(safe_result),
                None,
            )
            entries[command_id] = (cast(Action, action), completion)
        return entries

    def _read_payload(self) -> bytes | None:
        try:
            before = self._path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RelayAgentError("invalid_journal") from exc
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or (os.name == "posix" and before.st_uid != effective_uid())
            or (os.name == "posix" and before.st_mode & 0o777 != 0o600)
            or before.st_size > MAX_JOURNAL_BYTES
        ):
            raise RelayAgentError("unsafe_journal")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(self._path, flags)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (os.name == "posix" and opened.st_uid != effective_uid())
                or (os.name == "posix" and opened.st_mode & 0o777 != 0o600)
                or opened.st_size > MAX_JOURNAL_BYTES
            ):
                raise RelayAgentError("unsafe_journal")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = handle.read(MAX_JOURNAL_BYTES + 1)
            if len(payload) > MAX_JOURNAL_BYTES or len(payload) != opened.st_size:
                raise RelayAgentError("invalid_journal")
            return payload
        except RelayAgentError:
            raise
        except OSError as exc:
            raise RelayAgentError("unsafe_journal") from exc
        finally:
            if fd >= 0:
                os.close(fd)
