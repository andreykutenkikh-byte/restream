from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import stat
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

HELPER = (
    Path(__file__).resolve().parents[2] / "deploy/moblin-relay/test-native-startup-normalizer.py"
)


@pytest.fixture
def helper():
    return runpy.run_path(str(HELPER), run_name="_startup_wrapper_test")


def metadata(**changes):
    return SimpleNamespace(
        **(
            {
                "st_mode": stat.S_IFREG | 0o600,
                "st_uid": 0,
                "st_nlink": 1,
                "st_dev": 1,
                "st_ino": 2,
                "st_size": 4,
            }
            | changes
        )
    )


@pytest.fixture
def trusted_reader(helper, monkeypatch):
    before, after, opened, closed = metadata(), metadata(), [], []
    fake_os = SimpleNamespace(
        O_RDONLY=0,
        O_NOFOLLOW=1,
        O_CLOEXEC=2,
        O_NONBLOCK=4,
        open=lambda path, flags: opened.append(flags) or 9,
        fstat=lambda fd: after,
        fdopen=lambda fd, mode, closefd: io.BytesIO(b"data"),
        close=closed.append,
    )
    monkeypatch.setitem(helper["trusted_file"].__globals__, "os", fake_os)
    path = SimpleNamespace(lstat=lambda: before)
    return path, before, after, opened, closed


def test_trusted_file_is_bounded_no_follow_nonblocking_and_always_closed(helper, trusted_reader):
    path, _, _, opened, closed = trusted_reader
    assert helper["trusted_file"](path, 0o600, 4) == b"data"
    assert opened == [7]
    assert closed == [9]


@pytest.mark.parametrize(
    "changes",
    [
        {"st_mode": stat.S_IFLNK | 0o600},
        {"st_mode": stat.S_IFIFO | 0o600},
        {"st_mode": stat.S_IFREG | 0o644},
        {"st_uid": 1},
        {"st_nlink": 2},
        {"st_size": 5},
        {"st_size": -1},
    ],
)
def test_trusted_file_refuses_unsafe_metadata_before_open(helper, trusted_reader, changes):
    path, before, _, opened, _ = trusted_reader
    vars(before).update(changes)
    with pytest.raises(ValueError, match="FILE_INVALID"):
        helper["trusted_file"](path, 0o600, 4)
    assert not opened


@pytest.mark.parametrize("field", ["st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size"])
def test_trusted_file_refuses_post_open_substitution(helper, trusted_reader, field):
    path, _, after, _, closed = trusted_reader
    setattr(after, field, getattr(after, field) + 1)
    with pytest.raises(ValueError, match="FILE_CHANGED"):
        helper["trusted_file"](path, 0o600, 4)
    assert closed == [9]


@pytest.fixture
def stage_fixture(helper, monkeypatch):
    state = {
        "metadata": metadata(st_mode=stat.S_IFDIR | 0o700),
        "resolved": None,
        "uid": 0,
        "source": b"source",
        "wrapper": b"wrapper",
    }

    class StagePath(PurePosixPath):
        def lstat(self):
            return state["metadata"]

        def resolve(self, strict=False):
            assert strict
            return state["resolved"] or self

    wrapper = StagePath("/tmp/adojapan-ci-startup-abcdefgh/wrapper.py")  # noqa: S108 - pure fake path
    manifest = {
        "version": 1,
        "purpose": helper["PURPOSE"],
        "normalizer_sha256": hashlib.sha256(state["source"]).hexdigest(),
        "wrapper_sha256": hashlib.sha256(state["wrapper"]).hexdigest(),
    }
    state["manifest"] = manifest
    opened = []

    def trusted(path, mode, maximum):
        opened.append((path.name, mode, maximum))
        if path.name == "manifest.json":
            return state.get("raw_manifest", json.dumps(manifest).encode())
        return state["wrapper" if path.name == "wrapper.py" else "source"]

    api = helper["load_stage"].__globals__
    monkeypatch.setitem(api, "os", SimpleNamespace(geteuid=lambda: state["uid"]))
    monkeypatch.setitem(api, "trusted_file", trusted)
    return wrapper, state, opened


def test_stage_exact_private_identity_and_source_hashes(helper, stage_fixture):
    wrapper, state, opened = stage_fixture
    assert helper["load_stage"](wrapper) == (wrapper.parent, state["source"])
    assert [entry[:2] for entry in opened] == [
        ("manifest.json", 0o600),
        ("normalizer.py", 0o600),
        ("wrapper.py", 0o755),
    ]


