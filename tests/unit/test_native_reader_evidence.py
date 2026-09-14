"""Opt-in synthetic reader receipts do not modify deadlines or passing oracles."""

from __future__ import annotations

import io
import json
import os
import runpy
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-reader-evidence.py"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    api = load_self_test()
    namespace = runpy.run_path(str(HELPER), run_name="_reader_evidence")
    helper = namespace["install"].__globals__
    stage = tmp_path / "evidence"
    stage.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    # The real helper is Linux/root only. Exercise actual file I/O on Windows
    # while testing Linux ownership/mode rejection separately below.
    if os.name == "nt":
        portable = SimpleNamespace(**vars(os))
        portable.O_NOFOLLOW = 0
        portable.fchmod = lambda *_args: None
        monkeypatch.setitem(helper, "os", portable)

        def owned(path, *, directory=False, source=False):
            info = path.lstat()
            kind = stat.S_ISDIR if directory else stat.S_ISREG
            if not kind(info.st_mode) or (not directory and info.st_nlink != 1):
                raise ValueError("invalid private evidence path")
            return info

        monkeypatch.setitem(helper, "owned", owned)
    monkeypatch.setitem(helper, "cgroup_snapshot", lambda: {"usage_usec": 10})
    namespace["install"](
        api,
        stage,
        {
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "helper_sha256": "c" * 64,
            "self_test_sha256": "d" * 64,
            "ignored": "PRIVATE",
            "url": "rtmp://PRIVATE/key",
        },
    )
    globals_ = api["capture_final_sink_media_segment"].__globals__
    globals_["SELF_TEST_LAST_PROGRESS"] = {"stage": "stuck-live"}
    reader = api["CaptureReaderProgress"](started=time.monotonic())
    path = work / "sink-proof-004.flv"
    reader.begin_capture(path, 4)
    return SimpleNamespace(
        api=api, helper=helper, stage=stage, work=work, reader=reader, path=path, globals=globals_
    )


def invoke(evidence, source, *, timeout=2):
    return evidence.api["run_capture_reader"](
        [sys.executable, "-c", source],
        evidence.reader,
        timeout=timeout,
    )


def report(evidence):
    return json.loads((evidence.stage / "report.json").read_text())


def test_success_delegates_dual_pipes_and_retains_numeric_only(evidence):
    result = invoke(
        evidence,
        "import sys; "
        "sys.stderr.write('PRIVATE'*40000+'\\n'); sys.stderr.flush(); "
        "print('frame=90', flush=True)",
    )
    assert result.returncode == 0 and result.stdout is result.stderr is None
    value = report(evidence)
    assert value["outcome"] == 1 and value["returncode"] == 0 and value["reaped"]
    assert value["progress"][-1][1] == 90
    assert value["pipes"][0]["bytes"] > 0 and value["pipes"][1]["bytes"] in {280001, 280002}
    assert all(row["eof"] for row in value["pipes"])
    assert "PRIVATE" not in json.dumps(value) and "rtmp" not in json.dumps(value)
    assert value["artifact"]["validator"] == 0 and value["artifact"]["state"] == 0
    assert not (evidence.stage / "partial.flv").exists()
    assert [row[1] for row in value["events"]] == [1, 2, 3, 4, 5, 8, 4, 5, 11]
    waits = [row for row in value["events"] if row[1] == 4]
    assert [row[2] for row in waits] == [2_000_000_000, 0]


def test_timeout_reaps_and_progress_90_does_not_supply_success(evidence):
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        invoke(evidence, "import time; print('frame=90',flush=True); time.sleep(10)", timeout=0.3)
    assert caught.value.timeout == 0.3
    value = report(evidence)
    assert value["outcome"] == 2 and value["reaped"]
    assert value["progress"][-1][1] == 90
    assert value["artifact"]["forced"] and value["artifact"]["validator"] == 0
    codes = [row[1] for row in value["events"]]
    assert codes.index(6) < codes.index(9) < codes.index(10) < codes.index(5) < codes.index(11)


@pytest.mark.parametrize("stage,index", [("stall-live", 4), ("stuck-live", 3), ("stuck-live", 5)])
def test_only_exact_target_is_instrumented(evidence, stage, index):
    evidence.globals["SELF_TEST_LAST_PROGRESS"] = {"stage": stage}
    evidence.reader.begin_capture(evidence.path, index)
    result = invoke(evidence, "print('frame=2')")
    assert result.returncode == 0
    assert evidence.reader.snapshot()["reader_frames"] == 2
    assert not (evidence.stage / "report.json").exists()


def test_process_proxy_delegates_untargeted_other_thread_without_receipts(evidence):
    proxy = evidence.globals["subprocess"]
    before = list(evidence.reader.rows)
    child = proxy.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL)
    assert child.wait(timeout=2) == 0
    assert isinstance(child, subprocess.Popen)
    assert list(evidence.reader.rows) == before


def test_nonzero_exit_cannot_be_promoted(evidence):
    assert invoke(evidence, "import sys; print('frame=90'); sys.exit(7)").returncode == 7
    value = report(evidence)
    assert value["outcome"] == 3 and value["returncode"] == 7


