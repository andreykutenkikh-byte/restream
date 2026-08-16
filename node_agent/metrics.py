"""Bounded system and media-tool metrics collection."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from node_agent.errors import ProtocolError
from node_agent.models import NodeSnapshot

_MAX_TOOL_OUTPUT_BYTES = 4096


class MetricsProbe(Protocol):
    """Test-friendly platform probe boundary."""

    def hostname(self) -> str: ...

    def os_identity(self) -> tuple[str, str]: ...

    def architecture(self) -> str: ...

    def cpu_count(self) -> int: ...

    def uptime_seconds(self) -> float: ...

    def load_1m(self) -> float: ...

    def cpu_percent(self) -> float: ...

    def memory_bytes(self) -> tuple[int, int]: ...

    def disk_bytes(self, path: Path) -> tuple[int, int]: ...

    def tool_version(self, binary: str) -> str | None: ...


class LinuxMetricsProbe:
    """Reads Linux procfs and executes only fixed media-tool version probes."""

    def __init__(self) -> None:
        self._cpu_lock = threading.Lock()
        self._previous_cpu: tuple[int, int] | None = None
        self._tool_lock = threading.Lock()
        self._tool_versions: dict[str, str | None] = {}

    def hostname(self) -> str:
        return platform.node()

    def os_identity(self) -> tuple[str, str]:
        return platform.system(), platform.release()

    def architecture(self) -> str:
        return platform.machine()

    def cpu_count(self) -> int:
        return os.cpu_count() or 1

    def uptime_seconds(self) -> float:
        try:
            first_value = Path("/proc/uptime").read_text(encoding="ascii").split(maxsplit=1)[0]
            return float(first_value)
        except (OSError, ValueError, IndexError):
            return time.monotonic()

    def load_1m(self) -> float:
        try:
            first_value = Path("/proc/loadavg").read_text(encoding="ascii").split(maxsplit=1)[0]
            return float(first_value)
        except (OSError, ValueError, IndexError):
            return 0.0

    def cpu_percent(self) -> float:
        current = self._read_cpu_times()
        with self._cpu_lock:
            previous = self._previous_cpu
            self._previous_cpu = current
        if previous is None:
            return 0.0
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        busy_percent = 100.0 * (total_delta - max(0, idle_delta)) / total_delta
        return min(100.0, max(0.0, busy_percent))

    def _read_cpu_times(self) -> tuple[int, int]:
        try:
            first_line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
            fields = [int(value) for value in first_line.split()[1:]]
        except (OSError, ValueError, IndexError) as exc:
            raise ProtocolError("metrics_unavailable", "CPU metrics are unavailable") from exc
        if len(fields) < 4:
            raise ProtocolError("metrics_unavailable", "CPU metrics are unavailable")
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    def memory_bytes(self) -> tuple[int, int]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, separator, raw_value = line.partition(":")
                if separator and key in {"MemTotal", "MemAvailable"}:
                    fields = raw_value.split()
                    if not fields:
                        continue
                    values[key] = int(fields[0]) * 1024
        except (OSError, ValueError) as exc:
            raise ProtocolError("metrics_unavailable", "memory metrics are unavailable") from exc
        if "MemTotal" not in values or "MemAvailable" not in values:
            raise ProtocolError("metrics_unavailable", "memory metrics are unavailable")
        return values["MemTotal"], values["MemAvailable"]

    def disk_bytes(self, path: Path) -> tuple[int, int]:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            raise ProtocolError("metrics_unavailable", "disk metrics are unavailable") from exc
        return usage.total, usage.free

    def tool_version(self, binary: str) -> str | None:
        if binary not in {"ffmpeg", "ffprobe"}:
            raise ValueError("only fixed media tools may be probed")
        with self._tool_lock:
            if binary in self._tool_versions:
                return self._tool_versions[binary]
            version = self._probe_tool_version(binary)
            self._tool_versions[binary] = version
            return version

    @staticmethod
    def _probe_tool_version(binary: str) -> str | None:
        executable = shutil.which(binary, path=os.defpath)
        if executable is None:
            return None
        try:
            result = subprocess.run(  # noqa: S603 -- absolute executable from a fixed allowlist
                [executable, "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                env={"LANG": "C", "PATH": os.defpath},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or len(result.stdout) > _MAX_TOOL_OUTPUT_BYTES:
            return None
        first_line = result.stdout.splitlines()[0] if result.stdout else b""
        version = first_line.decode("utf-8", errors="replace").strip()
        if (
            not version
            or len(version) > 128
            or not all(character.isprintable() for character in version)
        ):
            return None
        return version


class MetricsCollector:
    """Creates a fully validated snapshot for enrollment and heartbeat."""

    def __init__(
        self,
        data_dir: Path,
        probe: MetricsProbe | None = None,
        *,
        host_hostname: str | None = None,
        host_os_name: str | None = None,
        host_os_version: str | None = None,
        host_architecture: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._probe = probe or LinuxMetricsProbe()
        self._host_hostname = host_hostname
        self._host_os_name = host_os_name
        self._host_os_version = host_os_version
        self._host_architecture = host_architecture

    def collect(self) -> NodeSnapshot:
        os_name, os_version = self._probe.os_identity()
        memory_total, memory_available = self._probe.memory_bytes()
        disk_total, disk_free = self._probe.disk_bytes(self._data_dir)
        return NodeSnapshot(
            hostname=self._host_hostname or self._probe.hostname(),
            os_name=self._host_os_name or os_name,
            os_version=self._host_os_version or os_version,
            architecture=self._host_architecture or self._probe.architecture(),
            cpu_count=self._probe.cpu_count(),
            uptime_seconds=self._probe.uptime_seconds(),
            load_1m=self._probe.load_1m(),
            cpu_percent=self._probe.cpu_percent(),
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            ffmpeg_version=self._probe.tool_version("ffmpeg"),
            ffprobe_version=self._probe.tool_version("ffprobe"),
        )