@pytest.mark.parametrize(
    "change", ["mode", "symlink", "owner", "resolved", "uid", "source", "wrapper"]
)
def test_stage_refuses_unsafe_identity_or_changed_source(helper, stage_fixture, change):
    wrapper, state, _ = stage_fixture
    if change == "mode":
        state["metadata"].st_mode = stat.S_IFDIR | 0o755
    elif change == "symlink":
        state["metadata"].st_mode = stat.S_IFLNK | 0o700
    elif change == "owner":
        state["metadata"].st_uid = 1
    elif change == "resolved":
        state["resolved"] = PurePosixPath("/elsewhere")
    elif change == "uid":
        state["uid"] = 1
    else:
        state[change] = b"changed"
    with pytest.raises(ValueError, match="CI_DIAGNOSTIC_(STAGE|PIN)"):
        helper["load_stage"](wrapper)


@pytest.mark.parametrize("change", ["version", "purpose", "extra", "pin", "duplicate"])
def test_manifest_rejects_shape_pin_and_duplicate_keys(helper, stage_fixture, change):
    wrapper, state, _ = stage_fixture
    if change == "duplicate":
        state["raw_manifest"] = b'{"version":1,"version":1}'
    elif change == "pin":
        state["manifest"]["normalizer_sha256"] = "not-a-hash"
    else:
        state["manifest"][change] = True
    with pytest.raises(ValueError, match="CI_DIAGNOSTIC_(MANIFEST|PIN|DUPLICATE)"):
        helper["load_stage"](wrapper)


def test_parser_exports_only_fixed_events_and_bounded_numbers(helper):
    parser = helper["Milestones"](1.0, lambda: 1.125)
    parser.feed(b"Opening an input file: rts" + b"p://secret:password@127.0.0.1\n")
    parser.feed(b"Input #0, rtsp, fake private URL\n")
    parser.feed(b"nal_unit_type: 5(IDR), nal_ref_idc: 3\n")
    assert parser.record["events"]["input_open"] == {"count": 1, "first_ms": 125, "last_ms": 125}
    assert parser.record["events"]["input_info"]["count"] == 1
    assert parser.record["events"]["nal_idr"]["count"] == 1
    encoded = json.dumps(parser.record)
    assert not any(value in encoded for value in ("secret", "password", "127.0.0.1", "private URL"))
    assert len(encoded) < helper["MAX_RECORD_BYTES"]
    assert set(parser.record["events"]) == set(helper["EVENTS"])


def test_parser_overlong_record_discards_suffix_until_lf(helper):
    parser = helper["Milestones"](0, lambda: 1)
    parser.feed(b"x" * 2049)
    parser.feed(b"\rInput #0, rtsp, secret\n")
    assert parser.record["line_cap_exceeded"]
    assert parser.record["events"]["input_info"]["count"] == 0
    parser.feed(b"Input #0, rtsp, next\n")
    assert parser.record["events"]["input_info"]["count"] == 1
    assert len(parser.pending) <= 2048


def test_parser_byte_cap_stops_parsing_but_drain_reaches_eof(helper, monkeypatch):
    parser = helper["Milestones"](0, lambda: 1)
    parser.feed(b"x" * (helper["MAX_PARSE_BYTES"] + 1))
    chunks = iter([b"Input #0, rtsp, secret\n", b""])
    reads = []

    def read(fd, count):
        reads.append((fd, count))
        return next(chunks)

    monkeypatch.setitem(helper["Milestones"].drain.__globals__, "os", SimpleNamespace(read=read))
    parser.drain(SimpleNamespace(fileno=lambda: 9))
    assert len(reads) == 2
    assert parser.record["byte_cap_exceeded"] and parser.record["eof"]
    assert parser.record["events"]["input_info"]["count"] == 0


def test_each_drain_has_fixed_nonblocking_work_budget(helper, monkeypatch):
    parser = helper["Milestones"](0, lambda: 1)
    requested = []

    def read(fd, count):
        requested.append(count)
        return b"x" * count

    monkeypatch.setitem(helper["Milestones"].drain.__globals__, "os", SimpleNamespace(read=read))
    parser.drain(SimpleNamespace(fileno=lambda: 9))
    assert sum(requested) == helper["MAX_DRAIN_BYTES"] == 65536
    assert parser.record["drain_cap_reached"]
    assert not parser.record["reader_error"]


