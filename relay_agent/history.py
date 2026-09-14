"""Bounded local telemetry; connection identities and metric labels stay in memory.

Metric names follow MediaMTX v1.20.1 internal/metrics/metrics.go. RTMP inbound
bytes measure the normalizer's publisher at relay-output. Forward outbound
bytes measure the configured RTMPS destination (including protocol overhead),
not delivery or playback at YouTube. Its counter spans destination retries;
observed error/idle states break the baseline, as do identity changes.
"""

from __future__ import annotations

import math
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TypeAlias, cast
from uuid import UUID

HISTORY_PATH = Path("/var/lib/adojapan-relay-history/history.sqlite3")
SAMPLE_INTERVAL_SECONDS = 5.0
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_ROWS = int(RETENTION_SECONDS / SAMPLE_INTERVAL_SECONDS)
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_EXPORT_ROWS = 10_000
MAX_METRICS_BYTES = 2_000_000
MAX_SAMPLE_GAP_SECONDS = 15.0
_MAX_NUMBER = 2**53 - 1
_MAX_BITRATE = 1_000_000_000
_INPUT_PATH = "iphone-live"
_OUTPUT_PATH = "relay-output"
_STATUSES = frozenset(
    {
        "ok",
        "baseline",
        "absent",
        "ambiguous",
        "metric_missing",
        "counter_reset",
        "reconnected",
        "sample_gap",
        "unavailable",
    }
)
_ERRORS = frozenset(
    {"none", "service_inactive", "service_unknown", "metrics_unavailable", "metrics_invalid"}
)
_COUNTERS = {
    "input_unique_bytes": "srt_conns_bytes_received_unique",
    "input_gross_bytes": "srt_conns_bytes_received",
    "output_rtmp_bytes": "rtmp_conns_inbound_bytes",
    "youtube_outbound_bytes": "forward_dests_outbound_bytes",
    "srt_packets_received": "srt_conns_packets_received",
    "srt_packets_received_unique": "srt_conns_packets_received_unique",
    "srt_packets_received_loss": "srt_conns_packets_received_loss",
    "srt_packets_received_drop": "srt_conns_packets_received_drop",
    "srt_packets_received_retrans": "srt_conns_packets_received_retrans",
}
_RATES = {
    "input_unique_bytes": "input_unique_bitrate_bps",
    "input_gross_bytes": "input_gross_bitrate_bps",
    "output_rtmp_bytes": "output_rtmp_bitrate_bps",
    "youtube_outbound_bytes": "youtube_outbound_bitrate_bps",
}
_PACKETS = tuple(key for key in _COUNTERS if key.startswith("srt_packets_"))
_NUMERIC_FIELDS = (
    "timestamp",
    "sample_interval_seconds",
    *_COUNTERS,
    *_RATES.values(),
    "srt_rtt_ms",
    *(key + "_delta" for key in _PACKETS),
)
_TEXT_FIELDS = ("source", "error_code", "input_status", "output_status", "youtube_status")
ROW_FIELDS = (*_NUMERIC_FIELDS, *_TEXT_FIELDS)
_METRIC_NAMES = frozenset(
    {"paths", "srt_conns", "rtmp_conns", "forward_dests", "srt_conns_ms_rtt", *_COUNTERS.values()}
)
_METRIC = re.compile(r"^([a-z_]+)(?:\{(.*)\})?\s+([^\s]+)$")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z_0-9]*)="((?:[^"\\]|\\[\\"n])*)"(?:,|$)')
_LABEL_KEYS = frozenset({"id", "path", "name", "state", "protocol"})
Scalar: TypeAlias = int | float | str | None  # noqa: UP040 -- deployed Python 3.10
HistoryRow: TypeAlias = dict[str, Scalar]  # noqa: UP040 -- deployed Python 3.10
Metrics: TypeAlias = dict[  # noqa: UP040 -- deployed Python 3.10
    str, list[tuple[dict[str, str], int | float | None]]
]


class HistoryError(Exception):
    """Fixed-code errors only: never include a path, SQL, labels or metric text."""


def _number(value: object, *, integer: bool = False) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not 0 <= value <= _MAX_NUMBER or not math.isfinite(value):
        return None
    if integer:
        return int(value) if float(value).is_integer() else None
    return value


def parse_metrics(body: str) -> Metrics:
    """Parse a bounded allowlist, discarding address/URL/unknown labels immediately."""
    if len(body) > MAX_METRICS_BYTES:
        raise HistoryError("metrics_invalid")
    result: Metrics = {}
    for index, line in enumerate(body.splitlines()):
        if index >= 20_000 or len(line) > 16_384:
            raise HistoryError("metrics_invalid")
        name = line.partition("{")[0].partition(" ")[0]
        if name not in _METRIC_NAMES:
            continue
        match = _METRIC.fullmatch(line)
        if match is None:
            raise HistoryError("metrics_invalid")
        raw_labels = match[2] or ""
        labels: dict[str, str] = {}
        seen: set[str] = set()
        position = 0
        while position < len(raw_labels):
            label = _LABEL.match(raw_labels, position)
            if label is None or label[1] in seen or len(seen) >= 32:
                raise HistoryError("metrics_invalid")
            seen.add(label[1])
            if label[1] in _LABEL_KEYS:
                labels[label[1]] = re.sub(
                    r'\\([\\"n])', lambda part: "\n" if part[1] == "n" else part[1], label[2]
                )
            position = label.end()
        try:
            numeric = _number(float(match[3]))
        except ValueError:
            numeric = None
        samples = result.setdefault(name, [])
        if len(samples) >= 64:
            raise HistoryError("metrics_invalid")
        samples.append((labels, numeric))
    if "paths" not in result:
        raise HistoryError("metrics_invalid")
    return result


def _identity(labels: Mapping[str, str]) -> str | None:
    value = labels.get("id", "")
    try:
        return value.lower() if str(UUID(value)) == value.lower() else None
    except ValueError:
        return None


def _publisher(
    metrics: Metrics, metric: str, path: str, *, forward: bool = False
) -> tuple[dict[str, str] | None, str]:
    if metric not in metrics and not forward:
        return None, "metric_missing"
    candidates = [
        (labels, value)
        for labels, value in metrics.get(metric, [])
        if labels.get("path") == path
        and (labels.get("protocol") == "rtmps" if forward else labels.get("state") == "publish")
    ]
    if not candidates:
        return None, "absent"
    if len(candidates) != 1:
        return None, "ambiguous"
    labels, value = candidates[0]
    if forward and labels.get("state") not in {"forwarding", "ready"}:
        return None, "absent"
    if value != 1 or _identity(labels) is None:
        return None, "metric_missing"
    return labels, "ok"


def _metric_value(metrics: Metrics, name: str, publisher: Mapping[str, str]) -> int | float | None:
    matches = [
        value
        for labels, value in metrics.get(name, [])
        if all(labels.get(key) == publisher.get(key) for key in ("id", "path", "state"))
        and labels.get("protocol") == publisher.get("protocol")
    ]
    if len(matches) != 1:
        return None
    return _number(matches[0], integer=name != "srt_conns_ms_rtt")


class HistorySampler:
    """Only three in-memory connection baselines, never a connection label on disk."""

    def __init__(self) -> None:
        self._previous: dict[str, tuple[str, float, dict[str, int | float | None]]] = {}
        self._last_monotonic: float | None = None

    def sample(
        self, body: str | None, *, timestamp: int, monotonic: float, active: bool | None
    ) -> HistoryRow:
        row: HistoryRow = dict.fromkeys(ROW_FIELDS)
        row.update(timestamp=timestamp, source="UNKNOWN", error_code="none")
        for group in ("input", "output", "youtube"):
            row[group + "_status"] = "unavailable"
        elapsed = None if self._last_monotonic is None else monotonic - self._last_monotonic
        self._last_monotonic = monotonic
        if elapsed is not None and 0 < elapsed < MAX_SAMPLE_GAP_SECONDS:
            row["sample_interval_seconds"] = round(elapsed, 3)
        if active is False or body is None:
            self._previous.clear()
            row["source"] = "NONE" if active is False else "UNKNOWN"
            row["error_code"] = "service_inactive" if active is False else "metrics_unavailable"
            return row
        try:
            metrics = parse_metrics(body)
        except HistoryError:
            self._previous.clear()
            row["error_code"] = "metrics_invalid"
            return row
        inputs, input_status = _publisher(metrics, "srt_conns", _INPUT_PATH)
        outputs, output_status = _publisher(metrics, "rtmp_conns", _OUTPUT_PATH)
        youtube, youtube_status = _publisher(metrics, "forward_dests", _OUTPUT_PATH, forward=True)
        paths = [
            (labels, value)
            for labels, value in metrics["paths"]
            if labels.get("name") == _OUTPUT_PATH
        ]
        ready = len(paths) == 1 and paths[0][0].get("state") == "ready" and paths[0][1] == 1
        if ready and input_status in {"ok", "absent"} and output_status in {"ok", "absent"}:
            row["source"] = "LIVE" if inputs is not None and outputs is not None else "SLATE"
        if active is None:
            row["error_code"] = "service_unknown"
        self._sample_group(
            row,
            metrics,
            "input",
            inputs,
            input_status,
            monotonic,
            ("input_unique_bytes", "input_gross_bytes", *_PACKETS),
        )
        self._sample_group(
            row, metrics, "output", outputs, output_status, monotonic, ("output_rtmp_bytes",)
        )
        self._sample_group(
            row, metrics, "youtube", youtube, youtube_status, monotonic, ("youtube_outbound_bytes",)
        )
        if inputs is not None:
            rtt = _metric_value(metrics, "srt_conns_ms_rtt", inputs)
            row["srt_rtt_ms"] = rtt if rtt is not None and rtt <= 60_000 else None
        return row

    def _sample_group(
        self,
        row: HistoryRow,
        metrics: Metrics,
        group: str,
        publisher: dict[str, str] | None,
        status: str,
        monotonic: float,
        fields: tuple[str, ...],
    ) -> None:
        row[group + "_status"] = status
        if publisher is None:
            self._previous.pop(group, None)
            return
        identity = publisher["id"]
        values = {field: _metric_value(metrics, _COUNTERS[field], publisher) for field in fields}
        row.update(values)
        previous = self._previous.get(group)
        self._previous[group] = (identity, monotonic, values)
        if previous is None:
            row[group + "_status"] = "baseline"
            return
        old_id, old_time, old_values = previous
        elapsed = monotonic - old_time
        if identity != old_id:
            row[group + "_status"] = "reconnected"
            return
        if not 0 < elapsed < MAX_SAMPLE_GAP_SECONDS:
            row[group + "_status"] = "sample_gap"
            return
        if any(
            value is not None and old is not None and value < old
            for field, value in values.items()
            for old in (old_values[field],)
        ):
            row[group + "_status"] = "counter_reset"
            return
        for field, value in values.items():
            old = old_values[field]
            if value is None or old is None:
                row[group + "_status"] = "metric_missing"
                continue
            delta = int(value - old)
            if field in _RATES:
                rate = round(delta * 8 / elapsed)
                if rate <= _MAX_BITRATE:
                    row[_RATES[field]] = rate
                else:
                    row[group + "_status"] = "metric_missing"
                    values[field] = None
            else:
                row[field + "_delta"] = delta


def _validate_row(row: Mapping[str, object]) -> HistoryRow:
    if set(row) != set(ROW_FIELDS):
        raise HistoryError("history_invalid")
    for field in _NUMERIC_FIELDS:
        value = row[field]
        if value is not None and _number(value) is None:
            raise HistoryError("history_invalid")
    if not isinstance(row["timestamp"], int) or isinstance(row["timestamp"], bool):
        raise HistoryError("history_invalid")
    if row["source"] not in {"LIVE", "SLATE", "NONE", "UNKNOWN"}:
        raise HistoryError("history_invalid")
    if row["error_code"] not in _ERRORS:
        raise HistoryError("history_invalid")
    if any(row[group + "_status"] not in _STATUSES for group in ("input", "output", "youtube")):
        raise HistoryError("history_invalid")
    return {field: cast(Scalar, row[field]) for field in ROW_FIELDS}


def _safe_directory(path: Path, expected_uid: int, *, create: bool) -> None:
    # Check each component before creating/opening anything below it. The immediate
    # directory is private; ancestors must not be symlinks (including junctions).
    for component in reversed((path, *path.parents)):
        if component == path and create:
            component.mkdir(mode=0o700, exist_ok=True)
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise HistoryError("history_unsafe")
        if getattr(info, "st_reparse_tag", 0):
            raise HistoryError("history_unsafe")
        if os.name == "posix" and component != path:
            trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, expected_uid} or (
                info.st_mode & 0o022 and not trusted_sticky
            ):
                raise HistoryError("history_unsafe")
        if (
            component == path
            and os.name == "posix"
            and (info.st_uid != expected_uid or info.st_mode & 0o777 != 0o700)
        ):
            raise HistoryError("history_unsafe")


