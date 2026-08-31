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

JOURNAL_VERSION = 1
MAX_JOURNAL_ENTRIES = 64
MAX_JOURNAL_BYTES = 64 * 1024


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
        if not self._path.exists():
            return entries
        try:
            metadata = self._path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (os.name == "posix" and metadata.st_uid != effective_uid())
                or (os.name == "posix" and metadata.st_mode & 0o777 != 0o600)
                or metadata.st_size > MAX_JOURNAL_BYTES
            ):
                raise RelayAgentError("unsafe_journal")
            decoded = json.loads(self._path.read_bytes())
        except RelayAgentError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RelayAgentError("invalid_journal") from exc
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"version", "entries"}
            or decoded["version"] != JOURNAL_VERSION
            or not isinstance(decoded["entries"], list)
            or len(decoded["entries"]) > MAX_JOURNAL_ENTRIES
        ):
            raise RelayAgentError("invalid_journal")
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
                or action
                not in {
                    "STATUS",
                    "START",
                    "STOP",
                    "CONFIGURE_YOUTUBE",
                    "CLEAR_YOUTUBE",
                    "REVEAL_MOBLIN_URL",
                }
                or status not in {"ok", "failed", "conflict"}
                or command_id in entries
            ):
                raise RelayAgentError("invalid_journal")
            completion = RelayCompletion(
                cast(CompletionStatus, status),
                cast(str, raw["completed_at"]),
                RelaySnapshot.parse(raw["safe_result"]),
                None,
            )
            entries[command_id] = (cast(Action, action), completion)
        return entries
