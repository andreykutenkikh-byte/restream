"""Root/POSIX history checks using Python 3.10 stdlib and temporary paths only.

Run from the checkout: sudo python3 -m scripts.check_relay_history_linux
No production paths, credentials, network requests, service calls or dependencies.
"""

from __future__ import annotations

import io
import json
import math
import os
import select
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast

import relay_agent.history as history

_MARKER = "SYNTHETIC_HISTORY_SECRET_3742987"
_INPUT_ID = "01234567-89ab-4cde-8fab-012345678901"
_NEXT_ID = "01234567-89ab-4cde-8fab-012345678902"


class CheckFailed(Exception):
    """Only fixed safe check names are raised."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise CheckFailed(code)


def rejected(operation: Callable[[], object], code: str) -> None:
    try:
        operation()
    except (history.HistoryError, sqlite3.Error, OSError):
        return
    raise CheckFailed(code)


def metrics(value: int = 1000, identity: str = _INPUT_ID) -> str:
    labels = (
        f'id="{identity}",path="iphone-live",state="publish",'
        f'remoteAddr="198.51.100.7:8890",url="rtmps://example/{_MARKER}"'
    )
    lines = [
        'paths{name="relay-output",state="ready"} 1',
        f"srt_conns{{{labels}}} 1",
        f"srt_conns_bytes_received_unique{{{labels}}} {value}",
        f"srt_conns_bytes_received{{{labels}}} {value * 2}",
        f"srt_conns_ms_rtt{{{labels}}} 30.5",
    ]
    for suffix in ("", "_unique", "_loss", "_drop", "_retrans"):
        lines.append(f"srt_conns_packets_received{suffix}{{{labels}}} {value // 100}")
    labels = f'id="{identity}",path="relay-output",state="publish"'
    lines.extend(
        [
            f"rtmp_conns{{{labels}}} 1",
            f"rtmp_conns_inbound_bytes{{{labels}}} {value * 3}",
        ]
    )
    labels = f'id="{identity}",path="relay-output",protocol="rtmps",state="forwarding"'
    lines.extend(
        [
            f"forward_dests{{{labels}}} 1",
            f"forward_dests_outbound_bytes{{{labels}}} {value * 4}",
        ]
    )
    return "\n".join(lines)


def row(timestamp: int | None = None) -> history.HistoryRow:
    return history.HistorySampler().sample(
        metrics(),
        timestamp=int(time.time()) if timestamp is None else timestamp,
        monotonic=100.0,
        active=True,
    )


def file_state(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
        for path in directory.iterdir()
    }


def check_metrics() -> None:
    sampler = history.HistorySampler()
    first = sampler.sample(metrics(), timestamp=1000, monotonic=100, active=True)
    require(first["input_unique_bitrate_bps"] is None, "baseline_is_null")
    second = sampler.sample(metrics(2000), timestamp=1005, monotonic=105, active=True)
    require(second["input_unique_bitrate_bps"] == 1600, "unique_rate")
    require(second["input_gross_bitrate_bps"] == 3200, "gross_rate")
    require(second["output_rtmp_bitrate_bps"] == 4800, "output_rate")
    require(second["youtube_outbound_bitrate_bps"] == 6400, "forward_rate")
    stalled = sampler.sample(metrics(2000), timestamp=1010, monotonic=110, active=True)
    require(stalled["input_unique_bitrate_bps"] == 0, "proven_stall_is_zero")
    for body, mono in ((metrics(100), 115), (metrics(2000, _NEXT_ID), 120), (metrics(3000), 150)):
        sampled = sampler.sample(body, timestamp=1000 + mono, monotonic=mono, active=True)
        require(sampled["input_unique_bitrate_bps"] is None, "reset_reconnect_gap_is_null")
    for bad in ("NaN", "+Inf", "-1", "1.5", "9007199254740992"):
        body = "\n".join(
            line.rsplit(" ", 1)[0] + " " + bad
            if line.startswith("srt_conns_bytes_received_unique{")
            else line
            for line in metrics().splitlines()
        )
        sampled = sampler.sample(body, timestamp=2000, monotonic=155, active=True)
        require(sampled["input_unique_bytes"] is None, "nonfinite_counter_is_null")
    failed = sampler.sample(None, timestamp=2005, monotonic=160, active=True)
    require(failed["error_code"] == "metrics_unavailable", "missing_metrics_fixed_error")
    for value in second.values():
        if isinstance(value, (float, int)):
            require(math.isfinite(value), "only_finite_numbers")
    encoded = json.dumps(second)
    require(
        all(marker not in encoded for marker in (_MARKER, _INPUT_ID, "198.51.100")),
        "no_labels_in_rows",
    )


def check_storage(root: Path) -> None:
    path = root / "bounded" / "history.sqlite3"
    store = history.HistoryStore(path, max_rows=3, retention_seconds=10)
    now = int(time.time())
    for offset in (0, 1, 2, 3):
        store.append(row(now - 20 + offset))
    samples, _ = store.read(since=0, until=now, limit=10)
    require(len(samples) == 3, "hard_row_bound")
    store.append(row(now))
    samples, _ = store.read(since=0, until=now, limit=10)
    require(len(samples) == 1, "retention_bound")
    require(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, "directory_mode")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "database_mode")
    require(path.stat().st_uid == 0 and path.stat().st_nlink == 1, "database_owner_nlink")
    require(path.stat().st_size <= history.MAX_DATABASE_BYTES, "database_size")
    before = file_state(path.parent)
    exported = history.read_history(path=path)
    require(exported["error_code"] == "none", "readonly_export")
    require(before == file_state(path.parent), "export_no_file_mutation")
    content = path.read_bytes()
    require(
        all(
            marker.encode() not in content
            for marker in (_MARKER, _INPUT_ID, "198.51.100", "remoteAddr", "rtmps:")
        ),
        "no_labels_on_disk",
    )
    invalid = row()
    invalid["source"] = _MARKER
    rejected(lambda: store.append(invalid), "reject_untrusted_persisted_state")


def check_filesystem_attacks(root: Path) -> None:
    path = root / "attacks" / "history.sqlite3"
    store = history.HistoryStore(path)
    store.append(row())
    path.chmod(0o644)
    rejected(lambda: store.append(row()), "reject_database_mode")
    path.chmod(0o600)
    chown = cast(Callable[[Path, int, int], None], os.__dict__["chown"])
    chown(path, 1, 1)
    try:
        rejected(lambda: store.append(row()), "reject_database_owner")
    finally:
        chown(path, 0, 0)
    path.parent.chmod(0o755)
    rejected(lambda: store.append(row()), "reject_directory_mode")
    path.parent.chmod(0o700)
    directory_link = root / "directory-link"
    directory_link.symlink_to(path.parent, target_is_directory=True)
    rejected(
        lambda: history.HistoryStore(directory_link / path.name).append(row()),
        "reject_directory_symlink",
    )
    target = root / "sentinel"
    target.write_text(_MARKER)
    target.chmod(0o600)
    for suffix in ("-journal", "-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        before = file_state(path.parent)
        for symlink in (True, False):
            if symlink:
                side.symlink_to(target)
            else:
                os.link(target, side)
            rejected(lambda: store.append(row()), "reject_sidefile_link")
            require(
                history.read_history(path=path)["error_code"] == "history_unavailable",
                "reject_export_sidefile_link",
            )
            side.unlink()
            require(file_state(path.parent) == before, "sidefile_attack_no_database_mutation")
    path.unlink()
    path.symlink_to(target)
    rejected(lambda: store.append(row()), "reject_database_symlink")
    path.unlink()
    os.link(target, path)
    rejected(lambda: store.append(row()), "reject_database_hardlink")
    require(target.read_text() == _MARKER, "sentinel_unchanged")


def check_locking_and_concurrency(root: Path) -> None:
    fcntl = __import__("fcntl")

    path = root / "concurrent" / "history.sqlite3"
    store = history.HistoryStore(path)
    store.append(row())
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        rejected(lambda: store.append(row()), "writer_lock_timeout")
        require(
            history.read_history(path=path)["error_code"] == "history_unavailable",
            "reader_lock_timeout",
        )
        require(time.monotonic() - started < 1.5, "bounded_history_lock")
    finally:
        os.close(fd)
    stop = threading.Event()
    errors: list[str] = []
    written: list[int] = []

    def writer() -> None:
        try:
            for index in range(30):
                if stop.is_set():
                    break
                store.append(row())
                written.append(index)
                time.sleep(0.002)
        except Exception:
            errors.append("concurrent_writer_failed")
        finally:
            stop.set()

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    deadline = time.monotonic() + 8.0
    reads = 0
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            result = history.read_history(path=path)
            require(
                result["error_code"] in {"none", "history_unavailable"}, "concurrent_export_code"
            )
            if result["error_code"] == "none":
                reads += 1
                require(bool(result["rows"]), "concurrent_export_committed_rows")
            time.sleep(0.005)
    finally:
        stop.set()
        worker.join(timeout=3)
    require(
        not worker.is_alive() and not errors and len(written) == 30, "concurrent_writer_finished"
    )
    require(reads > 0, "concurrent_read_succeeded")
    before = file_state(path.parent)
    require(history.read_history(path=path)["error_code"] == "none", "final_export")
    require(file_state(path.parent) == before, "concurrent_export_no_sidefiles")


def check_database_failures(root: Path) -> None:
    path = root / "corrupt" / "history.sqlite3"
    store = history.HistoryStore(path)
    store.append(row())
    with sqlite3.connect(path) as database:
        database.execute("UPDATE samples SET source=?", (_MARKER,))
    require(
        history.read_history(path=path)["error_code"] == "history_unavailable",
        "disk_row_revalidate",
    )
    side = path.with_name(path.name + "-journal")
    side.write_bytes(b"synthetic unrecovered journal")
    side.chmod(0o600)
    before = file_state(path.parent)
    require(
        history.read_history(path=path)["error_code"] == "history_unavailable", "refuse_hot_journal"
    )
    require(file_state(path.parent) == before, "readonly_no_journal_recovery")
    side.unlink()
    path.write_bytes(b"synthetic malformed sqlite")
    require(
        history.read_history(path=path)["error_code"] == "history_unavailable", "malformed_database"
    )
    sink = io.StringIO()
    collector = history.HistoryCollector(store, metrics_reader=metrics, active_reader=lambda: True)
    with redirect_stderr(sink):
        collector.collect_once()
        collector.collect_once()
    require(sink.getvalue() == "relay_history_collection_failed\n", "fail_open_bounded_safe_log")

    cap = history.MAX_DATABASE_BYTES
    history.MAX_DATABASE_BYTES = 16 * 1024
    try:
        bounded_path = root / "disk-cap" / "history.sqlite3"
        bounded = history.HistoryStore(bounded_path)
        did_fail = False
        for _ in range(500):
            try:
                bounded.append(row())
            except sqlite3.DatabaseError:
                did_fail = True
                break
        require(did_fail and bounded_path.stat().st_size <= 16 * 1024, "hard_database_byte_cap")
    finally:
        history.MAX_DATABASE_BYTES = cap


def check_collector_thread(root: Path) -> None:
    from relay_agent.broker import RelayBroker, _execute_bounded_request
    from relay_agent.models import JsonObject

    class SyntheticBroker:
        @staticmethod
        def reconciliation_state(_request: object) -> None:
            return None

        @staticmethod
        def handle(_request: object, *, relay_lock_held: bool = False) -> JsonObject:
            del relay_lock_held
            return {"status": "ok", "safe_result": {}, "secret_result": None}

    path = root / "collector" / "history.sqlite3"
    store = history.HistoryStore(path)
    sampled = threading.Event()
    calls = 0

    def fake_metrics() -> str:
        nonlocal calls
        calls += 1
        if calls >= 3:
            sampled.set()
        return metrics(1000 + calls * 1000)

    collector = history.HistoryCollector(
        store, metrics_reader=fake_metrics, active_reader=lambda: True, interval=0.02
    )
    collector.start()
    try:
        require(sampled.wait(2), "independent_collector_thread")
        for _ in range(3):
            response = _execute_bounded_request(
                cast(RelayBroker, SyntheticBroker()),
                {"action": "status", "payload": {}},
                client_deadline=time.monotonic() + 2.0,
                action_timeout_seconds=0.5,
            )
            require(json.loads(response)["status"] == "ok", "forked_broker_with_collector")
    finally:
        collector.close()
    result = history.read_history(path=path)
    require(result["error_code"] == "none", "thread_persisted_history")
    rows = cast(list[history.HistoryRow], result["rows"])
    require(len(rows) >= 2, "thread_sampled_without_requests")


def check_scoped_child_reaper(_root: Path) -> None:
    from relay_agent.broker import _enable_child_subreaper, _terminate_worker

    _enable_child_subreaper()
    fork = cast(Callable[[], int], os.__dict__["fork"])
    setsid = cast(Callable[[], None], os.__dict__["setsid"])
    waitpid = cast(Callable[[int, int], tuple[int, int]], os.__dict__["waitpid"])
    # Reproduce the collector's Popen ownership race deterministically: leave
    # an unrelated exit(3) child waiting while a broker worker group is reaped.
    with subprocess.Popen(  # noqa: S603 -- fixed synthetic child, no shell/network
        [sys.executable, "-c", "import os; os._exit(3)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as helper:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = Path(f"/proc/{helper.pid}/stat").read_text().rpartition(")")[2].split()[0]
            if state == "Z":
                break
            time.sleep(0.005)
        else:
            raise CheckFailed("synthetic_helper_exit_ready")
        read_fd, write_fd = os.pipe()
        worker = fork()
        if worker == 0:
            os.close(read_fd)
            setsid()
            descendant = fork()
            if descendant == 0:
                os.close(write_fd)
                time.sleep(10)
                os._exit(0)
            os.write(write_fd, str(descendant).encode("ascii"))
            os.close(write_fd)
            time.sleep(10)
            os._exit(0)
        os.close(write_fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 2)
            require(bool(readable), "synthetic_worker_group_ready")
            descendant = int(os.read(read_fd, 32))
        finally:
            os.close(read_fd)
            _terminate_worker(worker)
        require(helper.wait(timeout=1) == 3, "collector_helper_exit_status_preserved")
        try:
            waitpid(descendant, int(os.__dict__["WNOHANG"]))
        except ChildProcessError:
            pass
        else:
            raise CheckFailed("adopted_worker_descendant_reaped")
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            pass
        else:
            raise CheckFailed("worker_descendant_terminated")


def main() -> int:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("relay_history_linux_checks_require_root_posix", file=sys.stderr)
        return 2
    try:
        check_metrics()
        with tempfile.TemporaryDirectory(prefix="relay-history-check-") as temporary:
            root = Path(temporary)
            for check in (
                check_storage,
                check_filesystem_attacks,
                check_locking_and_concurrency,
                check_database_failures,
                check_collector_thread,
                check_scoped_child_reaper,
            ):
                check(root)
    except CheckFailed as exc:
        print("relay_history_linux_checks_failed:" + str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("relay_history_linux_checks_failed:unexpected_error", file=sys.stderr)
        return 1
    print("relay_history_linux_checks_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
