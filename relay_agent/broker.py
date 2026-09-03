"""Root-only, socket-activated broker for the existing ``relayctl`` implementation."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import os
import re
import runpy
import select
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from relay_agent.errors import RelayAgentError
from relay_agent.models import JsonObject, RelaySnapshot
from relay_agent.security import effective_uid

RELAYCTL_PATH = Path("/usr/local/sbin/relayctl")
EXPECTED_AGENT_USER = "restream-agent"
MAX_BROKER_MESSAGE_BYTES = 16 * 1024
MAX_SECRET_RESULT_CHARS = 4096
# The absolute client deadline is carried in every same-host request. Mutating
# workers finish (or are killed and reconciled) before that deadline, leaving a
# final response window. Keep these values below the 120-second command lease.
BROKER_CLIENT_TIMEOUT_SECONDS = 20.0
BROKER_SERVER_IO_TIMEOUT_SECONDS = 2.0
BROKER_SERVER_ACTION_TIMEOUT_SECONDS = 11.0
BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS = 6.0
BROKER_RESPONSE_RESERVE_SECONDS = 1.0
BROKER_MIN_MUTATION_WINDOW_SECONDS = 8.0
_BROKER_READY_BYTE = b"\x01"
_SYSTEMCTL_PATH = "/usr/bin/systemctl"
_MOBLIN_RELAY_SERVICE = "moblin-relay.service"
_RELAY_LOCK_DIRECTORY = Path("/run/lock/moblin-relay")
_RELAY_LOCK_PATH = _RELAY_LOCK_DIRECTORY / "control.lock"
_BITRATE_STATE_PATH = _RELAY_LOCK_DIRECTORY / "input-bitrate.json"
_BITRATE_STALE_AFTER_SECONDS = 15.0
_MAX_INPUT_BITRATE_BPS = 1_000_000_000
_MAX_INPUT_COUNTER = 2**53 - 1
_FINAL_OUTPUT_PATH = "relay-output"
_PR_SET_CHILD_SUBREAPER = 36
_subreaper_enabled = False
_YOUTUBE_HOSTS = frozenset({"a.rtmps.youtube.com", "b.rtmps.youtube.com"})
_ACTIONS = frozenset(
    {
        "status",
        "start",
        "stop",
        "configure_youtube",
        "configure_youtube_key",
        "clear_youtube",
        "reveal_moblin_url",
    }
)
_RELAYCTL_REQUIRED = frozenset(
    {
        "RelayLock",
        "atomic_save_secrets",
        "cmd_show_moblin_url",
        "cmd_start",
        "cmd_stop",
        "get_main_pid",
        "load_secrets",
        "parse_metric_samples",
        "read_metrics",
        "run_quiet",
        "service_allows_reconfiguration",
        "service_is_active",
        "service_is_enabled",
        "validate_youtube",
        "youtube_state",
        "SRT_PATH",
        "SRT_PORT",
    }
)
_RELAYCTL_LEGACY_MOBLIN_REQUIRED = frozenset({"PUBLIC_HOST", "VPN_HOST"})
_MAX_FALLBACK_SRT_URLS = 4


class RelayCtlNamespace(Protocol):
    def __getitem__(self, key: str) -> object: ...


class InputBitrateSampler:
    """Derive safe input bitrate from one MediaMTX publisher counter.

    Broker requests run in short-lived forked workers, so the minimum baseline
    needed for a monotonic delta lives in a root-only runtime file. The raw
    connection label is never persisted or returned; only its SHA-256 identity
    is retained to detect publisher changes.
    """

    def __init__(
        self,
        state_path: Path = _BITRATE_STATE_PATH,
        *,
        clock: Callable[[], float] = time.monotonic,
        stale_after_seconds: float = _BITRATE_STALE_AFTER_SECONDS,
        expected_uid: int = 0,
    ) -> None:
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if not isinstance(expected_uid, int) or isinstance(expected_uid, bool) or expected_uid < 0:
            raise ValueError("expected_uid must be non-negative")
        self._state_path = state_path
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._expected_uid = expected_uid

    def reset(self) -> None:
        with suppress(OSError):
            self._state_path.unlink(missing_ok=True)

    def _load(self) -> tuple[str, int, float] | None:
        try:
            before = self._state_path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_size < 1
                or before.st_size > 512
                or (
                    os.name == "posix"
                    and (before.st_uid != self._expected_uid or before.st_mode & 0o777 != 0o600)
                )
            ):
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._state_path, flags)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    return None
                with os.fdopen(fd, "r", encoding="ascii", closefd=False) as stream:
                    decoded = json.load(stream)
            finally:
                os.close(fd)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or set(decoded) != {
            "connection_identity",
            "bytes_received",
            "observed_at",
        }:
            return None
        identity = decoded["connection_identity"]
        counter = decoded["bytes_received"]
        observed_at = decoded["observed_at"]
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            or not isinstance(counter, int)
            or isinstance(counter, bool)
            or not 0 <= counter <= _MAX_INPUT_COUNTER
            or not isinstance(observed_at, (int, float))
            or isinstance(observed_at, bool)
            or not math.isfinite(float(observed_at))
            or float(observed_at) < 0
        ):
            return None
        return identity, counter, float(observed_at)

    def _save(self, identity: str, counter: int, observed_at: float) -> bool:
        encoded = json.dumps(
            {
                "connection_identity": identity,
                "bytes_received": counter,
                "observed_at": observed_at,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.{time.monotonic_ns()}"
        )
        fd = -1
        try:
            self._state_path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
            directory = self._state_path.parent.lstat()
            if not stat.S_ISDIR(directory.st_mode) or (
                os.name == "posix"
                and (directory.st_uid != self._expected_uid or directory.st_mode & 0o077)
            ):
                return False
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, self._state_path)
            return True
        except OSError:
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def sample(self, *, connection_id: object, bytes_received: object) -> int | None:
        observed_at = self._clock()
        if (
            not isinstance(connection_id, str)
            or not 1 <= len(connection_id) <= 64
            or not connection_id.isascii()
            or not isinstance(bytes_received, (int, float))
            or isinstance(bytes_received, bool)
            or not math.isfinite(float(bytes_received))
            or not float(bytes_received).is_integer()
            or not 0 <= float(bytes_received) <= _MAX_INPUT_COUNTER
            or not isinstance(observed_at, (int, float))
            or isinstance(observed_at, bool)
            or not math.isfinite(float(observed_at))
            or observed_at < 0
        ):
            self.reset()
            return None
        try:
            canonical_id = str(UUID(connection_id))
        except ValueError:
            self.reset()
            return None
        if canonical_id != connection_id.lower():
            self.reset()
            return None
        identity = hashlib.sha256(canonical_id.encode("ascii")).hexdigest()
        counter = int(bytes_received)
        now = float(observed_at)
        previous = self._load()
        if previous is None:
            self.reset()
            self._save(identity, counter, now)
            return None
        previous_identity, previous_counter, previous_at = previous
        elapsed = now - previous_at
        if (
            identity != previous_identity
            or counter < previous_counter
            or elapsed <= 0
            or elapsed >= self._stale_after_seconds
        ):
            self.reset()
            self._save(identity, counter, now)
            return None
        bitrate = round(((counter - previous_counter) * 8) / elapsed)
        if not 0 <= bitrate <= _MAX_INPUT_BITRATE_BPS or not self._save(identity, counter, now):
            self.reset()
            return None
        return bitrate


def _function(namespace: Mapping[str, object], name: str) -> Callable[..., Any]:
    candidate = namespace.get(name)
    if not callable(candidate):
        raise RelayAgentError("relayctl_failed")
    return candidate


def _safe_relayctl_namespace(path: Path = RELAYCTL_PATH) -> Mapping[str, object]:
    """Load the fixed root-owned relayctl source without invoking its CLI entrypoint."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise RelayAgentError("relayctl_failed") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_mode & 0o022
        or not 1 <= before.st_size <= 1024 * 1024
    ):
        raise RelayAgentError("relayctl_failed")
    try:
        loaded = runpy.run_path(str(path), run_name="_adojapan_relayctl_broker")
        after = path.lstat()
    except (OSError, RuntimeError, SystemExit) as exc:
        raise RelayAgentError("relayctl_failed") from exc
    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    ):
        raise RelayAgentError("relayctl_failed")
    if not _RELAYCTL_REQUIRED.issubset(loaded):
        raise RelayAgentError("relayctl_failed")
    for name in _RELAYCTL_REQUIRED - {"SRT_PATH", "SRT_PORT"}:
        if not callable(loaded[name]):
            raise RelayAgentError("relayctl_failed")
    if not callable(loaded.get("build_moblin_urls")) and not (
        _RELAYCTL_LEGACY_MOBLIN_REQUIRED.issubset(loaded)
    ):
        raise RelayAgentError("relayctl_failed")
    return MappingProxyType(loaded)


