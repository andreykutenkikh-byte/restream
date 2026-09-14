"""Test-only observation receipts preserve the original supervisor's decisions."""

from __future__ import annotations

import copy
import json
import os
import runpy
import stat
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy/moblin-relay/test-native-startup-normalizer.py"
NORMALIZER = ROOT / "deploy/moblin-relay/moblin-relay-normalize"


@pytest.fixture
def witness(monkeypatch):
    helper = runpy.run_path(str(HELPER), run_name="_timeline_helper")
    original = runpy.run_path(str(NORMALIZER), run_name="_timeline_normalizer")
    api = original["run_supervisor"].__globals__
    clock = [10**9]
    writes, calls, children, metrics = [], [], [], []
    monkeypatch.setitem(
        helper["atomic_timeline_record"].__globals__,
        "atomic_timeline_record",
        lambda stage, value: writes.append(copy.deepcopy(value)),
    )

    class Reader:
        def __init__(self, port, path, parser):
            self.kind = int(path != api["OUTPUT_METRICS_PATH"])
            calls.append(("reader_init", port, path, parser))

        def sample(self):
            calls.append(("sample", self.kind))
            result = metrics.pop(0)
            clock[0] += 200_000_000
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            calls.append(("reader_close", self.kind))

    class OriginalChild:
        def __init__(self, argv, **kwargs):
            calls.append(("spawn", argv, kwargs))
            self.pid = 1234
            self.result = 0
            self.poll_result = None
            self.reader, self.writer = os.pipe()
            self.stdout = os.fdopen(self.reader, "rb", buffering=0)
            children.append(self)

        def poll(self):
            calls.append(("poll",))
            return self.poll_result

        def kill(self):
            calls.append(("kill",))
            clock[0] += 100_000

        def wait(self, *args, **kwargs):
            calls.append(("wait", args, kwargs))
            clock[0] += 1_000_000
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    api["MetricsReader"] = Reader
    api["subprocess"] = SimpleNamespace(
        Popen=OriginalChild,
        TimeoutExpired=subprocess.TimeoutExpired,
        SubprocessError=subprocess.SubprocessError,
        DEVNULL=subprocess.DEVNULL,
        PIPE=subprocess.PIPE,
    )
    api["emit_state_event"] = lambda value: calls.append(("state", value))
    api["emit_restart_reason"] = lambda value: calls.append(("restart", value))
    save = helper["install_timeline"](api, Path("unused-private-stage"), lambda: clock[0])
    try:
        yield SimpleNamespace(
            api=api,
            helper=helper,
            clock=clock,
            writes=writes,
            calls=calls,
            children=children,
            metrics=metrics,
            save=save,
        )
    finally:
        for child in children:
            child.stdout.close()
            os.close(child.writer)


def test_metrics_are_delegated_once_and_identity_is_only_a_generation(witness):
    w = witness
    parser = object()
    reader = w.api["MetricsReader"](12345, w.api["OUTPUT_METRICS_PATH"], parser)
    samples = [
        (True, ("PRIVATE-FIRST-ID", 100)),
        (True, ("PRIVATE-FIRST-ID", 150)),
        (False, None),
        (True, None),
        (True, ("PRIVATE-SECOND-ID", 20)),
    ]
    w.metrics.extend(samples)
    assert [reader.sample() for _ in samples] == samples
    reader.close()
    assert not w.writes  # No per-loop file I/O.
    assert sum(call[0] == "sample" for call in w.calls) == len(samples)
    w.save()
    rows = w.writes[0]["events"]
    assert [row[1] for row in rows] == [1, 2] * 5
    ends = [row[2:] for row in rows if row[1] == 2]
    assert ends == [
        [0, 1, 2, 1, 100, -1],
        [0, 2, 2, 1, 150, 50],
        [0, 3, 0, 1, -1, -1],
        [0, 4, 1, 1, -1, -1],
        [0, 5, 2, 2, 20, -1],
    ]
    assert all(
        end[0] - start[0] == 200_000_000 for start, end in zip(rows[::2], rows[1::2], strict=True)
    )
    assert "PRIVATE" not in json.dumps(w.writes)
    assert w.helper["validated_timeline_report"](w.writes[0]) == w.writes[0]


def test_original_metrics_exception_is_not_swallowed_or_serialized(witness):
    reader = witness.api["MetricsReader"](12345, "private-ingest-path", None)
    error = OSError("private-token-exception")
    witness.metrics.append(error)
    with pytest.raises(OSError) as raised:
        reader.sample()
    assert raised.value is error
    witness.save()
    assert witness.writes[0]["events"][-1][2:] == [1, 1, 3, 0, -1, -1]
    assert "private" not in json.dumps(witness.writes)