def _safe_file(path: Path, expected_uid: int, *, create: bool = False) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = -1
    try:
        if not create:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise HistoryError("history_unsafe")
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        after = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino)
            or info.st_size > MAX_DATABASE_BYTES
            or (
                os.name == "posix"
                and (info.st_uid != expected_uid or info.st_mode & 0o777 != 0o600)
            )
        ):
            raise HistoryError("history_unsafe")
    finally:
        if fd >= 0:
            os.close(fd)


class HistoryStore:
    def __init__(
        self,
        path: Path = HISTORY_PATH,
        *,
        expected_uid: int = 0,
        max_rows: int = MAX_ROWS,
        retention_seconds: int = RETENTION_SECONDS,
    ) -> None:
        if not isinstance(expected_uid, int) or isinstance(expected_uid, bool) or expected_uid < 0:
            raise ValueError("invalid_history_owner")
        if not 1 <= max_rows <= MAX_ROWS or not 1 <= retention_seconds <= RETENTION_SECONDS:
            raise ValueError("invalid_history_bounds")
        self.path = path
        self.expected_uid = expected_uid
        self.max_rows = max_rows
        self.retention_seconds = retention_seconds

    @contextmanager
    def _connect(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        _safe_directory(self.path.parent, self.expected_uid, create=write)
        try:
            _safe_file(self.path, self.expected_uid)
        except FileNotFoundError:
            if not write:
                raise
            _safe_file(self.path, self.expected_uid, create=True)
        with self._file_lock(write=write), self._database(write=write) as connection:
            yield connection

    @contextmanager
    def _file_lock(self, *, write: bool) -> Iterator[None]:
        # This is only a history-file lock, never the relay command lock. A
        # read-only immutable SQLite connection cannot create side files; this
        # lock ensures it sees a complete committed database while a writer runs.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.path, flags)
        try:
            if os.name == "posix":
                fcntl = __import__("fcntl")
                deadline = time.monotonic() + 0.2
                operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                while True:
                    try:
                        fcntl.flock(fd, operation | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise HistoryError("history_busy") from None
                        time.sleep(0.01)
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _database(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                _safe_file(self.path.with_name(self.path.name + suffix), self.expected_uid)
            except FileNotFoundError:
                continue
            # A reader must not ignore/recover a hot journal or open a WAL DB.
            if not write or suffix != "-journal":
                raise HistoryError("history_busy")
        uri = self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro&immutable=1")
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
        try:
            deadline = time.monotonic() + 1.0
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-1024")
            if write:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
                columns = ",".join(
                    field + (" TEXT" if field in _TEXT_FIELDS else " NUMERIC")
                    for field in ROW_FIELDS
                )
                connection.execute(f"CREATE TABLE IF NOT EXISTS samples ({columns})")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS samples_timestamp ON samples(timestamp)"
                )
                connection.execute("PRAGMA user_version=1")
            else:
                connection.execute("PRAGMA query_only=ON")
            names = tuple(item[1] for item in connection.execute("PRAGMA table_info(samples)"))
            if names != ROW_FIELDS:
                raise HistoryError("history_invalid")
            yield connection
        finally:
            connection.close()

    def append(self, row: Mapping[str, object]) -> None:
        safe = _validate_row(row)
        now = cast(int, safe["timestamp"])
        with self._connect(write=True) as connection, connection:
            connection.execute(
                "DELETE FROM samples WHERE timestamp < ?", (now - self.retention_seconds,)
            )
            connection.execute(
                "DELETE FROM samples WHERE rowid IN "
                "(SELECT rowid FROM samples ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (self.max_rows - 1,),
            )
            placeholders = ",".join("?" for _ in ROW_FIELDS)
            connection.execute(
                f"INSERT INTO samples VALUES ({placeholders})",  # noqa: S608 -- fixed placeholders
                tuple(safe.values()),
            )

    def read(self, *, since: int, until: int, limit: int) -> tuple[list[HistoryRow], bool]:
        with self._connect(write=False) as connection:
            rows = connection.execute(
                "SELECT * FROM samples WHERE timestamp >= ? AND timestamp <= ? "
                "ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (since, until, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        return [
            _validate_row(dict(zip(ROW_FIELDS, row, strict=True))) for row in reversed(rows[:limit])
        ], truncated


def read_history(
    *,
    since: int | None = None,
    until: int | None = None,
    limit: int = 1000,
    path: Path = HISTORY_PATH,
    expected_uid: int = 0,
) -> dict[str, object]:
    """Root CLI helper, read-only and bounded. since/until are inclusive UTC epochs."""
    result: dict[str, object] = {
        "schema_version": 1,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "retention_seconds": RETENTION_SECONDS,
        "rows": [],
        "truncated": False,
        "error_code": "none",
    }
    now = int(time.time())
    start = max(0, now - RETENTION_SECONDS) if since is None else since
    end = now if until is None else until
    if (
        any(not isinstance(value, int) or isinstance(value, bool) for value in (start, end, limit))
        or start < 0
        or end > _MAX_NUMBER
        or end < start
        or not 1 <= limit <= MAX_EXPORT_ROWS
    ):
        result["error_code"] = "history_invalid_request"
        return result
    try:
        rows, truncated = HistoryStore(path, expected_uid=expected_uid).read(
            since=max(start, now - RETENTION_SECONDS), until=end, limit=limit
        )
        result.update(rows=rows, truncated=truncated)
    except FileNotFoundError:
        result["error_code"] = "history_missing"
    except (HistoryError, OSError, sqlite3.Error, ValueError, TypeError):
        result["error_code"] = "history_unavailable"
    return result


def read_loopback_metrics() -> str:
    """Fixed HTTP/1.0 close-delimited request, absolute deadline, no proxy/redirects.

    Reading the raw close-delimited response avoids urllib's environment proxies
    and per-read timeouts that an endless trickle can repeatedly extend.
    """
    deadline = time.monotonic() + 2.0
    chunks = bytearray()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(2.0)
        connection.connect(("127.0.0.1", 9998))
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(
            b"GET /metrics HTTP/1.0\r\nHost: 127.0.0.1:9998\r\nConnection: close\r\n\r\n"
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HistoryError("metrics_unavailable")
            connection.settimeout(remaining)
            chunk = connection.recv(min(65_536, MAX_METRICS_BYTES + 16_384 + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_METRICS_BYTES + 16_384:
                raise HistoryError("metrics_invalid")
    header, separator, body = bytes(chunks).partition(b"\r\n\r\n")
    if not separator or len(header) > 16_384 or len(body) > MAX_METRICS_BYTES:
        raise HistoryError("metrics_invalid")
    lines = header.split(b"\r\n")
    if lines[0] not in {b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK"}:
        raise HistoryError("metrics_unavailable")
    # HTTP/1.0 has no chunked transfer coding; reject unexpected encodings.
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name.lower() in {b"transfer-encoding", b"content-encoding"}:
            raise HistoryError("metrics_invalid")
        if name.lower() == b"content-length" and value.strip() != str(len(body)).encode("ascii"):
            raise HistoryError("metrics_invalid")
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HistoryError("metrics_invalid") from exc


def service_active() -> bool | None:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed read-only service query
            ["/usr/bin/systemctl", "is-active", "--quiet", "moblin-relay.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return True if result.returncode == 0 else False if result.returncode == 3 else None


class HistoryCollector:
    """A best-effort thread in the root broker; never enters its control lock."""

    def __init__(
        self,
        store: HistoryStore | None = None,
        *,
        metrics_reader: Callable[[], str] = read_loopback_metrics,
        active_reader: Callable[[], bool | None] = service_active,
        interval: float = SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid_history_interval")
        self._store = store if store is not None else HistoryStore()
        self._metrics_reader = metrics_reader
        self._active_reader = active_reader
        self._interval = interval
        self._sampler = HistorySampler()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log = -math.inf

    def _log_failure(self) -> None:
        # No exception formatting, tracebacks or raw metrics, and at most one/minute.
        now = time.monotonic()
        if now - self._last_log >= 60:
            self._last_log = now
            with suppress(Exception):
                print("relay_history_collection_failed", file=sys.stderr, flush=True)

    def collect_once(self) -> None:
        try:
            active = self._active_reader()
        except Exception:
            active = None
        try:
            body = self._metrics_reader()
        except Exception:
            body = None
        try:
            row = self._sampler.sample(
                body, timestamp=int(time.time()), monotonic=time.monotonic(), active=active
            )
            self._store.append(row)
        except Exception:
            self._log_failure()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.collect_once()
            self._stop.wait(max(0.01, self._interval - (time.monotonic() - started)))

    def start(self) -> None:
        try:
            self._thread = threading.Thread(target=self._run, name="relay-history", daemon=True)
            self._thread.start()
        except Exception:
            self._thread = None
            self._log_failure()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.25)
