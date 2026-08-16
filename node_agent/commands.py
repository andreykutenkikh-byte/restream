"""Fixed, idempotent Stage 4A command execution."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from node_agent.credentials import atomic_write_private
from node_agent.errors import ProtocolError
from node_agent.models import (
    SELF_TEST_CHECKS,
    CommandCompletion,
    NodeCommand,
    NodeSnapshot,
    utc_timestamp,
)

_JOURNAL_VERSION = 1
_MAX_JOURNAL_BYTES = 256 * 1024
_MAX_JOURNAL_ENTRIES = 256
_CONTROL_PROBE_TIMEOUT_SECONDS = 5.0


class SelfTestProbe(Protocol):
    def control_https(self) -> bool: ...

    def dns(self) -> bool: ...

    def data_writable(self) -> bool: ...

    def no_inbound_ports(self) -> bool: ...


class LocalSelfTestProbe:
    """Runs bounded checks without accepting commands, arguments, or executables."""

    def __init__(
        self,
        control_url: str,
        data_dir: Path,
        *,
        allow_insecure_http: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(control_url)
        self._control_is_secure = parsed.scheme == "https" or (
            allow_insecure_http and parsed.scheme == "http"
        )
        self._control_probe_url = f"{control_url.rstrip('/')}/node-api/v1/commands/next?wait=0"
        self._control_host = parsed.hostname or ""
        self._control_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._data_dir = data_dir
        self._transport = transport

    def control_https(self) -> bool:
        if not self._control_is_secure:
            return False
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(_CONTROL_PROBE_TIMEOUT_SECONDS),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    "GET",
                    self._control_probe_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "AdoJapan-Restream-Node-Self-Test/1",
                    },
                ) as response,
            ):
                return response.status_code == 401
        except httpx.HTTPError:
            return False

    def dns(self) -> bool:
        result: list[bool] = []

        def resolve() -> None:
            try:
                addresses = socket.getaddrinfo(
                    self._control_host,
                    self._control_port,
                    type=socket.SOCK_STREAM,
                )
            except OSError:
                result.append(False)
            else:
                result.append(bool(addresses))

        resolver = threading.Thread(target=resolve, name="node-dns-check", daemon=True)
        resolver.start()
        resolver.join(timeout=5)
        return bool(result and result[0])

    def data_writable(self) -> bool:
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".self-test-", dir=self._data_dir, delete=True
            ) as handle:
                handle.write(b"ok")
                handle.flush()
            return True
        except OSError:
            return False

    def no_inbound_ports(self) -> bool:
        inspected_table = False
        for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            try:
                lines = path.read_text(encoding="ascii").splitlines()[1:]
            except FileNotFoundError:
                continue
            except OSError:
                return False
            inspected_table = True
            for line in lines:
                fields = line.split()
                if len(fields) >= 4 and fields[3] == "0A":
                    return False
        return inspected_table


class CommandJournal:
    """A small private journal that prevents re-execution after redelivery or restart."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries = self._load()

    def lookup(self, command: NodeCommand) -> CommandCompletion | None:
        stored = self._entries.get(command.command_id)
        if stored is None:
            return None
        stored_type, completion = stored
        if stored_type != command.command_type:
            raise ProtocolError("command_id_conflict", "command id was reused with another type")
        self._entries.move_to_end(command.command_id)
        return completion

    def record(self, command: NodeCommand, completion: CommandCompletion) -> None:
        existing = self.lookup(command)
        if existing is not None:
            return
        candidate = self._entries.copy()
        candidate[command.command_id] = (command.command_type, completion)
        while len(candidate) > _MAX_JOURNAL_ENTRIES:
            candidate.popitem(last=False)
        serialized_entries: list[dict[str, object]] = []
        for command_id, (command_type, stored_completion) in candidate.items():
            serialized_entries.append(
                {
                    "id": command_id,
                    "command_type": command_type,
                    "completion": stored_completion.to_payload(),
                }
            )
        payload = json.dumps(
            {"version": _JOURNAL_VERSION, "entries": serialized_entries},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise ProtocolError("command_journal_too_large", "command journal is too large")
        atomic_write_private(self._path, payload)
        self._entries = candidate

    def _load(self) -> OrderedDict[str, tuple[str, CommandCompletion]]:
        entries: OrderedDict[str, tuple[str, CommandCompletion]] = OrderedDict()
        if not self._path.exists():
            return entries
        try:
            metadata = self._path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (os.name == "posix" and metadata.st_mode & 0o777 != 0o600)
                or metadata.st_size > _MAX_JOURNAL_BYTES
            ):
                raise ProtocolError("invalid_command_journal", "command journal is unsafe")
            decoded = json.loads(self._path.read_bytes())
        except ProtocolError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError("invalid_command_journal", "command journal is invalid") from exc
        if not isinstance(decoded, dict) or decoded.get("version") != _JOURNAL_VERSION:
            raise ProtocolError("invalid_command_journal", "command journal version is invalid")
        raw_entries = decoded.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_JOURNAL_ENTRIES:
            raise ProtocolError("invalid_command_journal", "command journal entries are invalid")
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise ProtocolError("invalid_command_journal", "command journal entry is invalid")
            command_id = raw_entry.get("id")
            command_type = raw_entry.get("command_type")
            if not isinstance(command_id, str) or command_type not in {"PING", "SELF_TEST"}:
                raise ProtocolError("invalid_command_journal", "command journal entry is invalid")
            command = NodeCommand.parse(
                {
                    "id": command_id,
                    "command_type": command_type,
                    "payload": {},
                    "lease_seconds": 1,
                    "attempt_count": 1,
                }
            )
            if command.command_id in entries:
                raise ProtocolError("invalid_command_journal", "command journal has duplicate ids")
            entries[command.command_id] = (
                command.command_type,
                CommandCompletion.parse_stored(raw_entry.get("completion")),
            )
        return entries