def validate_official_youtube_endpoint(url: str) -> None:
    """Require an RTMPS YouTube ingest endpoint and the Studio ``/live2`` path."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RelayAgentError("invalid_configuration") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "rtmps"
        or hostname is None
        or not hostname.isascii()
        or hostname not in _YOUTUBE_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/live2"
        or parsed.query
        or parsed.fragment
    ):
        raise RelayAgentError("invalid_configuration")


def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Return Linux PID/UID/GID credentials for a connected Unix socket."""

    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise RelayAgentError("peer_auth_failed")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise RelayAgentError("peer_auth_failed") from exc
    if pid <= 0 or uid < 0 or gid < 0:
        raise RelayAgentError("peer_auth_failed")
    return pid, uid, gid


def peer_is_expected_agent(connection: socket.socket, expected_uid: int) -> bool:
    return peer_credentials(connection)[1] == expected_uid


def _strict_json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RelayAgentError("invalid_request")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise RelayAgentError("invalid_request")


def _read_bounded_json(connection: socket.socket) -> JsonObject:
    body = bytearray()
    while True:
        try:
            chunk = connection.recv(4096)
        except (OSError, TimeoutError) as exc:
            raise RelayAgentError("invalid_request") from exc
        if not chunk:
            break
        if len(chunk) > MAX_BROKER_MESSAGE_BYTES - len(body):
            raise RelayAgentError("request_too_large")
        body.extend(chunk)
    if not body:
        raise RelayAgentError("invalid_request")
    try:
        decoded = json.loads(
            body,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except RelayAgentError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayAgentError("invalid_request") from exc
    if not isinstance(decoded, dict):
        raise RelayAgentError("invalid_request")
    return cast(JsonObject, decoded)


def _capture(function: object) -> tuple[int, str]:
    if not callable(function):
        raise RelayAgentError("relayctl_failed")
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = function()
    except (OSError, SystemExit, ValueError) as exc:
        raise RelayAgentError("relayctl_failed") from exc
    if not isinstance(result, int):
        raise RelayAgentError("relayctl_failed")
    return result, stdout.getvalue()


def _capture_return_code(function: object) -> int:
    return _capture(function)[0]


class RelayBroker:
    """Allowlisted privileged operations over the exact existing relayctl code."""

    def __init__(
        self,
        relayctl: Mapping[str, object] | None = None,
        *,
        bitrate_sampler: InputBitrateSampler | None = None,
    ) -> None:
        self._relayctl = relayctl if relayctl is not None else _safe_relayctl_namespace()
        self._portrait_cache: tuple[int, int, bool] | None = None
        self._bitrate_sampler = bitrate_sampler or InputBitrateSampler()

    def reconciliation_state(self, request: JsonObject) -> tuple[bool, bool] | None:
        """Capture START/STOP state before the worker is armed for mutation."""

        action = request.get("action")
        if not isinstance(action, str) or action not in {"start", "stop"}:
            return None
        if request.get("payload") != {}:
            return None
        try:
            return (
                bool(_function(self._relayctl, "service_is_active")()),
                bool(_function(self._relayctl, "service_is_enabled")()),
            )
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc

    def handle(self, request: object, *, relay_lock_held: bool = False) -> JsonObject:
        if not isinstance(request, dict) or set(request) != {"action", "payload"}:
            return self._result("failed", RelaySnapshot.unavailable("invalid_configuration"))
        action = request.get("action")
        payload = request.get("payload")
        if not isinstance(action, str) or action not in _ACTIONS or not isinstance(payload, dict):
            return self._result("failed", RelaySnapshot.unavailable("unsupported_command"))
        try:
            if action == "status":
                if payload:
                    raise RelayAgentError("invalid_configuration")
                return self._result("ok", self.snapshot())
            if action == "start":
                if payload:
                    raise RelayAgentError("invalid_configuration")
                return self._start(relay_lock_held=relay_lock_held)
            if action == "stop":
                if payload:
                    raise RelayAgentError("invalid_configuration")
                return self._stop(relay_lock_held=relay_lock_held)
            if action == "configure_youtube":
                return self._configure_youtube(payload)
            if action == "configure_youtube_key":
                return self._configure_youtube_key(payload)
            if action == "clear_youtube":
                if payload:
                    raise RelayAgentError("invalid_configuration")
                return self._clear_youtube()
            if action == "reveal_moblin_url":
                if payload:
                    raise RelayAgentError("invalid_configuration")
                return self._reveal_moblin_url()
        except RelayAgentError as exc:
            return self._result("failed", self._snapshot_or_unavailable(exc.code))
        return self._result("failed", RelaySnapshot.unavailable("unsupported_command"))

    @staticmethod
    def _result(
        status: str, snapshot: RelaySnapshot, secret_result: str | None = None
    ) -> JsonObject:
        return {
            "status": status,
            "safe_result": snapshot.to_json(),
            "secret_result": secret_result,
        }

    def _snapshot_or_unavailable(self, code: str) -> RelaySnapshot:
        safe_code = (
            code
            if code
            in {
                "relay_active",
                "youtube_not_configured",
                "relayctl_failed",
                "invalid_configuration",
                "unsupported_command",
                "internal_error",
            }
            else "internal_error"
        )
        try:
            return self.snapshot().with_error(safe_code)
        except RelayAgentError:
            return RelaySnapshot.unavailable(safe_code)

    def snapshot(self) -> RelaySnapshot:
        try:
            active = bool(_function(self._relayctl, "service_is_active")())
            enabled = bool(_function(self._relayctl, "service_is_enabled")())
            data = _function(self._relayctl, "load_secrets")(optional=True)
            has_url, has_key = _function(self._relayctl, "youtube_state")(data)
            service_state = self._service_state(active)
            portrait_profile = self._portrait_profile()
            main_process = self._main_process(active, service_state)
            srt_listener = self._srt_listener(active, service_state)
            source, metrics_ok, path_ready, forward_ok, input_bitrate_bps = self._media_state(
                active
            )
            if forward_ok:
                youtube_forward = "active"
            elif service_state == "inactive":
                youtube_forward = "inactive"
            elif service_state == "failed":
                youtube_forward = "failed"
            elif active and metrics_ok:
                youtube_forward = "connecting"
            else:
                youtube_forward = "unknown"
            healthy = bool(
                active
                and enabled
                and main_process == "running"
                and srt_listener == "listening"
                and metrics_ok
                and path_ready
                and forward_ok
                and portrait_profile
            )
            if healthy:
                overall = "healthy"
            elif service_state == "inactive":
                overall = "offline"
            elif service_state == "failed":
                overall = "failed"
            elif service_state == "active":
                overall = "degraded"
            else:
                overall = "unknown"
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc
        error_code = None if has_url and has_key else "youtube_not_configured"
        if service_state in {"failed", "unknown"}:
            error_code = "relayctl_failed"
        return RelaySnapshot(
            service_state=service_state,
            enabled=enabled,
            main_process=main_process,
            srt_listener=srt_listener,
            source=source,
            youtube_forward=cast(
                Literal["active", "inactive", "connecting", "failed", "unknown"],
                youtube_forward,
            ),
            overall=cast(
                Literal["ok", "healthy", "degraded", "failed", "offline", "unknown"],
                overall,
            ),
            youtube_url_configured=bool(has_url),
            youtube_key_configured=bool(has_key),
            healthy=healthy,
            portrait_profile=portrait_profile,
            error_code=error_code,
            input_bitrate_bps=input_bitrate_bps,
        )

    def _portrait_profile(self) -> bool:
        slate = Path("/var/lib/moblin-relay/slate.mp4")
        try:
            metadata = slate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return False
            cache_key = (metadata.st_mtime_ns, metadata.st_size)
            if self._portrait_cache is not None and self._portrait_cache[:2] == cache_key:
                return self._portrait_cache[2]
            result = _function(self._relayctl, "run_quiet")(
                [
                    "/usr/bin/ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,profile,level,width,height,pix_fmt,r_frame_rate",
                    "-of",
                    "json",
                    str(slate),
                ]
            )
            if getattr(result, "returncode", 1) != 0:
                portrait = False
            else:
                decoded = json.loads(str(getattr(result, "stdout", "")))
                streams = decoded.get("streams") if isinstance(decoded, dict) else None
                stream = streams[0] if isinstance(streams, list) and len(streams) == 1 else None
                portrait = bool(
                    isinstance(stream, dict)
                    and stream.get("codec_name") == "h264"
                    and stream.get("profile") == "Main"
                    and stream.get("level") == 40
                    and stream.get("width") == 1080
                    and stream.get("height") == 1920
                    and stream.get("pix_fmt") == "yuv420p"
                    and stream.get("r_frame_rate") == "30/1"
                )
            self._portrait_cache = (cache_key[0], cache_key[1], portrait)
            return portrait
        except (OSError, TypeError, ValueError):
            return False

    def _service_state(self, active: bool) -> Literal["active", "inactive", "failed", "unknown"]:
        if active:
            return "active"
        result = _function(self._relayctl, "run_quiet")(
            [
                "systemctl",
                "show",
                "moblin-relay.service",
                "--property=ActiveState",
                "--value",
                "--no-pager",
            ]
        )
        if getattr(result, "returncode", 1) != 0:
            return "unknown"
        value = str(getattr(result, "stdout", "")).strip()
        if value == "failed":
            return "failed"
        if value == "inactive":
            return "inactive"
        return "unknown"

    def _main_process(
        self,
        active: bool,
        service_state: Literal["active", "inactive", "failed", "unknown"],
    ) -> Literal["running", "stopped", "failed", "unknown"]:
        if not active:
            if service_state == "inactive":
                return "stopped"
            return "failed" if service_state == "failed" else "unknown"
        try:
            main_pid = _function(self._relayctl, "get_main_pid")()
        except (OSError, TypeError, ValueError):
            return "unknown"
        return "running" if isinstance(main_pid, int) and main_pid > 1 else "failed"

    def _srt_listener(
        self,
        active: bool,
        service_state: Literal["active", "inactive", "failed", "unknown"],
    ) -> Literal["listening", "closed", "failed", "unknown"]:
        if not active:
            if service_state == "inactive":
                return "closed"
            return "failed" if service_state == "failed" else "unknown"
        port = self._relayctl["SRT_PORT"]
        if not isinstance(port, int):
            return "unknown"
        result = _function(self._relayctl, "run_quiet")(
            ["/usr/bin/ss", "-H", "-lun", "sport", "=", f":{port}"]
        )
        if getattr(result, "returncode", 1) != 0:
            return "failed"
        return "listening" if f":{port}" in str(getattr(result, "stdout", "")) else "failed"

    def _media_state(
        self, active: bool
    ) -> tuple[
        Literal["SLATE", "LIVE", "NONE", "UNKNOWN"],
        bool,
        bool,
        bool,
        int | None,
    ]:
        if not active:
            self._bitrate_sampler.reset()
            return "NONE", False, False, False, None
        try:
            metrics = _function(self._relayctl, "read_metrics")()
            parse = _function(self._relayctl, "parse_metric_samples")
            ingress_path = str(self._relayctl["SRT_PATH"])
            path_ready = any(
                labels.get("name") == _FINAL_OUTPUT_PATH
                and labels.get("state") == "ready"
                and value == 1
                for labels, value in parse(metrics, "paths")
            )
            ingress_publishers = [
                labels
                for labels, value in parse(metrics, "srt_conns")
                if labels.get("path") == ingress_path
                and labels.get("state") == "publish"
                and value == 1
            ]
            normalizer_publishers = [
                labels
                for labels, value in parse(metrics, "rtmp_conns")
                if labels.get("path") == _FINAL_OUTPUT_PATH
                and labels.get("state") == "publish"
                and value == 1
            ]
            # SRT proves that Moblin reached the public ingress, while RTMP
            # proves that the local audio normalizer reached the canonical
            # output.  Neither half alone is a usable LIVE relay.
            live = bool(ingress_publishers) and bool(normalizer_publishers)
            counter_samples = [
                (labels, value)
                for labels, value in parse(metrics, "srt_conns_bytes_received")
                if labels.get("path") == ingress_path and labels.get("state") == "publish"
            ]
            forward_ok = any(
                labels.get("path") == _FINAL_OUTPUT_PATH
                and labels.get("protocol") == "rtmps"
                and labels.get("state") in {"forwarding", "ready"}
                and value == 1
                for labels, value in parse(metrics, "forward_dests")
            )
        except (OSError, TypeError, ValueError):
            self._bitrate_sampler.reset()
            return "UNKNOWN", False, False, False, None
        if live:
            input_bitrate_bps = self._input_bitrate(ingress_publishers, counter_samples)
            return "LIVE", True, path_ready, forward_ok, input_bitrate_bps
        self._bitrate_sampler.reset()
        return ("SLATE" if path_ready else "UNKNOWN"), True, path_ready, forward_ok, None

    def _input_bitrate(
        self,
        live_publishers: list[dict[str, str]],
        counter_samples: list[tuple[dict[str, str], float]],
    ) -> int | None:
        if len(live_publishers) != 1:
            self._bitrate_sampler.reset()
            return None
        connection_id = live_publishers[0].get("id")
        matching = [value for labels, value in counter_samples if labels.get("id") == connection_id]
        if len(matching) != 1:
            self._bitrate_sampler.reset()
            return None
        return self._bitrate_sampler.sample(
            connection_id=connection_id,
            bytes_received=matching[0],
        )

    def _start(self, *, relay_lock_held: bool = False) -> JsonObject:
        before = self.snapshot()
        if not (before.youtube_url_configured and before.youtube_key_configured):
            return self._result("failed", before.with_error("youtube_not_configured"))
        return_code = (
            self._start_with_held_lock()
            if relay_lock_held
            else _capture_return_code(self._relayctl["cmd_start"])
        )
        after = self.snapshot()
        if return_code != 0:
            return self._result("failed", after.with_error("relayctl_failed"))
        return self._result("ok", after)

    def _stop(self, *, relay_lock_held: bool = False) -> JsonObject:
        return_code = (
            self._stop_with_held_lock()
            if relay_lock_held
            else _capture_return_code(self._relayctl["cmd_stop"])
        )
        after = self.snapshot()
        if return_code != 0:
            return self._result("failed", after.with_error("relayctl_failed"))
        return self._result("ok", after)

    def _start_with_held_lock(self) -> int:
        try:
            data = _function(self._relayctl, "load_secrets")()
            has_url, has_key = _function(self._relayctl, "youtube_state")(data)
            if not (has_url and has_key) or not isinstance(data, dict):
                return 1
            youtube = data.get("youtube")
            if not isinstance(youtube, dict):
                return 1
            _function(self._relayctl, "validate_youtube")(youtube.get("url"), youtube.get("key"))
            result = _function(self._relayctl, "run_quiet")(
                ["systemctl", "enable", "--now", _MOBLIN_RELAY_SERVICE]
            )
            if getattr(result, "returncode", 1) != 0:
                return 1
            for _ in range(30):
                if _function(self._relayctl, "service_is_active")():
                    return 0
                time.sleep(0.25)
        except (KeyError, OSError, TypeError, ValueError, SystemExit):
            return 1
        return 1

    def _stop_with_held_lock(self) -> int:
        try:
            result = _function(self._relayctl, "run_quiet")(
                ["systemctl", "disable", "--now", _MOBLIN_RELAY_SERVICE]
            )
        except (KeyError, OSError, TypeError, ValueError, SystemExit):
            return 1
        return 0 if getattr(result, "returncode", 1) == 0 else 1

    def _configure_youtube(self, payload: dict[object, object]) -> JsonObject:
        if set(payload) != {"youtube_rtmps_url", "youtube_stream_key"}:
            raise RelayAgentError("invalid_configuration")
        raw_url = payload.get("youtube_rtmps_url")
        raw_key = payload.get("youtube_stream_key")
        if not isinstance(raw_url, str) or not isinstance(raw_key, str):
            raise RelayAgentError("invalid_configuration")
        lock_factory = _function(self._relayctl, "RelayLock")
        before_commit: RelaySnapshot
        try:
            with lock_factory():
                if not _function(self._relayctl, "service_allows_reconfiguration")():
                    return self._result("conflict", self.snapshot().with_error("relay_active"))
                try:
                    url, stream_key = _function(self._relayctl, "validate_youtube")(
                        raw_url, raw_key
                    )
                except ValueError as exc:
                    raise RelayAgentError("invalid_configuration") from exc
                validate_official_youtube_endpoint(url)
                data = _function(self._relayctl, "load_secrets")()
                if not isinstance(data, dict):
                    raise RelayAgentError("relayctl_failed")
                data["youtube"] = {"url": url, "key": stream_key}
                before_commit = self.snapshot()
                _function(self._relayctl, "atomic_save_secrets")(data)
        except RelayAgentError:
            raise
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc
        # The durable atomic save is the final blocking operation. Never turn a
        # committed configuration into a timeout/failure during a later probe.
        return self._result(
            "ok",
            replace(
                before_commit,
                youtube_url_configured=True,
                youtube_key_configured=True,
                error_code=(
                    None
                    if before_commit.error_code == "youtube_not_configured"
                    else before_commit.error_code
                ),
            ),
        )

    def _configure_youtube_key(self, payload: dict[object, object]) -> JsonObject:
        if set(payload) != {"youtube_stream_key"}:
            raise RelayAgentError("invalid_configuration")
        raw_key = payload.get("youtube_stream_key")
        if not isinstance(raw_key, str):
            raise RelayAgentError("invalid_configuration")
        lock_factory = _function(self._relayctl, "RelayLock")
        before_commit: RelaySnapshot
        try:
            with lock_factory():
                if not _function(self._relayctl, "service_allows_reconfiguration")():
                    return self._result("conflict", self.snapshot().with_error("relay_active"))
                data = _function(self._relayctl, "load_secrets")(optional=True)
                if not isinstance(data, dict):
                    raise RelayAgentError("relayctl_failed")
                youtube = data.get("youtube")
                if not isinstance(youtube, dict):
                    raise RelayAgentError("invalid_configuration")
                existing_url = youtube.get("url")
                if not isinstance(existing_url, str) or not existing_url.strip():
                    raise RelayAgentError("invalid_configuration")
                try:
                    validated_url, stream_key = _function(self._relayctl, "validate_youtube")(
                        existing_url, raw_key
                    )
                except ValueError as exc:
                    raise RelayAgentError("invalid_configuration") from exc
                validate_official_youtube_endpoint(validated_url)
                # This action is deliberately key-only. A URL requiring even
                # normalization must be repaired through the full configure action.
                if validated_url != existing_url:
                    raise RelayAgentError("invalid_configuration")
                youtube["key"] = stream_key
                before_commit = self.snapshot()
                _function(self._relayctl, "atomic_save_secrets")(data)
        except RelayAgentError:
            raise
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc
        return self._result(
            "ok",
            replace(
                before_commit,
                youtube_url_configured=True,
                youtube_key_configured=True,
                error_code=(
                    None
                    if before_commit.error_code == "youtube_not_configured"
                    else before_commit.error_code
                ),
            ),
        )

    def _clear_youtube(self) -> JsonObject:
        lock_factory = _function(self._relayctl, "RelayLock")
        before_commit: RelaySnapshot
        try:
            with lock_factory():
                if not _function(self._relayctl, "service_allows_reconfiguration")():
                    return self._result("conflict", self.snapshot().with_error("relay_active"))
                data = _function(self._relayctl, "load_secrets")()
                if not isinstance(data, dict):
                    raise RelayAgentError("relayctl_failed")
                data["youtube"] = {"url": "", "key": ""}
                before_commit = self.snapshot()
                _function(self._relayctl, "atomic_save_secrets")(data)
        except RelayAgentError:
            raise
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc
        return self._result(
            "ok",
            replace(
                before_commit,
                youtube_url_configured=False,
                youtube_key_configured=False,
                healthy=False,
                error_code=(
                    "youtube_not_configured"
                    if before_commit.error_code in {None, "youtube_not_configured"}
                    else before_commit.error_code
                ),
            ),
        )

    def _reveal_moblin_url(self) -> JsonObject:
        builder = self._relayctl.get("build_moblin_urls")
        if callable(builder):
            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    structured = builder()
            except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
                raise RelayAgentError("relayctl_failed") from exc
            secret_result = self._validate_moblin_urls(structured)
            return self._result("ok", self.snapshot(), secret_result)

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = _function(self._relayctl, "cmd_show_moblin_url")()
        except (KeyError, OSError, TypeError, ValueError, SystemExit) as exc:
            raise RelayAgentError("relayctl_failed") from exc
        if return_code != 0:
            raise RelayAgentError("relayctl_failed")
        secret_result = self._validate_moblin_output(stdout.getvalue())
        return self._result("ok", self.snapshot(), secret_result)

    def _validate_moblin_urls(self, value: object) -> str:
        if not isinstance(value, dict) or set(value) != {"public_url", "fallback_urls"}:
            raise RelayAgentError("relayctl_failed")
        public_url = value["public_url"]
        fallback_urls = value["fallback_urls"]
        if (
            not isinstance(public_url, str)
            or not isinstance(fallback_urls, list)
            or len(fallback_urls) > _MAX_FALLBACK_SRT_URLS
            or any(not isinstance(candidate, str) for candidate in fallback_urls)
        ):
            raise RelayAgentError("relayctl_failed")
        candidates = [public_url, *fallback_urls]
        if len(set(candidates)) != len(candidates):
            raise RelayAgentError("relayctl_failed")
        for candidate in candidates:
            self._validate_srt_url(candidate)
        encoded = json.dumps(
            {"public_url": public_url, "fallback_urls": fallback_urls},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if not 1 <= len(encoded) <= MAX_SECRET_RESULT_CHARS:
            raise RelayAgentError("relayctl_failed")
        return encoded

    def _validate_srt_url(self, candidate: str) -> None:
        if (
            not 1 <= len(candidate) <= 2048
            or not candidate.isascii()
            or any(ord(character) < 0x20 or character.isspace() for character in candidate)
        ):
            raise RelayAgentError("relayctl_failed")
        port = self._relayctl["SRT_PORT"]
        path = self._relayctl["SRT_PATH"]
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or not isinstance(path, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", path)
        ):
            raise RelayAgentError("relayctl_failed")
        try:
            parsed = urlsplit(candidate)
            parsed_port = parsed.port
        except ValueError as exc:
            raise RelayAgentError("relayctl_failed") from exc
        hostname = parsed.hostname
        if (
            parsed.scheme != "srt"
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed_port != port
            or parsed.path not in {"", "/"}
            or parsed.fragment
        ):
            raise RelayAgentError("relayctl_failed")
        if ":" in hostname:
            try:
                canonical_host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
            except ipaddress.AddressValueError as exc:
                raise RelayAgentError("relayctl_failed") from exc
        else:
            try:
                canonical_host = str(ipaddress.IPv4Address(hostname))
            except ipaddress.AddressValueError:
                if (
                    hostname.endswith(".")
                    or all(character.isdigit() or character == "." for character in hostname)
                    or any(
                        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in hostname.split(".")
                    )
                ):
                    raise RelayAgentError("relayctl_failed") from None
                canonical_host = hostname.lower()
        if parsed.netloc != f"{canonical_host}:{port}":
            raise RelayAgentError("relayctl_failed")
        query_pattern = re.compile(
            rf"streamid=publish:{re.escape(path)}:[A-Za-z0-9_-]{{1,64}}:"
            r"[A-Za-z0-9_-]{16,128}&passphrase=[A-Za-z0-9_-]{10,79}"
            r"&pbkeylen=32&latency=2000&payloadsize=1316\Z"
        )
        if query_pattern.fullmatch(parsed.query) is None:
            raise RelayAgentError("relayctl_failed")

    def _validate_moblin_output(self, output: str) -> str:
        normalized = output.strip()
        if not 1 <= len(normalized) <= MAX_SECRET_RESULT_CHARS:
            raise RelayAgentError("relayctl_failed")
        lines = normalized.splitlines()
        if (
            len(lines) != 4
            or lines[0] != "Public SRT URL:"
            or lines[2] != "VPN SRT URL (fallback):"
        ):
            raise RelayAgentError("relayctl_failed")
        expected_hosts = (str(self._relayctl["PUBLIC_HOST"]), str(self._relayctl["VPN_HOST"]))
        for candidate, expected_host in zip((lines[1], lines[3]), expected_hosts, strict=True):
            if any(ord(character) < 0x20 or character.isspace() for character in candidate):
                raise RelayAgentError("relayctl_failed")
            try:
                parsed = urlsplit(candidate)
                port = parsed.port
            except ValueError as exc:
                raise RelayAgentError("relayctl_failed") from exc
            if (
                parsed.scheme != "srt"
                or parsed.hostname != expected_host
                or not isinstance(self._relayctl["SRT_PORT"], int)
                or port != self._relayctl["SRT_PORT"]
                or parsed.path not in {"", "/"}
                or not parsed.query.startswith(f"streamid=publish:{self._relayctl['SRT_PATH']}:")
                or "&passphrase=" not in parsed.query
                or "&pbkeylen=32" not in parsed.query
            ):
                raise RelayAgentError("relayctl_failed")
        return normalized


def _encode_json(response: JsonObject) -> bytes:
    payload = json.dumps(response, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(payload) > MAX_BROKER_MESSAGE_BYTES:
        payload = json.dumps(
            RelayBroker._result("failed", RelaySnapshot.unavailable("internal_error")),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    return payload


def _failed_worker_response() -> bytes:
    return _encode_json(RelayBroker._result("failed", RelaySnapshot.unavailable("relayctl_failed")))


def _unwrap_request_deadline(request: JsonObject) -> tuple[JsonObject, float]:
    """Validate the same-host monotonic client deadline and strip its envelope."""

    if set(request) != {"action", "payload", "deadline_monotonic_ns"}:
        raise RelayAgentError("invalid_request")
    raw_deadline = request.get("deadline_monotonic_ns")
    if not isinstance(raw_deadline, int) or isinstance(raw_deadline, bool):
        raise RelayAgentError("invalid_request")
    now_ns = time.monotonic_ns()
    maximum_ns = int(BROKER_CLIENT_TIMEOUT_SECONDS * 1_000_000_000)
    if raw_deadline <= now_ns or raw_deadline - now_ns > maximum_ns:
        raise RelayAgentError("invalid_request")
    return (
        {"action": request.get("action"), "payload": request.get("payload")},
        raw_deadline / 1_000_000_000,
    )


def _waitpid_nointr(pid: int, options: int = 0) -> tuple[int, int]:
    while True:
        try:
            return os.waitpid(pid, options)
        except InterruptedError:
            continue


def _running_on_linux() -> bool:
    return sys.platform.startswith("linux")


def _enable_child_subreaper() -> None:
    """Make the long-lived Linux broker reap orphaned relayctl descendants."""

    global _subreaper_enabled
    if not _running_on_linux():
        raise RelayAgentError("relayctl_failed")
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (AttributeError, OSError) as exc:
        raise RelayAgentError("relayctl_failed") from exc
    if result != 0:
        raise RelayAgentError("relayctl_failed")
    _subreaper_enabled = True


def _reap_adopted_children() -> None:
    if not _subreaper_enabled:
        return
    deadline = time.monotonic() + 0.25
    nohang = int(getattr(os, "WNOHANG", 1))
    while True:
        try:
            adopted_pid, _ = _waitpid_nointr(-1, nohang)
        except ChildProcessError:
            return
        if adopted_pid > 0:
            continue
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def _terminate_worker(pid: int) -> None:
    """Kill the isolated worker session and all relayctl descendants, then reap it."""

    # A negative PID addresses the worker's process group on POSIX.  Before the
    # worker's ready byte, that group may not exist yet, so kill the PID too.
    for target in (-pid, pid):
        try:
            os.kill(target, 9)
        except OSError:
            continue
    try:
        _waitpid_nointr(pid)
    except ChildProcessError:
        _reap_adopted_children()
        return
    _reap_adopted_children()


def _encode_worker_header(state: tuple[bool, bool] | None) -> bytes:
    if state is None:
        return _BROKER_READY_BYTE + b"\x00"
    active, enabled = state
    state_byte = 0x80 | int(active) | (int(enabled) << 1)
    return _BROKER_READY_BYTE + bytes((state_byte,))


def _decode_worker_frame(payload: bytes) -> tuple[tuple[bool, bool] | None, bytes] | None:
    if len(payload) < 2 or payload[:1] != _BROKER_READY_BYTE:
        return None
    state_byte = payload[1]
    if state_byte == 0:
        state = None
    elif state_byte & 0xFC != 0x80:
        return None
    else:
        state = (bool(state_byte & 0x01), bool(state_byte & 0x02))
    return state, payload[2:]


def _acquire_relay_transaction_lock(deadline: float) -> int | None:
    """Acquire relayctl's exact flock without ever blocking past the safe start window."""

    fd = -1
    try:
        fcntl_module: Any = __import__("fcntl")

        _RELAY_LOCK_DIRECTORY.mkdir(mode=0o700, parents=False, exist_ok=True)
        directory = _RELAY_LOCK_DIRECTORY.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != effective_uid()
            or directory.st_mode & 0o077
        ):
            return None
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(_RELAY_LOCK_PATH, flags, 0o600)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != effective_uid()
            or metadata.st_mode & 0o077
        ):
            os.close(fd)
            fd = -1
            return None
        while time.monotonic() < deadline:
            try:
                fcntl_module.flock(fd, fcntl_module.LOCK_EX | fcntl_module.LOCK_NB)
                return fd
            except BlockingIOError:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        os.close(fd)
        fd = -1
    except (ImportError, OSError):
        if fd >= 0:
            os.close(fd)
        return None
    return None


def _release_relay_transaction_lock(fd: int) -> None:
    try:
        fcntl_module: Any = __import__("fcntl")

        fcntl_module.flock(fd, fcntl_module.LOCK_UN)
    except (ImportError, OSError):
        # Closing every inherited descriptor is the authoritative unlock path.
        os.close(fd)
        return
    os.close(fd)


def _worker_process(connection: socket.socket, broker: RelayBroker, request: JsonObject) -> None:
    exit_code = 1
    try:
        setsid = getattr(os, "setsid", None)
        if not callable(setsid):
            raise RelayAgentError("relayctl_failed")
        setsid()
        state = broker.reconciliation_state(request)
        connection.sendall(_encode_worker_header(state))
        connection.sendall(_encode_json(broker.handle(request, relay_lock_held=state is not None)))
        exit_code = 0
    except BaseException:
        # The root broker must never print relayctl exceptions or request data.
        exit_code = 1
    finally:
        connection.close()
    os._exit(exit_code)


def _run_fixed_systemctl(
    arguments: list[str], *, deadline: float, capture: bool = False
) -> subprocess.CompletedProcess[bytes] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return subprocess.run(  # noqa: S603 -- absolute executable and fixed arguments only
            [_SYSTEMCTL_PATH, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=min(1.0, remaining),
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _read_systemd_state(deadline: float) -> tuple[str, str, str] | None:
    result = _run_fixed_systemctl(
        [
            "show",
            _MOBLIN_RELAY_SERVICE,
            "--property=ActiveState",
            "--property=UnitFileState",
            "--property=Job",
            "--no-pager",
        ],
        deadline=deadline,
        capture=True,
    )
    if result is None or result.returncode != 0 or len(result.stdout) > 4096:
        return None
    values: dict[str, str] = {}
    try:
        for raw_line in result.stdout.decode("ascii", errors="strict").splitlines():
            key, separator, value = raw_line.partition("=")
            if not separator or key in values:
                return None
            values[key] = value
    except UnicodeDecodeError:
        return None
    if set(values) != {"ActiveState", "UnitFileState", "Job"}:
        return None
    return values["ActiveState"], values["UnitFileState"], values["Job"]


def _reconcile_service_state(active: bool, enabled: bool, deadline: float) -> bool:
    """Replace any queued systemd job and prove the exact pre-action state."""

    enable_verb = "enable" if enabled else "disable"
    active_verb = "start" if active else "stop"
    desired_active = "active" if active else "inactive"
    desired_enabled = "enabled" if enabled else "disabled"
    while time.monotonic() < deadline:
        enable_result = _run_fixed_systemctl(
            [enable_verb, _MOBLIN_RELAY_SERVICE], deadline=deadline
        )
        active_result = _run_fixed_systemctl(
            [active_verb, "--no-block", _MOBLIN_RELAY_SERVICE], deadline=deadline
        )
        if (
            enable_result is not None
            and enable_result.returncode == 0
            and active_result is not None
            and active_result.returncode == 0
        ):
            observed = _read_systemd_state(deadline)
            if observed == (desired_active, desired_enabled, ""):
                return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    return False


def _reconciliation_process(
    connection: socket.socket,
    state: tuple[bool, bool],
    deadline: float,
    reconcile: Callable[[bool, bool, float], bool],
) -> None:
    exit_code = 1
    try:
        setsid = getattr(os, "setsid", None)
        if not callable(setsid):
            raise RelayAgentError("relayctl_failed")
        setsid()
        connection.sendall(b"\x01" if reconcile(*state, deadline) else b"\x00")
        exit_code = 0
    except BaseException:
        # Never print reconciliation details or inherited request state.
        exit_code = 1
    finally:
        connection.close()
    os._exit(exit_code)


def _run_reconciliation(
    state: tuple[bool, bool],
    *,
    client_deadline: float,
    timeout_seconds: float,
    reconcile: Callable[[bool, bool, float], bool],
) -> bool:
    deadline = min(
        time.monotonic() + timeout_seconds,
        client_deadline - BROKER_RESPONSE_RESERVE_SECONDS,
    )
    if deadline <= time.monotonic():
        return False
    fork = getattr(os, "fork", None)
    if not callable(fork):
        return False
    try:
        parent_connection, worker_connection = socket.socketpair()
        pid = fork()
    except OSError:
        return False
    if pid == 0:
        parent_connection.close()
        _reconciliation_process(worker_connection, state, deadline, reconcile)
        os._exit(1)

    worker_connection.close()
    result = bytearray()
    timed_out = False
    try:
        parent_connection.setblocking(False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                readable, _, _ = select.select([parent_connection], [], [], remaining)
            except (OSError, ValueError):
                timed_out = True
                break
            if not readable:
                timed_out = True
                break
            try:
                chunk = parent_connection.recv(2)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > 1:
                timed_out = True
                break
    finally:
        parent_connection.close()
    if timed_out:
        _terminate_worker(pid)
        return False
    try:
        waited_pid, status = _waitpid_nointr(pid)
    except ChildProcessError:
        return False
    _reap_adopted_children()
    return waited_pid == pid and status == 0 and bytes(result) == b"\x01"


def _response_succeeded(response: bytes) -> bool:
    try:
        decoded = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(decoded, dict) and decoded.get("status") == "ok"


def _execute_bounded_request(
    broker: RelayBroker,
    request: JsonObject,
    *,
    client_deadline: float,
    action_timeout_seconds: float = BROKER_SERVER_ACTION_TIMEOUT_SECONDS,
    reconcile_timeout_seconds: float = BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS,
    reconcile: Callable[[bool, bool, float], bool] = _reconcile_service_state,
) -> bytes:
    """Reject stale mutations, then serialize START/STOP through relayctl's lock."""

    now = time.monotonic()
    action = request.get("action")
    mutating = isinstance(action, str) and action in {
        "start",
        "stop",
        "configure_youtube",
        "configure_youtube_key",
        "clear_youtube",
    }
    needs_reconciliation = (
        isinstance(action, str) and action in {"start", "stop"} and request.get("payload") == {}
    )
    required = action_timeout_seconds + BROKER_RESPONSE_RESERVE_SECONDS
    if needs_reconciliation:
        required += reconcile_timeout_seconds
    if mutating and client_deadline - now < required:
        return _failed_worker_response()

    lock_fd: int | None = None
    if needs_reconciliation:
        lock_fd = _acquire_relay_transaction_lock(client_deadline - required)
        if lock_fd is None:
            return _failed_worker_response()
    try:
        return _execute_bounded_request_with_lock(
            broker,
            request,
            client_deadline=client_deadline,
            action_timeout_seconds=action_timeout_seconds,
            reconcile_timeout_seconds=reconcile_timeout_seconds,
            reconcile=reconcile,
        )
    finally:
        if lock_fd is not None:
            _release_relay_transaction_lock(lock_fd)


def _execute_bounded_request_with_lock(
    broker: RelayBroker,
    request: JsonObject,
    *,
    client_deadline: float,
    action_timeout_seconds: float = BROKER_SERVER_ACTION_TIMEOUT_SECONDS,
    reconcile_timeout_seconds: float = BROKER_SERVER_RECONCILE_TIMEOUT_SECONDS,
    reconcile: Callable[[bool, bool, float], bool] = _reconcile_service_state,
) -> bytes:
    """Run one privileged request while the parent retains any relay transaction lock."""

    fork = getattr(os, "fork", None)
    if not callable(fork):
        return _failed_worker_response()
    now = time.monotonic()
    action = request.get("action")
    mutating = isinstance(action, str) and action in {
        "start",
        "stop",
        "configure_youtube",
        "configure_youtube_key",
        "clear_youtube",
    }
    needs_reconciliation = (
        isinstance(action, str) and action in {"start", "stop"} and request.get("payload") == {}
    )
    required = action_timeout_seconds + BROKER_RESPONSE_RESERVE_SECONDS
    if needs_reconciliation:
        required += reconcile_timeout_seconds
    remaining = client_deadline - now
    # Never let an agent-supplied short deadline shrink a mutation window. A
    # stale/backlogged mutation is rejected before a root worker exists.
    if mutating and remaining < required:
        return _failed_worker_response()
    deadline = min(
        now + action_timeout_seconds,
        client_deadline - BROKER_RESPONSE_RESERVE_SECONDS,
    )
    if deadline <= now:
        return _failed_worker_response()
    try:
        parent_connection, worker_connection = socket.socketpair()
        pid = fork()
    except OSError:
        return _failed_worker_response()
    if pid == 0:
        parent_connection.close()
        _worker_process(worker_connection, broker, request)
        os._exit(1)

    worker_connection.close()
    wire_response = bytearray()
    timed_out = False
    try:
        parent_connection.setblocking(False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                readable, _, _ = select.select([parent_connection], [], [], remaining)
            except (OSError, ValueError):
                timed_out = True
                break
            if not readable:
                timed_out = True
                break
            try:
                chunk = parent_connection.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            if len(chunk) > MAX_BROKER_MESSAGE_BYTES + 2 - len(wire_response):
                timed_out = True
                break
            wire_response.extend(chunk)
    except OSError:
        timed_out = True

    if timed_out:
        _terminate_worker(pid)
        # The fixed header is written atomically before the handler is armed.
        # Drain it after process-group death to decide whether rollback is owed.
        while len(wire_response) < 2:
            try:
                chunk = parent_connection.recv(2 - len(wire_response))
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            wire_response.extend(chunk)
        frame = _decode_worker_frame(bytes(wire_response))
        parent_connection.close()
        state = frame[0] if frame is not None else None
        if state is not None and not _run_reconciliation(
            state,
            client_deadline=client_deadline,
            timeout_seconds=reconcile_timeout_seconds,
            reconcile=reconcile,
        ):
            # Do not claim a completed failure unless the original state was proven.
            return b""
        return _failed_worker_response()

    parent_connection.close()
    try:
        waited_pid, status = _waitpid_nointr(pid)
    except ChildProcessError:
        return _failed_worker_response()
    _reap_adopted_children()
    frame = _decode_worker_frame(bytes(wire_response))
    if waited_pid != pid or status != 0 or frame is None or not frame[1]:
        state = frame[0] if frame is not None else None
        if state is not None and not _run_reconciliation(
            state,
            client_deadline=client_deadline,
            timeout_seconds=reconcile_timeout_seconds,
            reconcile=reconcile,
        ):
            return b""
        return _failed_worker_response()

    state, response = frame
    if (
        state is not None
        and not _response_succeeded(response)
        and not _run_reconciliation(
            state,
            client_deadline=client_deadline,
            timeout_seconds=reconcile_timeout_seconds,
            reconcile=reconcile,
        )
    ):
        return b""
    return response


def serve(listener: socket.socket, *, expected_uid: int, broker: RelayBroker) -> None:
    """Serve authenticated local requests; unauthorized peers receive no response."""

    while True:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(BROKER_SERVER_IO_TIMEOUT_SECONDS)
            try:
                if not peer_is_expected_agent(connection, expected_uid):
                    continue
                request, client_deadline = _unwrap_request_deadline(_read_bounded_json(connection))
                response = _execute_bounded_request(
                    broker, request, client_deadline=client_deadline
                )
            except RelayAgentError:
                response = _encode_json(
                    RelayBroker._result(
                        "failed", RelaySnapshot.unavailable("invalid_configuration")
                    )
                )
            try:
                connection.sendall(response)
            except OSError:
                continue


def _systemd_listener() -> socket.socket:
    try:
        listen_pid = int(os.environ.get("LISTEN_PID", "0"))
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError as exc:
        raise RelayAgentError("socket_activation_required") from exc
    if listen_pid != os.getpid() or listen_fds != 1:
        raise RelayAgentError("socket_activation_required")
    try:
        unix_family = socket.__dict__["AF_UNIX"]
        listener = socket.fromfd(3, unix_family, socket.SOCK_STREAM)
    except OSError as exc:
        raise RelayAgentError("socket_activation_required") from exc
    if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        listener.close()
        raise RelayAgentError("socket_activation_required")
    return listener


def main() -> int:
    if sys.argv[1:] or effective_uid() != 0:
        return 2
    try:
        pwd_module = __import__("pwd")
        expected_uid = int(pwd_module.getpwnam(EXPECTED_AGENT_USER).pw_uid)
        _enable_child_subreaper()
        listener = _systemd_listener()
        broker = RelayBroker()
        with listener:
            serve(listener, expected_uid=expected_uid, broker=broker)
    except (KeyError, OSError, RelayAgentError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