def test_spawn_progress_stop_preserve_argv_pipe_wait_and_no_extra_polls(witness):
    w = witness
    argv = ["original-program", "private-argument"]
    kwargs = {"env": {"PRIVATE": "private-secret"}, "stdout": subprocess.PIPE}
    child = w.api["subprocess"].Popen(argv, **kwargs)
    spawn = next(call for call in w.calls if call[0] == "spawn")
    assert spawn[1] is argv and spawn[2] == kwargs
    assert child.pid == 1234 and child.stdout is w.children[0].stdout
    progress = w.api["VideoProgress"](child.stdout, w.clock[0] / 10**9)
    progress.sample(w.clock[0] / 10**9)
    os.write(w.children[0].writer, b"frame=90\nprogress=continue\n")
    w.clock[0] += 100_000_000
    progress.sample(w.clock[0] / 10**9)
    w.clock[0] += 2_500_000_000
    progress.sample(w.clock[0] / 10**9)
    assert progress.stalled(w.clock[0] / 10**9)
    assert not w.writes
    w.api["stop_child"](child, force=True)
    progress.close()
    assert [call[0] for call in w.calls] == ["spawn", "poll", "kill", "wait"]
    assert w.calls[-1][2] == {"timeout": 0.025}
    w.save()
    rows = w.writes[-1]["events"]
    assert [row[2:] for row in rows if row[1] == 3] == [
        [1, 0, -1],
        [1, 90, 0],
        [1, 90, 2_500_000_000],
    ]
    assert [row[1] for row in rows if row[1] in (8, 9, 10)] == [10, 8, 8, 9, 9, 10]
    assert rows[-1][1] == 13
    assert "private" not in json.dumps(w.writes)


@pytest.mark.parametrize(
    "outcome", [subprocess.TimeoutExpired("private", 0.025), OSError("private")]
)
def test_wait_records_confirmation_only_when_original_wait_returns(witness, outcome):
    child = witness.api["subprocess"].Popen([])
    witness.children[0].result = outcome
    with pytest.raises(type(outcome)) as raised:
        child.wait(timeout=0.025)
    assert raised.value is outcome
    witness.save()
    rows = [row for row in witness.writes[0]["events"] if row[1] == 9]
    expected = 2 if isinstance(outcome, subprocess.TimeoutExpired) else 3
    assert [row[2:] for row in rows] == [[1, 0, -9999], [1, expected, -9999]]
    assert "private" not in json.dumps(witness.writes)


def test_timeline_distinguishes_real_growth_from_delayed_watchdog_observation(witness):
    """Same 8s wall delay, different evidence; do not invent missing idle proof."""
    w = witness
    watchdog = w.api["MediaWatchdog"](("private-output-id", 100), 1.0)
    for second in range(2, 8):
        w.clock[0] = second * 10**9
        assert watchdog.observe_output(True, ("private-output-id", 100 + second), second) == (
            True,
            False,
        )
    # No execution occurs for three seconds: the next actual decision rejects.
    w.clock[0] = 10 * 10**9
    assert watchdog.observe_output(True, ("private-output-id", 107), 10.0) == (True, True)
    w.clock[0] = 10_050_000_000
    assert watchdog.observe_output(True, ("private-output-id", 107), 10.05) == (False, False)
    w.save()
    decisions = [row for row in w.writes[0]["events"] if row[1] == 4]
    assert all(row[4:] == [0, 0, 0] for row in decisions[:-2])
    assert decisions[-2][0] - decisions[-3][0] == 3_000_000_000
    assert decisions[-1][4:] == [
        w.helper["TIMELINE_REASONS"].index("output-fallback"),
        3_050_000_000,
        2,
    ]


def test_ring_is_bounded_and_schema_rejects_raw_or_out_of_order_data(witness):
    w = witness
    trace = w.helper["SupervisorTimeline"](Path("unused"), lambda: w.clock[0])
    for _index in range(w.helper["TIMELINE_LIMIT"] + 3):
        w.clock[0] += 1
        trace.add(6, 0)
    trace.save()
    value = w.writes[-1]
    assert value["dropped_events"] == 3 and len(value["events"]) == w.helper["TIMELINE_LIMIT"]
    assert len(json.dumps(value).encode()) < w.helper["TIMELINE_MAX_BYTES"]
    safe = w.helper["validated_timeline_report"](value)
    assert safe is not None and safe["events"] is not value["events"]
    for change in ("secret", "code", "bool", "reverse", "oversized", "shape"):
        invalid = copy.deepcopy(value)
        if change == "secret":
            invalid["key"] = "PRIVATE"
        elif change == "code":
            invalid["events"][0][1] = 999
        elif change == "bool":
            invalid["events"][0][2] = True
        elif change == "reverse":
            invalid["events"][1][0] = invalid["events"][0][0] - 1
        elif change == "oversized":
            invalid["events"][0][0] = 2**64
        else:
            invalid["events"][0].append("PRIVATE")
        assert w.helper["validated_timeline_report"](invalid) is None