@pytest.mark.parametrize(
    "now,field", [(float("nan"), "clock_invalid"), (-1, "clock_invalid"), (21, "outside_window")]
)
def test_parser_invalid_or_late_clock_does_not_create_event(helper, now, field):
    parser = helper["Milestones"](0, lambda: now)
    parser.feed(b"Input #0, rtsp, secret\n")
    assert parser.record[field]
    assert not parser.record["events"]["input_info"]["count"]


def test_parser_event_count_saturates(helper):
    parser = helper["Milestones"](0, lambda: 1)
    for _ in range(helper["MAX_EVENT_COUNT"] + 1):
        parser.line(b"All info found\n")
    assert parser.record["events"]["info_complete"]["count"] == 4096
    assert parser.record["event_cap_exceeded"]


@pytest.fixture
def capture_fixture(helper, monkeypatch):
    calls, writes, stops, samples, state_events, restart_events = [], [], [], [], [], []

    class Pipe:
        closed = False

        def fileno(self):
            return 9

        def close(self):
            self.closed = True

    class Child:
        def __init__(self):
            self.stdout, self.stderr, self.returncode = Pipe(), Pipe(), None

        def poll(self):
            return self.returncode

    def popen(argv, **kwargs):
        child = Child()
        calls.append((list(argv), kwargs, child))
        return child

    class Progress:
        def __init__(self, pipe, now):
            self.pipe = pipe

        def sample(self, now):
            samples.append(now)
            return "original-progress"

        def close(self):
            self.pipe.close()

    def stop(child, *, force=False):
        stops.append((child, force))
        child.returncode = 0

    expected = [
        "/usr/bin/ffmpeg",
        "-loglevel",
        "error",
        "-i",
        "rtsp://127.0.0.1:1/iphone-live",
        "-c:v",
        "copy",
        "-f",
        "flv",
        "rtmp://127.0.0.1:2/live-normalized",
    ]
    original_subprocess = SimpleNamespace(
        Popen=popen, PIPE=-1, DEVNULL=-3, SubprocessError=subprocess.SubprocessError
    )
    api = {
        "subprocess": original_subprocess,
        "VideoProgress": Progress,
        "stop_child": stop,
        "emit_restart_reason": restart_events.append,
        "emit_state_event": state_events.append,
        "build_ffmpeg_argv": lambda left, right: list(expected),
        "validated_port": int,
        "SAFE_RTSP_PORT_ENV": "DIAGNOSTIC_TEST_RTSP",
        "SAFE_RTMP_PORT_ENV": "DIAGNOSTIC_TEST_RTMP",
        "RESTART_REASON_OUTPUT_START_TIMEOUT": "timeout",
        "RESTART_REASON_CHILD_EXIT": "exit",
        "STATE_EVENT_BRIDGE_ACTIVE": "active",
    }
    monkeypatch.setenv("DIAGNOSTIC_TEST_RTSP", "1")
    monkeypatch.setenv("DIAGNOSTIC_TEST_RTMP", "2")
    blocking = []

    def read(fd, count):
        raise BlockingIOError

    monkeypatch.setitem(
        helper["install_capture"].__globals__,
        "os",
        SimpleNamespace(
            environ=os.environ,
            set_blocking=lambda fd, value: blocking.append((fd, value)),
            read=read,
        ),
    )
    monkeypatch.setitem(
        helper["install_capture"].__globals__,
        "atomic_record",
        lambda stage, record: writes.append(json.loads(json.dumps(record))),
    )
    finish = helper["install_capture"](api, PurePosixPath("/private"), clock=lambda: 1.0)
    kwargs = {
        "stdin": -3,
        "stdout": -1,
        "env": {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        "close_fds": True,
        "preexec_fn": lambda: None,
    }
    return SimpleNamespace(
        api=api,
        argv=expected,
        kwargs=kwargs,
        calls=calls,
        writes=writes,
        finish=finish,
        stops=stops,
        samples=samples,
        blocking=blocking,
        original_subprocess=original_subprocess,
        state_events=state_events,
        restart_events=restart_events,
    )


def test_capture_changes_only_first_child_log_level_and_stderr(helper, capture_fixture):
    fixture = capture_fixture
    kwargs = fixture.kwargs
    child = fixture.api["subprocess"].Popen(fixture.argv, **kwargs)
    actual, actual_kwargs, _ = fixture.calls[0]
    expected = list(fixture.argv)
    expected[2] = "debug"
    assert actual == expected and actual_kwargs == kwargs | {"stderr": -1}
    assert fixture.blocking == [(9, False)]
    assert fixture.original_subprocess.Popen is not fixture.api["subprocess"].Popen
    progress = fixture.api["VideoProgress"](child.stdout, 1.0)
    assert progress.sample(1.1) == "original-progress"
    fixture.api["emit_restart_reason"]("timeout")
    fixture.api["stop_child"](child, force=True)
    assert fixture.stops == [(child, True)] and child.stderr.closed
    assert len(fixture.writes) == 1 and fixture.writes[0]["first_child_timeout"]
    assert fixture.writes[0]["child_stopped"]
    fixture.api["subprocess"].Popen(fixture.argv, **kwargs)
    assert fixture.calls[1][:2] == (fixture.argv, kwargs)
    fixture.api["emit_state_event"]("active")
    fixture.finish()
    assert len(fixture.writes) == 1 and not fixture.writes[0]["first_child_bridge_active"]


def test_first_natural_exit_is_frozen_before_later_retry_evidence(helper, capture_fixture):
    fixture = capture_fixture
    child = fixture.api["subprocess"].Popen(fixture.argv, **fixture.kwargs)
    progress = fixture.api["VideoProgress"](child.stdout, 1.0)
    child.returncode = 1
    progress.close()
    fixture.api["emit_restart_reason"]("exit")
    fixture.api["subprocess"].Popen(fixture.argv, **fixture.kwargs)
    fixture.api["emit_state_event"]("active")
    fixture.api["emit_restart_reason"]("timeout")
    fixture.finish()
    assert len(fixture.writes) == 1
    assert fixture.writes[0]["child_stopped"]
    assert not fixture.writes[0]["first_child_bridge_active"]
    assert not fixture.writes[0]["first_child_timeout"]


@pytest.mark.parametrize("change", ["argv", "stderr", "stdout"])
def test_unexpected_first_child_is_refused_before_spawn(helper, capture_fixture, change):
    fixture = capture_fixture
    argv, kwargs = list(fixture.argv), dict(fixture.kwargs)
    if change == "argv":
        argv.append("unexpected")
    else:
        kwargs[change] = -3
    with pytest.raises(ValueError, match="CHILD_CHANGED"):
        fixture.api["subprocess"].Popen(argv, **kwargs)
    assert not fixture.calls


def test_pipe_setup_failure_reaps_original_child_without_raw_error(
    helper, capture_fixture, monkeypatch
):
    fixture = capture_fixture

    def unavailable(fd, blocking):
        raise OSError("private raw details must not leave the diagnostic")

    monkeypatch.setattr(helper["install_capture"].__globals__["os"], "set_blocking", unavailable)
    with pytest.raises(OSError, match="^CI_DIAGNOSTIC_PIPE_SETUP_FAILED$"):
        fixture.api["subprocess"].Popen(fixture.argv, **fixture.kwargs)
    child = fixture.calls[0][2]
    assert fixture.stops == [(child, True)] and child.stderr.closed
    fixture.finish()
    assert len(fixture.writes) == 1 and fixture.writes[0]["reader_error"]
    assert fixture.writes[0]["child_stopped"]
    assert "private raw details" not in json.dumps(fixture.writes)


def test_spawn_failure_is_recorded_without_intercepting_original_error(helper, capture_fixture):
    fixture = capture_fixture

    def unavailable(argv, **kwargs):
        raise OSError("original spawn failure")

    fixture.original_subprocess.Popen = unavailable
    with pytest.raises(OSError, match="original spawn failure"):
        fixture.api["subprocess"].Popen(fixture.argv, **fixture.kwargs)
    fixture.finish()
    assert fixture.writes[0]["spawn_failed"]
    assert not fixture.writes[0]["first_child_spawned"]
    assert "original spawn failure" not in json.dumps(fixture.writes)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "scope",
        "version",
        "flag",
        "bytes",
        "events",
        "event_extra",
        "count",
        "bool_count",
        "zero_with_time",
        "missing_time",
        "reversed",
        "late",
    ],
)
def test_report_projection_rejects_non_allowlisted_or_invalid_values(helper, change):
    value = helper["Milestones"](0).record
    event = value["events"]["input_info"]
    if change == "extra":
        value["private"] = "private URL"
    elif change == "scope":
        value["scope"] = "private URL" * 4096
    elif change == "version":
        value["version"] = True
    elif change == "flag":
        value["reader_error"] = 1
    elif change == "bytes":
        value["parse_bytes"] = 10**310
    elif change == "events":
        value["events"]["unexpected"] = event.copy()
    elif change == "event_extra":
        event["url"] = "private URL"
    elif change in ("count", "bool_count"):
        event["count"] = 4097 if change == "count" else True
    elif change == "zero_with_time":
        event["first_ms"] = 0
    elif change == "missing_time":
        event["count"] = 1
    elif change == "reversed":
        event.update(count=1, first_ms=2, last_ms=1)
    else:
        event.update(count=1, first_ms=20000, last_ms=20001)
    assert helper["validated_report"](value) is None


