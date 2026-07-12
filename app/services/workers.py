"""Independent FFmpeg workers for RTMP/RTMPS destinations.

One supervisor and one direct FFmpeg subprocess are maintained per destination.
The module deliberately knows nothing about the database or encryption layer:
callers inject destination and ingest providers.  This makes process behaviour
testable without FFmpeg and prevents transient worker state becoming a source
of truth for persistent destination configuration.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .mediamtx import normalize_codec

type DestinationId = str | int
type ProviderResult = Awaitable[Any] | Any
type StatusCallback = Callable[[DestinationId, "WorkerStatus"], Awaitable[None] | None]


class WorkerState(StrEnum):
    STOPPED = "stopped"
    WAITING_FOR_INPUT = "waiting_for_input"
    CONNECTING = "connecting"
    LIVE = "live"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DestinationSpec:
    """Decrypted, ephemeral process configuration returned by a provider.

    ``publish_url`` is never copied into status or diagnostic objects.  The
    provider should construct this object only for the short time needed to
    start a process and must not persist plaintext secrets itself.
    """

    destination_id: DestinationId
    input_url: str
    publish_url: str
    secret_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.input_url:
            raise ValueError("input_url must not be empty")
        if not self.publish_url:
            raise ValueError("publish_url must not be empty")

    @classmethod
    def from_parts(
        cls,
        destination_id: DestinationId,
        *,
        input_url: str,
        server_url: str,
        stream_key: str,
        secret_values: Sequence[str] = (),
    ) -> DestinationSpec:
        if not stream_key:
            raise ValueError("stream_key must not be empty")
        publish_url = f"{server_url.rstrip('/')}/{stream_key.lstrip('/')}"
        secrets = tuple(value for value in (*secret_values, stream_key) if value)
        return cls(destination_id, input_url, publish_url, secrets)


@dataclass(frozen=True, slots=True)
class IngestSnapshot:
    """Small worker-facing view of ingest availability and codecs."""

    state: str
    video_codec: str | None = None
    audio_codec: str | None = None
    metadata_known: bool = False

    @property
    def available(self) -> bool:
        return self.state.lower() in {"live", "unstable", "ready", "online"}


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    destination_id: DestinationId
    state: WorkerState
    desired_running: bool
    pid: int | None = None
    started_at: datetime | None = None
    live_since: datetime | None = None
    restart_count: int = 0
    consecutive_failures: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        result["started_at"] = self.started_at.isoformat() if self.started_at else None
        result["live_since"] = self.live_since.isoformat() if self.live_since else None
        result["diagnostics"] = list(self.diagnostics)
        return result


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Bounded exponential reconnect policy."""

    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0
    max_fast_failures: int = 5
    stable_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be at least the initial delay")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.max_fast_failures < 1:
            raise ValueError("max_fast_failures must be positive")
        if self.stable_after_seconds < 0:
            raise ValueError("stable_after_seconds must not be negative")

    def delay_for(self, failure_number: int) -> float:
        """Delay after a 1-based consecutive failure number."""

        exponent = max(0, failure_number - 1)
        delay = self.initial_delay_seconds * (self.multiplier**exponent)
        return min(self.max_delay_seconds, delay)