def test_bad_clock_and_invalid_numeric_payload_are_reported_without_raw_values(witness):
    w = witness
    trace = w.helper["SupervisorTimeline"](Path("unused"), lambda: w.clock[0])
    trace.add(6, "PRIVATE")
    w.clock[0] -= 1
    trace.add(6, 0)
    trace.save()
    value = w.writes[-1]
    assert value["clock_invalid"] and value["invalid_data"] and value["events"] == []
    assert "PRIVATE" not in json.dumps(value)


def test_real_supervisor_with_timeline_still_stops_audio_growth_at_video_deadline(
    witness,
    monkeypatch,
):
    w = witness
    handlers, counts, frames_sent = {}, [0, 0], []
    reader_base = w.api["MetricsReader"].__mro__[1]

    def sample(reader):
        w.clock[0] += 50_000_000
        counts[reader.kind] += 100
        if w.children and not frames_sent:
            os.write(w.children[0].writer, b"frame=90\nprogress=continue\n")
            frames_sent.append(True)
        identity = "11111111-2222-4333-8444-555555555555" if reader.kind else "private-output"
        return True, (identity, counts[reader.kind])

    def sleep(seconds):
        w.clock[0] += round(seconds * 10**9)
        assert w.clock[0] < 10**10, "original supervisor did not reach its video deadline"

    original_restart = w.api["emit_restart_reason"]

    def restart(reason):
        original_restart(reason)
        handlers[15](15, None)

    monkeypatch.setattr(reader_base, "sample", sample)
    monkeypatch.setitem(
        w.api,
        "time",
        SimpleNamespace(
            monotonic=lambda: w.clock[0] / 10**9,
            sleep=sleep,
        ),
    )
    monkeypatch.setitem(
        w.api,
        "signal",
        SimpleNamespace(
            SIGHUP=1,
            SIGINT=2,
            SIGTERM=15,
            signal=lambda number, handler: handlers.update({number: handler}),
        ),
    )
    monkeypatch.setitem(w.api, "make_parent_death_setup", lambda _pid: None)
    monkeypatch.setitem(w.api, "emit_restart_reason", restart)
    assert (
        w.api["run_supervisor"](
            18554,
            11936,
            19998,
            "11111111-2222-4333-8444-555555555555",
        )
        == 0
    )
    w.save()
    assert [(call[0], call[1]) for call in w.calls if call[0] == "restart"] == [
        ("restart", "video-stalled"),
    ]
    rows = w.writes[-1]["events"]
    videos = [row for row in rows if row[1] == 3]
    assert all(row[3] == 90 for row in videos)
    assert 2_500_000_000 <= videos[-1][4] < 2_700_000_000
    assert sum(row[1] == 7 and row[3] == 1 for row in rows) == 1
    assert sum(row[1] == 9 and row[3] == 1 for row in rows) == 1
    assert w.helper["validated_timeline_report"](w.writes[-1]) is not None


@pytest.mark.parametrize("unsafe", ["mode", "owner", "symlink", "resolved", "uid", "path"])
def test_atomic_timeline_checks_private_stage_before_opening_any_destination(monkeypatch, unsafe):
    helper = runpy.run_path(str(HELPER), run_name="_timeline_guard")
    writes = []
    trace = helper["SupervisorTimeline"](None, lambda: 10**9)
    save_globals = helper["SupervisorTimeline"].save.__globals__
    original_writer = helper["atomic_timeline_record"]
    monkeypatch.setitem(
        save_globals, "atomic_timeline_record", lambda stage, value: writes.append(value)
    )
    trace.save()
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)

    class Stage(PurePosixPath):
        def lstat(self):
            return metadata

        def resolve(self, strict=True):
            return PurePosixPath("/elsewhere") if unsafe == "resolved" else self

    stage = Stage("/tmp/adojapan-ci-startup-abcdefgh" if unsafe != "path" else "/untrusted")  # noqa: S108
    if unsafe == "mode":
        metadata.st_mode = stat.S_IFDIR | 0o755
    elif unsafe == "owner":
        metadata.st_uid = 1000
    elif unsafe == "symlink":
        metadata.st_mode = stat.S_IFLNK | 0o700
    monkeypatch.setitem(
        original_writer.__globals__,
        "os",
        SimpleNamespace(
            geteuid=lambda: 1000 if unsafe == "uid" else 0,
        ),
    )
    with pytest.raises(ValueError, match="CI_TIMELINE_STAGE_INVALID"):
        original_writer(stage, writes[0])