def test_report_projection_copies_nested_values_and_worst_bounded_shape_fits(helper):
    value = helper["Milestones"](0).record
    for event in value["events"].values():
        event.update(count=4096, first_ms=19999, last_ms=20000)
    value["parse_bytes"] = helper["MAX_PARSE_BYTES"] + 1
    safe = helper["validated_report"](value)
    assert safe == value and safe is not value and safe["events"] is not value["events"]
    value["events"]["input_info"]["count"] = 1
    assert safe["events"]["input_info"]["count"] == 4096
    assert len(json.dumps(safe, separators=(",", ":")).encode()) <= helper["MAX_RECORD_BYTES"]


def test_capture_claim_is_exclusive_and_existing_claim_is_revalidated(helper, monkeypatch):
    flags, closed, validated = [], [], []
    state = {"exists": False}

    def claim(path, mode, permission):
        flags.append((mode, permission))
        if state["exists"]:
            raise FileExistsError
        return 9

    fake_os = SimpleNamespace(
        O_WRONLY=1, O_CREAT=2, O_EXCL=4, O_NOFOLLOW=8, O_CLOEXEC=16, open=claim, close=closed.append
    )
    globals_ = helper["claim_capture"].__globals__
    monkeypatch.setitem(globals_, "os", fake_os)
    monkeypatch.setitem(globals_, "trusted_file", lambda *args: validated.append(args) or b"")
    stage = PurePosixPath("/private")
    assert helper["claim_capture"](stage) is True
    assert flags == [(31, 0o600)] and closed == [9]
    state["exists"] = True
    assert helper["claim_capture"](stage) is False
    assert validated == [(stage / "normalizer-capture.claim", 0o600, 0)]