def test_small_progress_line_observed_without_4096_bytes_or_exit(evidence):
    result = invoke(
        evidence,
        "import time; print('frame=1',flush=True); time.sleep(0.4); print('frame=90',flush=True)",
    )
    assert result.returncode == 0
    value = report(evidence)
    first, last = value["progress"][0], value["progress"][-1]
    assert first[1] == 1 and last[1] == 90
    assert last[0] - first[0] > 200_000_000


def test_rings_and_inspection_are_bounded_without_stopping_drain(evidence):
    reader = evidence.reader
    for index in range(400):
        reader.observe_line(f"frame={index}".encode(), progress=True)
        reader.event(8)
    stream = io.BytesIO(b"x" * (1024 * 1024 + 100))
    reader.drain(stream, progress=False)
    assert stream.tell() == 1024 * 1024 + 100
    assert len(reader.rows) == len(reader.progress_rows) == 256
    assert reader.dropped and reader.progress_dropped
    assert reader.snapshot()["reader_inspection_limited"]


def stopped_failure(evidence):
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        invoke(evidence, "import time; time.sleep(10)", timeout=0.1)
    evidence.path.write_bytes(b"synthetic incomplete FLV")
    evidence.path.chmod(0o600)
    return caught.value


def test_partial_is_copied_only_after_reap_and_failure_persisted(evidence, monkeypatch):
    error = stopped_failure(evidence)
    original = evidence.helper["atomic_report"]
    observed = []

    def write(stage, value):
        observed.append(
            (value["outcome"], value["artifact"]["state"], (stage / "partial.flv").exists())
        )
        return original(stage, value)

    monkeypatch.setitem(evidence.helper, "atomic_report", write)
    evidence.reader.retain_failure(evidence.path, error)
    value = report(evidence)
    assert observed == [(2, 0, False), (2, 1, True)]
    assert (evidence.stage / "partial.flv").read_bytes() == evidence.path.read_bytes()
    assert value["artifact"]["incomplete"] and value["artifact"]["validator"] == 0
    assert value["artifact"]["bytes"] == evidence.path.stat().st_size
    assert len(value["artifact"]["sha256"]) == 64


@pytest.mark.parametrize("invalid", ["missing", "oversize", "hardlink", "other-path", "not-reaped"])
def test_invalid_partial_never_replaces_failure_or_leaves_copy(evidence, invalid):
    error = stopped_failure(evidence)
    if invalid == "missing":
        evidence.path.unlink()
    elif invalid == "oversize":
        evidence.helper["PARTIAL_LIMIT"] = 1
    elif invalid == "hardlink":
        os.link(evidence.path, evidence.work / "linked.flv")
    elif invalid == "not-reaped":
        evidence.reader.reaped = False
    path = evidence.path if invalid != "other-path" else evidence.work / "other.flv"
    evidence.reader.retain_failure(path, error)
    assert not (evidence.stage / "partial.flv").exists()
    assert report(evidence)["outcome"] == 2


def test_failed_report_prevents_artifact_copy(evidence, monkeypatch):
    error = stopped_failure(evidence)

    def fail(*_args):
        raise OSError("PRIVATE failure")

    monkeypatch.setitem(evidence.helper, "atomic_report", fail)
    evidence.reader.retain_failure(evidence.path, error)
    assert not (evidence.stage / "partial.flv").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("st_uid", 1000),
        ("st_mode", stat.S_IFREG | 0o644),
        ("st_mode", stat.S_IFLNK | 0o600),
        ("st_nlink", 2),
    ],
)
def test_linux_private_file_constraints_fail_closed(field, value):
    namespace = runpy.run_path(str(HELPER))
    metadata = {"st_uid": 0, "st_mode": stat.S_IFREG | 0o600, "st_nlink": 1}
    metadata[field] = value
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(**metadata))
    with pytest.raises(ValueError, match="private evidence path"):
        namespace["owned"](path)


def test_source_globals_receive_subclasses_and_target_reassignment(evidence):
    assert evidence.globals["CaptureReaderProgress"] is evidence.api["CaptureReaderProgress"]
    assert evidence.globals["run_capture_reader"] is evidence.api["run_capture_reader"]
    evidence.globals["SELF_TEST_LAST_PROGRESS"] = {"stage": "crash-death"}
    evidence.reader.begin_capture(evidence.path, 4)
    assert not evidence.reader.active


def test_existing_observer_samples_have_only_local_generations_and_numeric_rows(evidence):
    start = evidence.reader.anchor / 1e9
    samples = []
    for index in range(3):
        samples.append(
            {
                "t": start + index,
                "finished": start + index + 0.1,
                "ingest_ids": ["PRIVATE OLD" if index == 0 else "PRIVATE NEW"],
                "normalized_ids": ["PRIVATE NORMAL"],
                "sink_ids": ["PRIVATE SINK"],
                "live": True,
                "sink_bytes": 100 * index,
                "capture_size": 50 * index,
                "other": "PRIVATE",
                "ingest_bytes": "PRIVATE",
            }
        )
    evidence.reader.observer = SimpleNamespace(lock=threading.Lock(), samples=samples)
    invoke(evidence, "pass")
    rows = report(evidence)["observer"]
    assert [row[3] for row in rows] == [1, 2, 2]
    assert [row[4:6] for row in rows] == [[1, 1]] * 3
    assert rows[-1][6:] == [None, None, 200, 100]
    assert "PRIVATE" not in json.dumps(rows)