@dataclass(frozen=True, slots=True)
class WorkerRuntimeConfig:
    ffmpeg_executable: str = "ffmpeg"
    input_poll_seconds: float = 1.0
    process_poll_seconds: float = 0.5
    live_after_seconds: float = 1.0
    terminate_grace_seconds: float = 5.0
    stop_timeout_seconds: float = 8.0
    ingest_error_tolerance: int = 2
    diagnostic_lines: int = 40
    diagnostic_line_chars: int = 500

    def __post_init__(self) -> None:
        if not self.ffmpeg_executable:
            raise ValueError("ffmpeg_executable must not be empty")
        for name in (
            "input_poll_seconds",
            "process_poll_seconds",
            "terminate_grace_seconds",
            "stop_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.live_after_seconds < 0:
            raise ValueError("live_after_seconds must not be negative")
        if self.ingest_error_tolerance < 0:
            raise ValueError("ingest_error_tolerance must not be negative")
        if self.diagnostic_lines < 1 or self.diagnostic_line_chars < 32:
            raise ValueError("diagnostic tail limits are too small")


class ProcessHandle(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def iter_stderr(self) -> AsyncIterator[str]: ...


class ProcessLauncher(Protocol):
    async def spawn(self, argv: Sequence[str]) -> ProcessHandle: ...


class AsyncioProcessHandle:
    """Adapter around ``asyncio.subprocess.Process`` with process-group stop."""

    def __init__(self, process: asyncio.subprocess.Process, *, process_group: bool) -> None:
        self._process = process
        self._process_group = process_group

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()

    def _send_group_signal(self, sig: int) -> bool:
        if not self._process_group or self.pid is None:
            return False
        kill_group: Callable[[int, int], None] | None = getattr(os, "killpg", None)
        if kill_group is None:
            return False
        try:
            kill_group(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def terminate(self) -> None:
        if self.returncode is not None:
            return
        if not self._send_group_signal(signal.SIGTERM):
            with suppress(ProcessLookupError):
                self._process.terminate()

    def kill(self) -> None:
        if self.returncode is not None:
            return
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        if not self._send_group_signal(kill_signal):
            with suppress(ProcessLookupError):
                self._process.kill()

    async def iter_stderr(self) -> AsyncIterator[str]:
        stderr = self._process.stderr
        if stderr is None:
            return
        while True:
            line = await stderr.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\r\n")


class AsyncioProcessLauncher:
    """Production launcher. ``create_subprocess_exec`` never invokes a shell."""

    async def spawn(self, argv: Sequence[str]) -> ProcessHandle:
        if not argv:
            raise ValueError("argv must not be empty")
        use_process_group = os.name != "nt"
        process = await asyncio.create_subprocess_exec(
            *tuple(argv),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=use_process_group,
        )
        return AsyncioProcessHandle(process, process_group=use_process_group)


def build_ffmpeg_argv(
    spec: DestinationSpec, *, ffmpeg_executable: str = "ffmpeg"
) -> tuple[str, ...]:
    """Build the fixed stream-copy command; no user-provided arguments exist."""

    return (
        ffmpeg_executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-i",
        spec.input_url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-f",
        "flv",
        spec.publish_url,
    )


_URL_RE = re.compile(r"(?P<url>(?:rtmps?|https?)://[^\s\]\[\"'<>]+)", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<key>(?:token|key|stream_key|secret|password|passwd|signature|auth)=)[^&\s]+"
)


def redact_url(value: str) -> str:
    """Mask credentials, query values, and the final RTMP path component."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.scheme or not parsed.netloc:
        return "[REDACTED_URL]"

    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "[REDACTED_URL]"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{port}" if port is not None else hostname
    if parsed.username is not None or parsed.password is not None:
        netloc = f"[REDACTED]@{netloc}"

    path = parsed.path
    if parsed.scheme.lower() in {"rtmp", "rtmps"}:
        parts = path.split("/")
        for index in range(len(parts) - 1, -1, -1):
            if parts[index]:
                parts[index] = "[REDACTED]"
                break
        path = "/".join(parts)

    query = urlencode([(key, "[REDACTED]") for key, _ in parse_qsl(parsed.query, True)])
    return urlunsplit((parsed.scheme, netloc, path, query, ""))


def redact_diagnostic(
    value: str,
    *,
    known_urls: Sequence[str] = (),
    secret_values: Sequence[str] = (),
) -> str:
    """Return a diagnostic line safe for bounded storage and UI exposure."""

    result = value
    for url in sorted((item for item in known_urls if item), key=len, reverse=True):
        result = result.replace(url, redact_url(url))

    def replace_url(match: re.Match[str]) -> str:
        candidate = match.group("url")
        trailing = ""
        while candidate and candidate[-1] in ".,;:)":
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        return redact_url(candidate) + trailing

    result = _URL_RE.sub(replace_url, result)
    result = _CREDENTIAL_RE.sub(r"\g<scheme>[REDACTED]@", result)
    result = _QUERY_SECRET_RE.sub(r"\g<key>[REDACTED]", result)
    for secret in sorted((item for item in secret_values if item), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def _mapping_or_attr(value: object, *names: str) -> Any:
    if isinstance(value, Mapping):
        return _first_mapping(value, *names)
    for name in names:
        result = getattr(value, name, None)
        if result is not None:
            return result
    return None


def _first_mapping(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def coerce_destination_spec(value: object, destination_id: DestinationId) -> DestinationSpec:
    """Adapt a provider DTO/mapping to the narrow process configuration."""

    if isinstance(value, DestinationSpec):
        return value
    if value is None:
        raise ValueError("destination configuration was not found")

    resolved_id = _mapping_or_attr(value, "destination_id", "id")
    input_url = _mapping_or_attr(value, "input_url", "source_url", "ingest_url")
    publish_url = _mapping_or_attr(
        value, "publish_url", "output_url", "destination_url", "target_url"
    )
    stream_key = _mapping_or_attr(value, "stream_key", "destination_key", "key")
    server_url = _mapping_or_attr(value, "server_url", "rtmp_url", "base_url")
    if publish_url is None and server_url is not None and stream_key is not None:
        publish_url = f"{str(server_url).rstrip('/')}/{str(stream_key).lstrip('/')}"

    secrets_value = _mapping_or_attr(value, "secret_values", "secrets") or ()
    secrets: tuple[str, ...]
    if isinstance(secrets_value, str):
        secrets = (secrets_value,)
    else:
        secrets = tuple(str(item) for item in secrets_value if item)
    if stream_key:
        secrets = (*secrets, str(stream_key))

    return DestinationSpec(
        destination_id=resolved_id if resolved_id is not None else destination_id,
        input_url=str(input_url or ""),
        publish_url=str(publish_url or ""),
        secret_values=secrets,
    )


def coerce_ingest_snapshot(value: object) -> IngestSnapshot:
    """Adapt bools, mappings, or ``mediamtx.IngestStatus`` objects."""

    if isinstance(value, IngestSnapshot):
        return value
    if isinstance(value, bool):
        return IngestSnapshot("live" if value else "offline")
    if value is None:
        return IngestSnapshot("offline")

    state_value = _mapping_or_attr(value, "state", "status")
    if isinstance(state_value, Enum):
        state_value = state_value.value
    available_value = _mapping_or_attr(value, "available", "is_available", "ready")
    if state_value is None and available_value is not None:
        state_value = "live" if bool(available_value) else "offline"

    metadata = _mapping_or_attr(value, "metadata")
    video_codec = _mapping_or_attr(value, "video_codec", "videoCodec")
    audio_codec = _mapping_or_attr(value, "audio_codec", "audioCodec")
    tracks: object = None
    if metadata is not None:
        video_codec = video_codec or _mapping_or_attr(metadata, "video_codec", "videoCodec")
        audio_codec = audio_codec or _mapping_or_attr(metadata, "audio_codec", "audioCodec")
        tracks = _mapping_or_attr(metadata, "tracks")

    metadata_known = bool(video_codec or audio_codec or tracks)
    return IngestSnapshot(
        state=str(state_value or "offline").lower(),
        video_codec=normalize_codec(video_codec),
        audio_codec=normalize_codec(audio_codec),
        metadata_known=metadata_known,
    )


def check_stream_compatibility(ingest: IngestSnapshot) -> tuple[bool, str | None]:
    """Validate the Stage 1 stream-copy codec contract."""

    if ingest.video_codec and ingest.video_codec != "h264":
        return False, "Incoming video must use H.264 for stream copy"
    if ingest.metadata_known and not ingest.video_codec:
        return False, "Incoming stream does not contain a supported H.264 video track"
    if ingest.audio_codec and ingest.audio_codec != "aac":
        return False, "Incoming audio must use AAC or be absent for stream copy"
    return True, None


@dataclass(slots=True)
class _WorkerSlot:
    destination_id: DestinationId
    diagnostic_limit: int
    state: WorkerState = WorkerState.STOPPED
    desired_running: bool = False
    process: ProcessHandle | None = None
    task: asyncio.Task[None] | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: datetime | None = None
    live_since: datetime | None = None
    process_starts: int = 0
    consecutive_failures: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    diagnostics: deque[str] = field(init=False)

    def __post_init__(self) -> None:
        self.diagnostics = deque(maxlen=self.diagnostic_limit)


class WorkerManager:
    """Lifecycle manager for independent per-destination FFmpeg processes."""

    def __init__(
        self,
        destination_provider: Callable[[DestinationId], ProviderResult],
        ingest_provider: Callable[[], ProviderResult],
        *,
        launcher: ProcessLauncher | None = None,
        reconnect_policy: ReconnectPolicy | None = None,
        runtime: WorkerRuntimeConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self._destination_provider = destination_provider
        self._ingest_provider = ingest_provider
        self._launcher = launcher or AsyncioProcessLauncher()
        self._reconnect = reconnect_policy or ReconnectPolicy()
        self._runtime = runtime or WorkerRuntimeConfig()
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._status_callback = status_callback
        self._slots: dict[DestinationId, _WorkerSlot] = {}
        self._operation_lock = asyncio.Lock()

    def _slot(self, destination_id: DestinationId) -> _WorkerSlot:
        slot = self._slots.get(destination_id)
        if slot is None:
            slot = _WorkerSlot(destination_id, self._runtime.diagnostic_lines)
            self._slots[destination_id] = slot
        return slot

    def _snapshot(self, slot: _WorkerSlot) -> WorkerStatus:
        return WorkerStatus(
            destination_id=slot.destination_id,
            state=slot.state,
            desired_running=slot.desired_running,
            pid=slot.process.pid if slot.process else None,
            started_at=slot.started_at,
            live_since=slot.live_since,
            restart_count=max(0, slot.process_starts - 1),
            consecutive_failures=slot.consecutive_failures,
            last_exit_code=slot.last_exit_code,
            last_error=slot.last_error,
            diagnostics=tuple(slot.diagnostics),
        )

    def status(self, destination_id: DestinationId) -> WorkerStatus:
        """Read current in-memory process state (never persistent config)."""

        return self._snapshot(self._slot(destination_id))

    async def get_status(self, destination_id: DestinationId) -> WorkerStatus:
        """Async alias convenient for FastAPI service methods."""

        return self.status(destination_id)

    def all_statuses(self) -> tuple[WorkerStatus, ...]:
        return tuple(self._snapshot(slot) for slot in self._slots.values())

    async def _emit_status(self, slot: _WorkerSlot) -> None:
        callback = self._status_callback
        if callback is None:
            return
        try:
            result = callback(slot.destination_id, self._snapshot(slot))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # Persistence/notification failures must not terminate an active
            # media process. The next transition provides another opportunity
            # to reconcile the stored presentation state.
            return

    async def _transition(
        self, slot: _WorkerSlot, state: WorkerState, *, force: bool = False
    ) -> None:
        changed = slot.state is not state
        slot.state = state
        if changed or force:
            await self._emit_status(slot)

    async def start(self, destination_id: DestinationId) -> WorkerStatus:
        """Start or idempotently return the existing destination supervisor."""

        while True:
            old_task: asyncio.Task[None] | None = None
            async with self._operation_lock:
                slot = self._slot(destination_id)
                if slot.task is not None and not slot.task.done():
                    if slot.desired_running:
                        return self._snapshot(slot)
                    old_task = slot.task
                else:
                    slot.stop_event = asyncio.Event()
                    slot.wake_event = asyncio.Event()
                    slot.desired_running = True
                    slot.state = WorkerState.CONNECTING
                    slot.consecutive_failures = 0
                    slot.last_error = None
                    slot.last_exit_code = None
                    slot.live_since = None
                    slot.task = asyncio.create_task(
                        self._supervise(slot),
                        name=f"ffmpeg-worker-{destination_id}",
                    )
                    return self._snapshot(slot)
            if old_task is not None:
                await asyncio.shield(old_task)

    async def stop(self, destination_id: DestinationId) -> WorkerStatus:
        """Stop only this destination, escalating SIGTERM to kill if needed."""

        async with self._operation_lock:
            slot = self._slot(destination_id)
            slot.desired_running = False
            slot.stop_event.set()
            slot.wake_event.set()
            task = slot.task if slot.task and not slot.task.done() else None

        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self._runtime.stop_timeout_seconds
                )
            except TimeoutError:
                process = slot.process
                if process is not None:
                    await self._force_kill(process)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        slot.process = None
        await self._transition(slot, WorkerState.STOPPED)
        slot.started_at = None
        slot.live_since = None
        return self._snapshot(slot)

    async def remove(self, destination_id: DestinationId) -> None:
        await self.stop(destination_id)
        async with self._operation_lock:
            self._slots.pop(destination_id, None)

    async def shutdown(self) -> None:
        """Gracefully stop every process owned by this manager."""

        await asyncio.gather(
            *(self.stop(destination_id) for destination_id in tuple(self._slots)),
            return_exceptions=False,
        )

    async def reconcile(self, enabled_destination_ids: Sequence[DestinationId]) -> None:
        """Idempotently restore enabled destinations after backend startup."""

        enabled = set(enabled_destination_ids)
        to_stop = [
            destination_id
            for destination_id, slot in self._slots.items()
            if slot.desired_running and destination_id not in enabled
        ]
        await asyncio.gather(*(self.stop(item) for item in to_stop))
        await asyncio.gather(*(self.start(item) for item in enabled))

    def notify_ingest_changed(self) -> None:
        """Wake waiting workers after a MediaMTX publish/unpublish event."""

        for slot in self._slots.values():
            if slot.desired_running:
                slot.wake_event.set()

    async def _resolve(self, result: ProviderResult) -> Any:
        if inspect.isawaitable(result):
            return await result
        return result

    async def _load_ingest(self) -> IngestSnapshot:
        try:
            value = await self._resolve(self._ingest_provider())
            return coerce_ingest_snapshot(value)
        except asyncio.CancelledError:
            raise
        except Exception:
            return IngestSnapshot("error")

    async def _load_destination(self, destination_id: DestinationId) -> DestinationSpec:
        value = await self._resolve(self._destination_provider(destination_id))
        return coerce_destination_spec(value, destination_id)

    async def _interruptible_wait(self, slot: _WorkerSlot, delay_seconds: float) -> bool:
        """Wait for timeout/config change and return whether stop was requested."""

        if slot.stop_event.is_set() or not slot.desired_running:
            return True
        with suppress(TimeoutError):
            await asyncio.wait_for(slot.wake_event.wait(), timeout=delay_seconds)
        slot.wake_event.clear()
        return slot.stop_event.is_set() or not slot.desired_running

    async def _drain_diagnostics(
        self,
        slot: _WorkerSlot,
        process: ProcessHandle,
        spec: DestinationSpec,
    ) -> None:
        try:
            async for raw_line in process.iter_stderr():
                safe_line = redact_diagnostic(
                    raw_line,
                    known_urls=(spec.input_url, spec.publish_url),
                    secret_values=spec.secret_values,
                )
                if safe_line:
                    slot.diagnostics.append(safe_line[: self._runtime.diagnostic_line_chars])
        except asyncio.CancelledError:
            raise
        except Exception:
            slot.diagnostics.append("FFmpeg diagnostic stream ended unexpectedly")

    async def _finish_diagnostics(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=min(1.0, self._runtime.terminate_grace_seconds),
            )
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _wait_for_exit(self, exit_task: asyncio.Task[int], wait_seconds: float) -> int | None:
        try:
            return await asyncio.wait_for(asyncio.shield(exit_task), timeout=wait_seconds)
        except TimeoutError:
            return None

    async def _terminate_process(
        self, process: ProcessHandle, exit_task: asyncio.Task[int]
    ) -> bool:
        if exit_task.done() or process.returncode is not None:
            await asyncio.gather(exit_task, return_exceptions=True)
            return True
        process.terminate()
        code = await self._wait_for_exit(exit_task, self._runtime.terminate_grace_seconds)
        if code is not None or exit_task.done():
            return True
        process.kill()
        code = await self._wait_for_exit(exit_task, self._runtime.terminate_grace_seconds)
        return code is not None or exit_task.done()

    async def _force_kill(self, process: ProcessHandle) -> None:
        process.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=self._runtime.terminate_grace_seconds)

    async def _monitor_process(
        self,
        slot: _WorkerSlot,
        process: ProcessHandle,
        exit_task: asyncio.Task[int],
        process_started: float,
    ) -> tuple[str, int | None, float]:
        ingest_errors = 0
        while True:
            if slot.stop_event.is_set() or not slot.desired_running:
                stopped = await self._terminate_process(process, exit_task)
                return ("stopped" if stopped else "stop_failed", None, 0.0)

            code = await self._wait_for_exit(exit_task, self._runtime.process_poll_seconds)
            runtime = max(0.0, self._monotonic() - process_started)
            if code is not None or exit_task.done():
                if code is None:
                    code = exit_task.result()
                return "exited", code, runtime

            if runtime >= self._runtime.live_after_seconds and slot.state in {
                WorkerState.CONNECTING,
                WorkerState.RECONNECTING,
            }:
                slot.live_since = self._utcnow()
                await self._transition(slot, WorkerState.LIVE)

            ingest = await self._load_ingest()
            if ingest.state == "error":
                ingest_errors += 1
                if ingest_errors <= self._runtime.ingest_error_tolerance:
                    continue
            else:
                ingest_errors = 0

            if not ingest.available:
                stopped = await self._terminate_process(process, exit_task)
                return ("input_lost" if stopped else "stop_failed", None, runtime)

            compatible, compatibility_error = check_stream_compatibility(ingest)
            if not compatible:
                slot.last_error = compatibility_error
                stopped = await self._terminate_process(process, exit_task)
                return ("incompatible" if stopped else "stop_failed", None, runtime)

    async def _record_start_failure(
        self, slot: _WorkerSlot, failure_count: int, message: str
    ) -> bool:
        slot.consecutive_failures = failure_count
        slot.last_error = message
        if failure_count >= self._reconnect.max_fast_failures:
            slot.desired_running = False
            await self._transition(slot, WorkerState.FAILED, force=True)
            return False
        await self._transition(slot, WorkerState.RECONNECTING, force=True)
        return not await self._interruptible_wait(slot, self._reconnect.delay_for(failure_count))

    async def _supervise(self, slot: _WorkerSlot) -> None:
        failures = 0
        first_process = True
        try:
            await self._transition(slot, slot.state, force=True)
            while slot.desired_running and not slot.stop_event.is_set():
                ingest = await self._load_ingest()
                if not ingest.available:
                    await self._transition(slot, WorkerState.WAITING_FOR_INPUT)
                    slot.live_since = None
                    if await self._interruptible_wait(slot, self._runtime.input_poll_seconds):
                        break
                    continue

                compatible, compatibility_error = check_stream_compatibility(ingest)
                if not compatible:
                    slot.last_error = compatibility_error
                    slot.desired_running = False
                    await self._transition(slot, WorkerState.FAILED, force=True)
                    break

                try:
                    spec = await self._load_destination(slot.destination_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    slot.last_error = "Destination configuration is unavailable"
                    slot.desired_running = False
                    await self._transition(slot, WorkerState.FAILED, force=True)
                    break

                await self._transition(
                    slot,
                    WorkerState.CONNECTING if first_process else WorkerState.RECONNECTING,
                    force=True,
                )
                slot.live_since = None
                argv = build_ffmpeg_argv(spec, ffmpeg_executable=self._runtime.ffmpeg_executable)
                try:
                    process = await self._launcher.spawn(argv)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    if not await self._record_start_failure(
                        slot, failures, "Unable to start FFmpeg process"
                    ):
                        break
                    first_process = False
                    continue

                first_process = False
                slot.process = process
                slot.process_starts += 1
                slot.started_at = self._utcnow()
                process_started = self._monotonic()
                diagnostics_task = asyncio.create_task(
                    self._drain_diagnostics(slot, process, spec),
                    name=f"ffmpeg-diagnostics-{slot.destination_id}",
                )
                exit_task = asyncio.create_task(
                    process.wait(), name=f"ffmpeg-exit-{slot.destination_id}"
                )

                outcome, exit_code, runtime = await self._monitor_process(
                    slot, process, exit_task, process_started
                )
                slot.process = None
                slot.started_at = None
                await self._finish_diagnostics(diagnostics_task)

                if outcome == "stopped":
                    break
                if outcome == "stop_failed":
                    slot.last_error = "FFmpeg process could not be stopped safely"
                    slot.desired_running = False
                    await self._transition(slot, WorkerState.FAILED, force=True)
                    break
                if outcome == "input_lost":
                    failures = 0
                    slot.consecutive_failures = 0
                    await self._transition(slot, WorkerState.WAITING_FOR_INPUT)
                    slot.live_since = None
                    continue
                if outcome == "incompatible":
                    slot.desired_running = False
                    await self._transition(slot, WorkerState.FAILED, force=True)
                    break

                slot.last_exit_code = exit_code
                slot.live_since = None
                if runtime >= self._reconnect.stable_after_seconds:
                    failures = 1
                else:
                    failures += 1
                message = f"FFmpeg exited unexpectedly (code {exit_code})"
                if not await self._record_start_failure(slot, failures, message):
                    break
        except asyncio.CancelledError:
            raise
        finally:
            active_process = slot.process
            if active_process is not None:
                exit_task = asyncio.create_task(active_process.wait())
                await self._terminate_process(active_process, exit_task)
                slot.process = None
            slot.started_at = None
            slot.live_since = None
            if slot.state is not WorkerState.FAILED:
                slot.desired_running = False
                await self._transition(slot, WorkerState.STOPPED)


__all__ = [
    "AsyncioProcessHandle",
    "AsyncioProcessLauncher",
    "DestinationId",
    "DestinationSpec",
    "IngestSnapshot",
    "ProcessHandle",
    "ProcessLauncher",
    "ReconnectPolicy",
    "StatusCallback",
    "WorkerManager",
    "WorkerRuntimeConfig",
    "WorkerState",
    "WorkerStatus",
    "build_ffmpeg_argv",
    "check_stream_compatibility",
    "coerce_destination_spec",
    "coerce_ingest_snapshot",
    "redact_diagnostic",
    "redact_url",
]