@pytest.mark.parametrize("supervisor,claim", [(False, False), (True, False), (True, True)])
def test_delegate_keeps_wrapper_reexec_identity_and_claims_only_supervisor(
    helper, monkeypatch, supervisor, claim
):
    source = b'SUPERVISOR_ARGUMENT="--supervisor"\ndef main():\n return __file__\n'
    stage = PurePosixPath("/private")
    wrapper = stage / "wrapper.py"
    actions = []
    globals_ = helper["delegate"].__globals__
    monkeypatch.setitem(
        globals_,
        "sys",
        SimpleNamespace(argv=[str(wrapper)] + (["--supervisor"] if supervisor else [])),
    )
    monkeypatch.setitem(globals_, "claim_capture", lambda path: actions.append("claim") or claim)
    monkeypatch.setitem(
        globals_,
        "install_capture",
        lambda api, path: actions.append("install") or (lambda: actions.append("finish")),
    )
    assert helper["delegate"](wrapper, stage, source) == str(wrapper)
    assert actions == (
        ["claim", "install", "finish"] if supervisor and claim else ["claim"] if supervisor else []
    )


def test_wrapper_has_no_threads_process_search_or_runtime_timing_patch():
    source = HELPER.read_text(encoding="utf-8")
    assert "import threading" not in source
    assert "killpg" not in source and "pkill" not in source
    assert "OUTPUT_START_TIMEOUT_SECONDS =" not in source
    assert "os.set_blocking(child.stderr.fileno(), False)" in source
    assert '"__file__": str(wrapper)' in source
    assert 'sys.argv[1:] != [api["SUPERVISOR_ARGUMENT"]]' in source
