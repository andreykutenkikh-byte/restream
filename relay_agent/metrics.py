"""Small, dependency-free host metrics collector for safe heartbeat data."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from relay_agent.errors import RelayAgentError
from relay_agent.models import HostMetrics


class HostMetricsCollector:
    def __init__(self, disk_path: Path = Path("/")) -> None:
        self._disk_path = disk_path
        self._lock = threading.Lock()
        self._previous_cpu: tuple[int, int] | None = None

    @staticmethod
    def _first_number(path: Path) -> float:
        try:
            return float(path.read_text(encoding="ascii").split(maxsplit=1)[0])
        except (OSError, ValueError, IndexError) as exc:
            raise RelayAgentError("metrics_unavailable") from exc

    @staticmethod
    def _memory() -> tuple[int, int]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, separator, raw = line.partition(":")
                if separator and key in {"MemTotal", "MemAvailable"}:
                    fields = raw.split()
                    if fields:
                        values[key] = int(fields[0]) * 1024
        except (OSError, ValueError) as exc:
            raise RelayAgentError("metrics_unavailable") from exc
        if "MemTotal" not in values or "MemAvailable" not in values:
            raise RelayAgentError("metrics_unavailable")
        return values["MemTotal"], values["MemAvailable"]

    @staticmethod
    def _cpu_times() -> tuple[int, int]:
        try:
            line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
            fields = [int(value) for value in line.split()[1:]]
        except (OSError, ValueError, IndexError) as exc:
            raise RelayAgentError("metrics_unavailable") from exc
        if len(fields) < 4:
            raise RelayAgentError("metrics_unavailable")
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    def _cpu_percent(self) -> float:
        current = self._cpu_times()
        with self._lock:
            previous = self._previous_cpu
            self._previous_cpu = current
        if previous is None:
            return 0.0
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        return min(100.0, max(0.0, 100.0 * (total_delta - max(0, idle_delta)) / total_delta))

    def collect(self) -> HostMetrics:
        uptime = self._first_number(Path("/proc/uptime"))
        load = self._first_number(Path("/proc/loadavg"))
        total_memory, available_memory = self._memory()
        try:
            disk = shutil.disk_usage(self._disk_path)
        except OSError as exc:
            raise RelayAgentError("metrics_unavailable") from exc
        return HostMetrics(
            uptime_seconds=uptime,
            load_1m=load,
            cpu_percent=self._cpu_percent(),
            memory_total_bytes=total_memory,
            memory_available_bytes=available_memory,
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
        )