class CommandProcessor:
    """Executes exactly PING and SELF_TEST with no user-controlled arguments."""

    def __init__(
        self,
        *,
        agent_version: str,
        journal: CommandJournal,
        snapshot_supplier: Callable[[], NodeSnapshot],
        self_test_probe: SelfTestProbe,
    ) -> None:
        self._agent_version = agent_version
        self._journal = journal
        self._snapshot_supplier = snapshot_supplier
        self._self_test_probe = self_test_probe

    def process(self, command: NodeCommand) -> CommandCompletion:
        stored = self._journal.lookup(command)
        if stored is not None:
            return stored
        if command.command_type == "PING":
            completion = CommandCompletion(
                status="ok",
                received_at=command.received_at,
                completed_at=utc_timestamp(),
                agent_version=self._agent_version,
            )
        elif command.command_type == "SELF_TEST":
            completion = self._self_test(command)
        else:  # pragma: no cover - NodeCommand validation makes this unreachable
            raise ProtocolError("unsupported_command", "command type is not supported")
        self._journal.record(command, completion)
        return completion

    def _self_test(self, command: NodeCommand) -> CommandCompletion:
        snapshot = self._snapshot_supplier()
        checks = {
            "control_https": self._self_test_probe.control_https(),
            "dns": self._self_test_probe.dns(),
            "ffmpeg": snapshot.ffmpeg_version is not None,
            "ffprobe": snapshot.ffprobe_version is not None,
            "memory": snapshot.memory_total_bytes > 0 and snapshot.memory_available_bytes > 0,
            "disk": snapshot.disk_total_bytes > 0 and snapshot.disk_free_bytes > 0,
            "data_writable": self._self_test_probe.data_writable(),
            "no_inbound_ports": self._self_test_probe.no_inbound_ports(),
        }
        if tuple(checks) != SELF_TEST_CHECKS:
            raise ProtocolError("invalid_command_result", "self-test checks are invalid")
        return CommandCompletion(
            status="ok" if all(checks.values()) else "failed",
            received_at=None,
            completed_at=utc_timestamp(),
            agent_version=self._agent_version,
            checks=checks,
        )
