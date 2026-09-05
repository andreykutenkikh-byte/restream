"""History schema fixtures follow MediaMTX v1.20.1 internal/metrics/metrics.go."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relay_agent.history import (
    HISTORY_PATH,
    MAX_DATABASE_BYTES,
    MAX_EXPORT_ROWS,
    MAX_METRICS_BYTES,
    MAX_ROWS,
    RETENTION_SECONDS,
    ROW_FIELDS,
    HistoryCollector,
    HistoryError,
    HistorySampler,
    HistoryStore,
    parse_metrics,
    read_history,
    read_loopback_metrics,
)
from relay_agent.security import effective_uid

INPUT_ID = "01234567-89ab-4cde-8fab-012345678901"
OUTPUT_ID = "01234567-89ab-4cde-8fab-012345678902"
FORWARD_ID = "01234567-89ab-4cde-8fab-012345678903"
NEXT_ID = "01234567-89ab-4cde-8fab-012345678904"
SECRET = "FAKE_STREAM_KEY_DO_NOT_PERSIST_38247"


def test_history_has_dedicated_root_directory() -> None:
    assert HISTORY_PATH.as_posix() == "/var/lib/adojapan-relay-history/history.sqlite3"


def test_broker_reaper_waits_only_for_its_worker_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import relay_agent.broker as broker_module

    waited = []

    def waitpid(pid, options):
        waited.append((pid, options))
        raise ChildProcessError

    monkeypatch.setattr(broker_module, "_subreaper_enabled", True)
    monkeypatch.setattr(broker_module, "_waitpid_nointr", waitpid)
    broker_module._reap_adopted_children(12345)
    assert waited == [(-12345, getattr(os, "WNOHANG", 1))]
    broker_module._reap_adopted_children(0)
    assert len(waited) == 1


def fixture(
    value: int = 1000,
    *,
    input_id: str = INPUT_ID,
    input_present: bool = True,
    output_present: bool = True,
    forward_state: str = "forwarding",
) -> str:
    # The unknown labels deliberately carry secrets and addresses. Production
    # metrics expose remoteAddr, while arbitrary extra labels must be discarded.
    unsafe_labels = f',remoteAddr="198.51.100.7:1935",url="rtmps://example/{SECRET}"'
    inputs = f'id="{input_id}",path="iphone-live",state="publish"{unsafe_labels}'
    outputs = f'id="{OUTPUT_ID}",path="relay-output",state="publish"{unsafe_labels}'
    forward = f'id="{FORWARD_ID}",path="relay-output",protocol="rtmps",state="{forward_state}"'
    lines = ['paths{name="relay-output",state="ready"} 1']
    if input_present:
        lines.extend(
            [
                f"srt_conns{{{inputs}}} 1",
                f"srt_conns_bytes_received_unique{{{inputs}}} {value}",
                f"srt_conns_bytes_received{{{inputs}}} {value * 2}",
                f"srt_conns_ms_rtt{{{inputs}}} 35.5",
            ]
        )
        for metric in (
            "packets_received",
            "packets_received_unique",
            "packets_received_loss",
            "packets_received_drop",
            "packets_received_retrans",
        ):
            lines.append(f"srt_conns_{metric}{{{inputs}}} {value // 100}")
    else:
        lines.append("srt_conns 0")
    if output_present:
        lines.extend(
            [f"rtmp_conns{{{outputs}}} 1", f"rtmp_conns_inbound_bytes{{{outputs}}} {value * 3}"]
        )
    else:
        lines.append("rtmp_conns 0")
    lines.extend(
        [f"forward_dests{{{forward}}} 1", f"forward_dests_outbound_bytes{{{forward}}} {value * 4}"]
    )
    return "\n".join(lines) + "\n"


def sample(sampler: HistorySampler, value: int = 1000, *, mono: float = 100, **kwargs: Any):
    return sampler.sample(
        fixture(value, **kwargs), timestamp=1000 + int(mono), monotonic=mono, active=True
    )


def test_exact_metric_names_safe_per_source_deltas_and_stalls() -> None:
    sampler = HistorySampler()
    baseline = sample(sampler)
    assert set(baseline) == set(ROW_FIELDS)
    assert baseline["source"] == "LIVE"
    assert baseline["input_unique_bytes"] == 1000
    assert baseline["input_unique_bitrate_bps"] is None
    assert baseline["input_status"] == "baseline"
    row = sample(sampler, 2000, mono=105)
    assert row["input_unique_bitrate_bps"] == 1600
    assert row["input_gross_bitrate_bps"] == 3200
    assert row["output_rtmp_bitrate_bps"] == 4800
    assert row["youtube_outbound_bitrate_bps"] == 6400
    assert row["srt_rtt_ms"] == 35.5
    assert row["sample_interval_seconds"] == 5
    for suffix in ("", "_unique", "_loss", "_drop", "_retrans"):
        assert row[f"srt_packets_received{suffix}_delta"] == 10
    stalled = sample(sampler, 2000, mono=110)
    assert stalled["input_unique_bitrate_bps"] == 0
    assert stalled["output_rtmp_bitrate_bps"] == 0
    assert stalled["youtube_outbound_bitrate_bps"] == 0
    assert stalled["srt_packets_received_loss_delta"] == 0
    encoded = json.dumps(row)
    for value in (SECRET, INPUT_ID, OUTPUT_ID, FORWARD_ID, "198.51.100", "remoteAddr", "rtmps:"):
        assert value not in encoded


@pytest.mark.parametrize("bad", ["NaN", "+Inf", "-Inf", "-1", "1.5", "9007199254740992"])
def test_missing_nonfinite_negative_fractional_and_oversize_counters_are_null(bad: str) -> None:
    sampler = HistorySampler()
    sample(sampler)
    body = fixture(2000)
    lines = body.splitlines()
    body = "\n".join(
        line.rsplit(" ", 1)[0] + " " + bad
        if line.startswith("srt_conns_bytes_received_unique{")
        else line
        for line in lines
    )
    row = sampler.sample(body, timestamp=1105, monotonic=105, active=True)
    assert row["input_unique_bytes"] is None
    assert row["input_unique_bitrate_bps"] is None
    assert row["input_gross_bitrate_bps"] == 3200
    assert row["output_rtmp_bitrate_bps"] == 4800
    assert sample(sampler, 3000, mono=110)["input_unique_bitrate_bps"] is None
    assert sample(sampler, 4000, mono=115)["input_unique_bitrate_bps"] == 1600


def test_missing_metric_does_not_use_aggregate_or_deprecated_substitute() -> None:
    sampler = HistorySampler()
    sample(sampler)
    body = "\n".join(
        line
        for line in fixture(2000).splitlines()
        if not line.startswith("srt_conns_bytes_received_unique{")
    )
    body += "\nsrt_conns_bytes_received_unique 0\n"
    row = sampler.sample(body, timestamp=1105, monotonic=105, active=True)
    assert row["input_unique_bytes"] is None
    assert row["input_unique_bitrate_bps"] is None
    assert row["input_status"] == "metric_missing"


def test_missing_publisher_family_cannot_claim_slate() -> None:
    sampler = HistorySampler()
    sample(sampler)
    body = "\n".join(line for line in fixture().splitlines() if not line.startswith("srt_conns"))
    row = sampler.sample(body, timestamp=1105, monotonic=105, active=True)
    assert row["source"] == "UNKNOWN"
    assert row["input_status"] == "metric_missing"
    assert row["input_unique_bitrate_bps"] is None


def test_reconnect_reset_stale_and_nonincreasing_time_break_group_baselines() -> None:
    sampler = HistorySampler()
    sample(sampler)
    reconnect = sample(sampler, 2000, mono=105, input_id=NEXT_ID)
    assert reconnect["input_status"] == "reconnected"
    assert reconnect["input_unique_bitrate_bps"] is None
    assert reconnect["srt_packets_received_loss_delta"] is None
    assert reconnect["output_rtmp_bitrate_bps"] == 4800
    reset = sample(sampler, 500, mono=110, input_id=NEXT_ID)
    assert reset["input_status"] == "counter_reset"
    assert reset["input_unique_bitrate_bps"] is None
    assert reset["output_rtmp_bitrate_bps"] is None
    long_gap = sample(sampler, 2000, mono=130, input_id=NEXT_ID)
    assert long_gap["input_status"] == "sample_gap"
    assert long_gap["input_unique_bitrate_bps"] is None
    assert long_gap["sample_interval_seconds"] is None
    duplicate_time = sample(sampler, 3000, mono=130, input_id=NEXT_ID)
    assert duplicate_time["input_unique_bitrate_bps"] is None
    assert duplicate_time["output_rtmp_bitrate_bps"] is None


def test_ambiguous_publishers_and_duplicate_counter_never_sum_or_select_first() -> None:
    sampler = HistorySampler()
    sample(sampler)
    body = fixture(2000)
    ingress = next(line for line in body.splitlines() if line.startswith("srt_conns{"))
    body += ingress.replace(INPUT_ID, NEXT_ID) + "\n"
    row = sampler.sample(body, timestamp=1105, monotonic=105, active=True)
    assert row["source"] == "UNKNOWN"
    assert row["input_status"] == "ambiguous"
    assert row["input_unique_bytes"] is None
    assert row["input_unique_bitrate_bps"] is None
    assert row["srt_rtt_ms"] is None
    assert row["output_rtmp_bitrate_bps"] == 4800
    assert sample(sampler, 3000, mono=110)["input_unique_bitrate_bps"] is None
    body = fixture(4000)
    body += (
        next(
            line
            for line in body.splitlines()
            if line.startswith("srt_conns_bytes_received_unique{")
        )
        + "\n"
    )
    row = sampler.sample(body, timestamp=1115, monotonic=115, active=True)
    assert row["input_unique_bytes"] is None
    assert row["input_unique_bitrate_bps"] is None


def test_observed_forward_retry_breaks_baseline_even_with_same_destination_id() -> None:
    sampler = HistorySampler()
    sample(sampler)
    error = sample(sampler, 2000, mono=105, forward_state="error")
    assert error["youtube_outbound_bitrate_bps"] is None
    assert error["youtube_status"] == "absent"
    resumed = sample(sampler, 3000, mono=110)
    assert resumed["youtube_status"] == "baseline"
    assert resumed["youtube_outbound_bitrate_bps"] is None
    assert sample(sampler, 4000, mono=115)["youtube_outbound_bitrate_bps"] == 6400


@pytest.mark.parametrize(
    ("body", "active", "source", "error"),
    [
        (None, False, "NONE", "service_inactive"),
        (None, True, "UNKNOWN", "metrics_unavailable"),
        (None, None, "UNKNOWN", "metrics_unavailable"),
        ("nonsense", True, "UNKNOWN", "metrics_invalid"),
        (fixture(input_present=False), True, "SLATE", "none"),
        (fixture(output_present=False), True, "SLATE", "none"),
        (fixture(), None, "LIVE", "service_unknown"),
    ],
)
def test_fixed_source_and_error_states(body, active, source, error) -> None:
    sampler = HistorySampler()
    row = sampler.sample(body, timestamp=1100, monotonic=100, active=active)
    assert row["source"] == source
    assert row["error_code"] == error
    assert row["input_unique_bitrate_bps"] is None


def test_parser_bounds_strict_labels_and_memory_allowlist() -> None:
    metrics = parse_metrics(fixture())
    assert all(
        "remoteAddr" not in labels and "url" not in labels
        for samples in metrics.values()
        for labels, _ in samples
    )
    assert SECRET not in repr(metrics)
    for body in (
        "x" * (MAX_METRICS_BYTES + 1),
        'paths{name="relay-output",name="relay-output"} 1',
        'paths{name="relay-output",oops} 1',
        'paths{name="' + "x" * 17_000 + '"} 1',
        'paths{name="relay-output",state="ready"} 1\n' * 65,
    ):
        with pytest.raises(HistoryError, match="metrics_invalid"):
            parse_metrics(body)


def test_storage_root_private_bounded_retention_and_no_secret_bytes(tmp_path: Path) -> None:
    path = tmp_path / "history" / "history.sqlite3"
    store = HistoryStore(path, expected_uid=effective_uid(), max_rows=3, retention_seconds=10)
    sampler = HistorySampler()
    for tick in (100, 101, 102, 103):
        store.append(sample(sampler, tick * 100, mono=tick))
    rows, truncated = store.read(since=0, until=9999, limit=10)
    assert [row["timestamp"] for row in rows] == [1101, 1102, 1103]
    assert not truncated
    store.append(sample(sampler, 20_000, mono=120))
    rows, truncated = store.read(since=0, until=9999, limit=10)
    assert [row["timestamp"] for row in rows] == [1120]
    assert len(list(path.parent.iterdir())) == 1
    assert path.stat().st_size <= MAX_DATABASE_BYTES
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.stat().st_uid == effective_uid()
    content = path.read_bytes()
    for marker in (SECRET, INPUT_ID, OUTPUT_ID, FORWARD_ID, "198.51.100.7", "remoteAddr", "rtmps:"):
        assert marker.encode() not in content


def test_history_export_is_bounded_readonly_with_missing_and_invalid_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "history" / "history.sqlite3"
    monkeypatch.setattr("relay_agent.history.time.time", lambda: 2000)
    assert read_history(path=path)["error_code"] == "history_missing"
    assert not path.parent.exists()
    store = HistoryStore(path, expected_uid=effective_uid())
    sampler = HistorySampler()
    for tick in range(100, 110):
        store.append(sample(sampler, tick * 100, mono=tick))
    before = {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in path.parent.iterdir()
    }
    result = read_history(path=path, since=1102, until=1108, limit=3, expected_uid=effective_uid())
    assert result["error_code"] == "none"
    assert result["truncated"] is True
    assert [row["timestamp"] for row in result["rows"]] == [1106, 1107, 1108]
    after = {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in path.parent.iterdir()
    }
    assert before == after
    for kwargs in (
        {"limit": MAX_EXPORT_ROWS + 1},
        {"since": -1},
        {"until": 1, "since": 2},
        {"limit": True},
        {"since": "secret"},
    ):
        assert read_history(path=path, **kwargs)["error_code"] == "history_invalid_request"


def test_storage_rejects_nonallowlisted_fields_and_values(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history" / "history.sqlite3", expected_uid=effective_uid())
    row = sample(HistorySampler())
    for invalid in (
        {**row, "secret": SECRET},
        {**row, "source": SECRET},
        {**row, "error_code": SECRET},
        {**row, "input_status": SECRET},
        {**row, "input_unique_bytes": SECRET},
        {**row, "srt_rtt_ms": float("nan")},
    ):
        with pytest.raises(HistoryError, match="history_invalid"):
            store.append(invalid)
    assert not store.path.exists()
    for kwargs in ({"max_rows": MAX_ROWS + 1}, {"retention_seconds": RETENTION_SECONDS + 1}):
        with pytest.raises(ValueError, match="invalid_history_bounds"):
            HistoryStore(**kwargs)


def test_hardlinked_database_and_sidefiles_are_never_modified(tmp_path: Path) -> None:
    private = tmp_path / "history"
    private.mkdir(mode=0o700)
    target = tmp_path / "valuable"
    target.write_text(SECRET)
    target.chmod(0o600)
    path = private / "history.sqlite3"
    os.link(target, path)
    store = HistoryStore(path, expected_uid=effective_uid())
    with pytest.raises(HistoryError, match="history_unsafe"):
        store.append(sample(HistorySampler()))
    assert target.read_text() == SECRET
    path.unlink()
    store.append(sample(HistorySampler()))
    for suffix in ("-journal", "-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        os.link(target, side)
        before = path.read_bytes()
        with pytest.raises(HistoryError, match="history_unsafe"):
            store.append(sample(HistorySampler()))
        assert path.read_bytes() == before
        assert target.read_text() == SECRET
        side.unlink()


@pytest.mark.skipif(os.name != "posix", reason="Unix ownership/modes/symlink semantics")
def test_unsafe_modes_owners_symlinks_and_parent_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "history" / "history.sqlite3"
    store = HistoryStore(path, expected_uid=effective_uid())
    store.append(sample(HistorySampler()))
    path.chmod(0o644)
    with pytest.raises(HistoryError, match="history_unsafe"):
        store.append(sample(HistorySampler()))
    path.chmod(0o600)
    wrong_owner = HistoryStore(path, expected_uid=effective_uid() + 1)
    with pytest.raises(HistoryError, match="history_unsafe"):
        wrong_owner.append(sample(HistorySampler()))
    path.parent.chmod(0o755)
    with pytest.raises(HistoryError, match="history_unsafe"):
        store.append(sample(HistorySampler()))
    path.parent.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(HistoryError, match="history_unsafe"):
        HistoryStore(alias / path.name, expected_uid=effective_uid()).append(
            sample(HistorySampler())
        )
    target = tmp_path / "valuable"
    target.write_text(SECRET)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(HistoryError, match="history_unsafe"):
        store.append(sample(HistorySampler()))
    assert target.read_text() == SECRET


def test_export_revalidates_disk_rows_and_refuses_hot_journal_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history" / "history.sqlite3"
    store = HistoryStore(path, expected_uid=effective_uid())
    row = sample(HistorySampler())
    row["timestamp"] = int(time.time())
    store.append(row)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE samples SET source=?", (SECRET,))
    result = read_history(path=path, expected_uid=effective_uid())
    assert result["error_code"] == "history_unavailable"
    assert SECRET not in json.dumps(result)
    journal = path.with_name(path.name + "-journal")
    journal.write_bytes(b"unrecovered journal")
    journal.chmod(0o600)
    before = {item.name: item.read_bytes() for item in path.parent.iterdir()}
    assert (
        read_history(path=path, expected_uid=effective_uid())["error_code"] == "history_unavailable"
    )
    assert before == {item.name: item.read_bytes() for item in path.parent.iterdir()}


def test_collector_independent_thread_persists_without_requests_and_stays_fail_open(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RecordingStore:
        def __init__(self):
            self.rows = []
            self.enough = threading.Event()

        def append(self, row):
            self.rows.append(row)
            if len(self.rows) >= 2:
                self.enough.set()

    store = RecordingStore()
    collector = HistoryCollector(
        store, metrics_reader=fixture, active_reader=lambda: True, interval=0.01
    )
    collector.start()
    try:
        assert store.enough.wait(timeout=1)
    finally:
        collector.close()
    assert len(store.rows) >= 2
    assert all(row["source"] == "LIVE" for row in store.rows)

    def fail():
        raise OSError(SECRET)

    actual_store = HistoryStore(
        tmp_path / "history" / "history.sqlite3", expected_uid=effective_uid()
    )
    broken_metrics = HistoryCollector(actual_store, metrics_reader=fail, active_reader=lambda: True)
    broken_metrics.collect_once()
    result = read_history(path=actual_store.path, expected_uid=effective_uid())
    assert result["rows"][0]["error_code"] == "metrics_unavailable"
    actual_store.path.write_bytes(b"not sqlite")
    broken_metrics.collect_once()
    broken_metrics.collect_once()
    output = capsys.readouterr()
    assert output.err.count("relay_history_collection_failed") == 1
    assert SECRET not in output.err
    assert SECRET not in output.out


def test_loopback_read_absolute_deadline_size_bound_and_redirect_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self, payload):
            self.payload = payload
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            assert 0 < timeout <= 2

        def connect(self, endpoint):
            assert endpoint == ("127.0.0.1", 9998)

        def sendall(self, payload):
            assert payload.startswith(b"GET /metrics HTTP/1.0")
            assert SECRET.encode() not in payload

        def recv(self, limit):
            self.calls += 1
            part, self.payload = self.payload[:limit], self.payload[limit:]
            return part

    raw = fixture().encode()
    sock = FakeSocket(
        b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw
    )
    monkeypatch.setattr("relay_agent.history.socket.socket", lambda *_args: sock)
    assert read_loopback_metrics() == fixture()
    for response in (
        b"HTTP/1.0 302 Found\r\nLocation: https://example/" + SECRET.encode() + b"\r\n\r\n",
        b"HTTP/1.0 200 OK\r\nContent-Length: 9999\r\n\r\nshort",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"HTTP/1.0 200 OK\r\n\r\n" + b"x" * (MAX_METRICS_BYTES + 20_000),
    ):
        sock = FakeSocket(response)
        with pytest.raises(HistoryError):
            read_loopback_metrics()
    sock = FakeSocket(b"x" * 100_000)
    times = iter((100.0, 100.5, 101.0, 102.1))
    monkeypatch.setattr("relay_agent.history.time.monotonic", lambda: next(times))
    with pytest.raises(HistoryError, match="metrics_unavailable"):
        read_loopback_metrics()
    assert sock.calls == 1


def test_broker_lifecycle_starts_history_before_serving_and_closes_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import relay_agent.broker as broker_module

    events = []

    class FakeCollector:
        def start(self):
            events.append("collector_started")

        def close(self):
            events.append("collector_closed")

    def serve(*_args, **_kwargs):
        events.append("broker_served")

    monkeypatch.setattr(broker_module.sys, "argv", ["broker-entry.py"])
    monkeypatch.setitem(
        broker_module.sys.modules,
        "pwd",
        SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=1000)),
    )
    monkeypatch.setattr(broker_module, "effective_uid", lambda: 0)
    monkeypatch.setattr(broker_module, "_enable_child_subreaper", lambda: None)
    monkeypatch.setattr(broker_module, "_systemd_listener", lambda: nullcontext())
    monkeypatch.setattr(broker_module, "RelayBroker", object)
    monkeypatch.setattr(broker_module, "HistoryCollector", FakeCollector)
    monkeypatch.setattr(broker_module, "serve", serve)
    assert broker_module.main() == 0
    assert events == ["collector_started", "broker_served", "collector_closed"]

    def broken_collector():
        raise OSError(SECRET)

    events.clear()
    monkeypatch.setattr(broker_module, "HistoryCollector", broken_collector)
    assert broker_module.main() == 0
    assert events == ["broker_served"]


def test_database_hard_size_limit_and_oversize_file_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import relay_agent.history as history_module

    path = tmp_path / "history" / "history.sqlite3"
    monkeypatch.setattr(history_module, "MAX_DATABASE_BYTES", 16 * 1024)
    store = HistoryStore(path, expected_uid=effective_uid())
    sampler = HistorySampler()
    with pytest.raises(sqlite3.DatabaseError, match="full"):
        for tick in range(1000):
            store.append(sample(sampler, value=1000 + tick, mono=100 + tick))
    assert path.stat().st_size <= 16 * 1024
    before = path.stat().st_size
    with path.open("r+b") as stream:
        stream.truncate(16 * 1024 + 1)
    with pytest.raises(HistoryError, match="history_unsafe"):
        store.append(sample(sampler))
    assert path.stat().st_size == 16 * 1024 + 1
    assert before <= 16 * 1024


@pytest.mark.skipif(os.name != "posix", reason="Unix flock semantics")
def test_history_lock_is_bounded_and_independent_from_control_lock(tmp_path: Path) -> None:
    import fcntl

    path = tmp_path / "history" / "history.sqlite3"
    store = HistoryStore(path, expected_uid=effective_uid())
    store.append(sample(HistorySampler()))
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(HistoryError, match="history_busy"):
            store.append(sample(HistorySampler()))
        assert time.monotonic() - started < 1
        assert (
            read_history(path=path, expected_uid=effective_uid())["error_code"]
            == "history_unavailable"
        )
    finally:
        os.close(fd)
    store.append(sample(HistorySampler()))
