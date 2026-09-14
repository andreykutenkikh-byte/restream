"""Offline diagnostic security and counts without media or network subprocesses."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNNER = (
    Path(__file__).resolve().parents[2] / "deploy/moblin-relay/test-native-reader-postmortem.py"
)


@pytest.fixture
def api():
    return runpy.run_path(str(RUNNER), run_name="_offline_reader_postmortem")


def report(partial=b"FLV" + b"\0" * 20):
    pipe = {"bytes": 17, "reads": 2, "last_ns": 12, "handler_max_ns": 4, "eof": True}
    return {
        "version": 1,
        "segment": 4,
        "outcome": 2,
        "returncode": -9,
        "reaped": True,
        "source": {"source_sha": "a" * 40},
        "events": [[1, 6, 0], [2, 5, -9], [3, 12, 0]],
        "events_dropped": 0,
        "progress": [[1, 7, 1000]],
        "progress_dropped": 0,
        "pipes": [pipe, pipe.copy()],
        "cgroup_before": {"cpu_max": [None, 100000]},
        "cgroup_after": {"usage_usec": 25},
        "observer": [[-1, 0, 511, 1, 1, 1, 20, 20, 20, 30]],
        "capture_observer": [[1, True, 30]],
        "reader": {"reader_frames": 7},
        "binaries": {"ffmpeg": {"bytes": 100, "sha256": "b" * 64}, "mediamtx": None},
        "artifact": {
            "state": 1,
            "bytes": len(partial),
            "sha256": hashlib.sha256(partial).hexdigest(),
            "forced": True,
            "incomplete": True,
            "validator": 0,
        },
    }


@pytest.fixture
def staged(api, monkeypatch, tmp_path):
    scope = api["analyze"].__globals__
    stage = tmp_path / "private-stage"
    stage.mkdir()
    monkeypatch.setitem(scope, "STAGE", stage)
    monkeypatch.setattr(
        scope["os"], "O_NOFOLLOW", getattr(scope["os"], "O_NOFOLLOW", 0), raising=False
    )
    original = scope["check_metadata"]

    def local_permissions(info, *, directory=False, limit=scope["REPORT_LIMIT"]):
        # Windows fixtures have no Unix uid/mode; security metadata has separate tests below.
        converted = SimpleNamespace(
            st_mode=stat.S_IFMT(info.st_mode) | (0o700 if directory else 0o600),
            st_uid=0,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
        )
        original(converted, directory=directory, limit=limit)

    monkeypatch.setitem(scope, "check_metadata", local_permissions)
    monkeypatch.setattr(scope["time"], "time", lambda: 1100)
    marker = {
        "version": 1,
        "purpose": "synthetic-stuck-live-reader",
        "created_epoch": 1000,
        "expires_epoch": 2800,
    }
    (stage / "marker.json").write_text(json.dumps(marker))
    (stage / "report.json").write_text(json.dumps(report()))
    (stage / "partial.flv").write_bytes(b"FLV" + b"\0" * 20)
    (stage / "helper.py").write_text("# fixture")
    (stage / "postmortem.py").write_text("# fixture")
    return scope, stage


@pytest.mark.parametrize(
    "changes",
    [
        {"st_uid": 1000},
        {"st_mode": stat.S_IFREG | 0o644},
        {"st_nlink": 2},
        {"st_mode": stat.S_IFLNK | 0o600},
        {"st_mode": stat.S_IFIFO | 0o600},
        {"st_size": 513 * 1024},
        {"st_size": -1},
    ],
)
def test_private_files_reject_untrusted_metadata(api, changes):
    info = {"st_uid": 0, "st_mode": stat.S_IFREG | 0o600, "st_nlink": 1, "st_size": 10}
    with pytest.raises(ValueError, match="UNSAFE_PATH"):
        api["check_metadata"](SimpleNamespace(**(info | changes)))


@pytest.mark.parametrize("mode", [stat.S_IFDIR | 0o755, stat.S_IFLNK | 0o700, stat.S_IFREG | 0o700])
def test_stage_requires_root_private_directory(api, mode):
    with pytest.raises(ValueError):
        api["check_metadata"](SimpleNamespace(st_mode=mode, st_uid=0), directory=True)


def test_final_counts_are_separate_from_progress_and_acceptance(staged, monkeypatch):
    scope, stage = staged
    calls = []

    def probe(descriptor, field, deadline):
        assert descriptor >= 0 and deadline > 0
        calls.append(field)
        return {"state": "KNOWN", "value": 19 if field == "nb_read_packets" else 13}

    monkeypatch.setitem(scope, "probe_count", probe)
    result = scope["analyze"]()
    assert result["last_progress_counter"]["value"] == 7
    assert result["actual_video_packets"]["value"] == 19
    assert result["actually_decoded_frames"]["value"] == 13
    assert result["full_original_validator"] == {"state": "NOT_RUN", "reason": "reader_failed"}
    assert result["original_strict_result"] == "FAIL"
    assert calls == ["nb_read_packets", "nb_read_frames"]
    assert result["reader_evidence"]["events"] == report()["events"]
    assert result["reader_evidence"]["observer"] == report()["observer"]
    assert result["private_stage_removed"] and not stage.exists()
    assert "PASS" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["absent", "corrupt", "oversize", "report", "expired", "future"])
def test_unusable_evidence_is_unknown_and_private_files_are_removed(staged, monkeypatch, damage):
    scope, stage = staged

    def prohibited(*_args):
        pytest.fail("untrusted partial must not reach the decoder")

    monkeypatch.setitem(scope, "probe_count", prohibited)
    if damage == "absent":
        (stage / "partial.flv").unlink()
    elif damage == "corrupt":
        (stage / "partial.flv").write_bytes(b"corrupt media")
    elif damage == "oversize":
        monkeypatch.setitem(scope, "PARTIAL_LIMIT", 16)
    elif damage == "report":
        (stage / "report.json").write_text('{"PRIVATE":"rtmp://secret"')
    else:
        monkeypatch.setattr(scope["time"], "time", lambda: 2801 if damage == "expired" else 999)
    result = scope["analyze"]()
    assert result["actual_video_packets"]["state"] == "UNKNOWN"
    assert result["actually_decoded_frames"]["state"] == "UNKNOWN"
    expected = "UNKNOWN" if damage in {"report", "expired", "future"} else "NOT_RUN"
    assert result["full_original_validator"]["state"] == expected
    assert result["private_stage_removed"] and not stage.exists()
    assert "PRIVATE" not in json.dumps(result) and "secret" not in json.dumps(result)


def test_decoder_failure_does_not_erase_packet_measurement(staged, monkeypatch):
    scope, _stage = staged
    monkeypatch.setitem(
        scope,
        "probe_count",
        lambda _fd, field, _deadline: (
            {"state": "KNOWN", "value": 19}
            if field == "nb_read_packets"
            else scope["unknown"]("TIME_LIMIT")
        ),
    )
    result = scope["analyze"]()
    assert result["actual_video_packets"]["value"] == 19
    assert result["actually_decoded_frames"] == scope["unknown"]("TIME_LIMIT")
    assert result["original_strict_result"] == "FAIL" and result["private_stage_removed"]


def test_unrelated_files_are_never_deleted(staged):
    scope, stage = staged
    (stage / "keep.txt").write_text("unrelated")
    (stage / "partial.flv").unlink()
    result = scope["analyze"]()
    assert not result["private_stage_removed"]
    assert [path.name for path in stage.iterdir()] == ["keep.txt"]


def test_symlink_is_rejected_and_target_preserved(staged, tmp_path):
    scope, stage = staged
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE")
    (stage / "partial.flv").unlink()
    try:
        (stage / "partial.flv").symlink_to(outside)
    except OSError:
        pytest.skip("host does not grant symlink creation")
    result = scope["analyze"]()
    assert result["actual_video_packets"]["state"] == "UNKNOWN"
    assert outside.read_bytes() == b"PRIVATE"
    assert not result["private_stage_removed"]


@pytest.mark.parametrize(
    "update",
    [
        {"outcome": 0},
        {"reaped": False},
        {"events": [[1, 11, 0]]},
        {"progress": [[1, "PRIVATE", 2]]},
        {"pipes": []},
        {"observer": [[1, 2]]},
        {"events": [[1, 12, 0]] * 257},
        {"version": True},
    ],
)
def test_bad_report_cannot_assert_reaped_original_failure(api, update):
    with pytest.raises(ValueError):
        api["safe_report"](report() | update)


def test_report_projection_drops_all_nonallowlisted_text(api):
    value = report() | {"stderr": "PRIVATE", "command": "rtmp://PRIVATE"}
    value["source"]["source_tree"] = "PRIVATE"
    value["cgroup_before"]["url"] = "PRIVATE"
    value["reader"]["reader_media_seconds"] = float("inf")
    value["artifact"]["raw"] = "PRIVATE"
    encoded = json.dumps(api["safe_report"](value), allow_nan=False)
    assert "PRIVATE" not in encoded and "Infinity" not in encoded
    assert len(encoded) < 1024 * 1024


@pytest.mark.parametrize("reached", [True, False])
def test_target_success_or_not_reached_never_invents_main_failure(staged, monkeypatch, reached):
    scope, stage = staged
    if reached:
        value = report() | {"outcome": 1, "returncode": 0, "events": [[1, 5, 0], [2, 11, 0]]}
        value["artifact"].update(state=0, bytes=0, sha256=None)
        (stage / "report.json").write_text(json.dumps(value))
    else:
        (stage / "report.json").unlink()

    def prohibited(*_args):
        pytest.fail("successful or absent reader must not trigger failed-media analysis")

    monkeypatch.setitem(scope, "probe_count", prohibited)
    result = scope["analyze"]()
    assert result["original_strict_result"] == ("READER_COMPLETED" if reached else "UNKNOWN")
    assert result["full_original_validator"] == scope["unknown"]("MAIN_RESULT_SEPARATE")
    assert ("reader_evidence" in result) is reached
    assert result["private_stage_removed"]


@pytest.mark.parametrize("failure", ["timeout", "overflow", "exit", "bad_json", "na", "ok"])
def test_probe_bound_stop_reap_and_safe_output(api, monkeypatch, failure):
    scope = api["probe_count"].__globals__
    now = SimpleNamespace(value=0.0)
    signals, waits, commands = [], [], []
    payload = {"streams": [{"nb_read_frames": "13" if failure != "na" else "N/A"}]}
    raw = json.dumps(payload).encode()
    if failure == "overflow":
        raw = b"PRIVATE" * 100000
    elif failure == "bad_json":
        raw = b"PRIVATE invalid json"
    child = SimpleNamespace(
        stdout=io.BytesIO(raw),
        pid=12345,
        returncode=None if failure == "timeout" else int(failure == "exit"),
    )

    def wait(*, timeout):
        waits.append(timeout)
        now.value += 1
        if child.returncode is None:
            raise subprocess.TimeoutExpired("private", timeout)
        return child.returncode

    def killpg(pid, sig):
        signals.append((pid, sig))
        child.returncode = -9

    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["pass_fds"] == (42,) and kwargs["start_new_session"]
        return child

    child.poll, child.wait = lambda: child.returncode, wait
    monkeypatch.setattr(scope["subprocess"], "Popen", popen)
    monkeypatch.setattr(scope["os"], "lseek", lambda *_: 0)
    monkeypatch.setattr(scope["os"], "killpg", killpg, raising=False)
    monkeypatch.setattr(scope["signal"], "SIGKILL", 9, raising=False)
    monkeypatch.setattr(scope["time"], "monotonic", lambda: now.value)
    result = scope["probe_count"](42, "nb_read_frames", 2)
    assert result["state"] == ("KNOWN" if failure == "ok" else "UNKNOWN")
    assert signals and waits and child.returncode is not None and child.stdout.closed
    assert "-count_frames" in commands[0] and "/proc/self/fd/42" in commands[0]
    assert "file" in commands[0] and "flv" in commands[0]
    assert "PRIVATE" not in json.dumps(result)