def test_read_only_world_readable_partial_is_protected_only_after_reader_stops(evidence):
    error = stopped_failure(evidence)
    evidence.path.chmod(0o644)
    evidence.reader.retain_failure(evidence.path, error)
    assert report(evidence)["artifact"]["state"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(evidence.path.stat().st_mode) == 0o600


def test_source_world_writable_mode_is_rejected():
    namespace = runpy.run_path(str(HELPER))
    path = SimpleNamespace(
        lstat=lambda: SimpleNamespace(
            st_uid=0,
            st_mode=stat.S_IFREG | 0o666,
            st_nlink=1,
        )
    )
    with pytest.raises(ValueError):
        namespace["owned"](path, source=True)


def test_capture_observer_retains_only_existing_numeric_growth(evidence):
    start = evidence.reader.anchor / 1e9
    evidence.reader.capture_observer = SimpleNamespace(
        lock=threading.Lock(),
        samples=[
            {"t": start, "capture_ok": True, "capture_size": 100, "path": "PRIVATE"},
            {"t": start + 0.2, "capture_ok": True, "capture_size": 150},
            {"t": start + 0.3, "capture_ok": True, "capture_size": "PRIVATE"},
        ],
    )
    invoke(evidence, "pass")
    rows = report(evidence)["capture_observer"]
    assert [row[1:] for row in rows] == [[True, 100], [True, 150]]
    assert "PRIVATE" not in json.dumps(rows)


def test_existing_milestones_keep_same_capture_anchor(evidence):
    invoke(
        evidence,
        "import sys; "
        "print('[flv @ 0x1234] Before avformat_find_stream_info() pos: 13 "
        "bytes read:32768 seeks:0 nb_streams:0', file=sys.stderr); "
        "print('[flv @ 0x1234] After avformat_find_stream_info() pos: 50000 "
        "bytes read:65536 seeks:0 frames:75', file=sys.stderr); "
        "print('Input #0, flv, from PRIVATE:', file=sys.stderr); "
        "print('frame=86',flush=True)",
    )
    value = report(evidence)
    assert value["reader"]["reader_frames"] == 86
    assert value["reader"]["reader_probe_start_seconds"] >= 0
    assert (
        value["reader"]["reader_probe_end_seconds"] >= value["reader"]["reader_probe_start_seconds"]
    )
    assert "PRIVATE" not in json.dumps(value)


def test_executable_hashes_are_actual_content_without_paths(evidence):
    executable = evidence.work / "synthetic-executable"
    executable.write_bytes(b"synthetic executable content")
    evidence.globals["FFMPEG"] = executable
    evidence.globals["MEDIAMTX"] = executable
    invoke(evidence, "pass")
    values = report(evidence)["binaries"]
    assert values["ffmpeg"] == values["mediamtx"]
    assert values["ffmpeg"]["bytes"] == executable.stat().st_size
    assert len(values["ffmpeg"]["sha256"]) == 64
    assert str(executable) not in json.dumps(values)


def test_copy_budget_failure_removes_only_new_copy(evidence, monkeypatch):
    error = stopped_failure(evidence)
    clock = iter([0, 3_000_000_000])
    real_clock = time.monotonic_ns
    times = [real_clock()]

    def now():
        # event receipt, then copy deadline, then its first read is overdue.
        if times:
            return times.pop()
        return next(clock)

    monkeypatch.setitem(evidence.helper, "time", SimpleNamespace(monotonic_ns=now))
    evidence.reader.retain_failure(evidence.path, error)
    assert not (evidence.stage / "partial.flv").exists()
    assert report(evidence)["outcome"] == 2
    assert report(evidence)["artifact"]["state"] == 2
    assert evidence.path.exists()


def test_preexisting_artifact_is_never_overwritten_or_deleted(evidence):
    error = stopped_failure(evidence)
    target = evidence.stage / "partial.flv"
    target.write_bytes(b"previous owned evidence")
    evidence.reader.retain_failure(evidence.path, error)
    assert target.read_bytes() == b"previous owned evidence"
    assert report(evidence)["artifact"]["state"] == 2


def test_optional_resource_diagnostic_failure_cannot_prevent_reader_or_replace_timeout(
    evidence, monkeypatch
):
    def fail():
        raise RuntimeError("PRIVATE")

    monkeypatch.setitem(evidence.helper, "cgroup_snapshot", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(evidence, "import time; print('frame=89',flush=True); time.sleep(10)", timeout=0.3)
    value = report(evidence)
    assert value["outcome"] == 2 and value["reaped"]
    assert value["cgroup_before"] == value["cgroup_after"] == {}
    assert value["progress"][-1][1] == 89
    assert "PRIVATE" not in json.dumps(value)
