"""Private numeric FF_FDEBUG_TS observation preserves the original strict oracle."""

from __future__ import annotations

import io
import json
import runpy
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

RUNNER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/test-native-startup.py"


@pytest.fixture
def api():
    return runpy.run_path(str(RUNNER), run_name="_packet_trace_test")


def line(phase="ff_read_packet", stream=0, pts=17, dts=-2, size=100, duration=33, flags=1):
    return (
        f"[flv @ 0x123abc] {phase} stream={stream}, pts={pts}, dts={dts}, "
        f"size={size}, duration={duration}, flags={flags}"
    ).encode()


def sample(api):
    trace = api["ReaderPacketTrace"](clock=lambda: 10.0)
    trace.observe_line(line(), progress=False)
    return trace.finish()


def test_two_exact_source_patterns_keep_only_numeric_fields_and_nopts(api):
    clock = SimpleNamespace(now=10.0)
    trace = api["ReaderPacketTrace"](clock=lambda: clock.now)
    clock.now = 10.25
    trace.observe_line(line(pts="NOPTS", dts=-(2**63)), progress=False)
    clock.now = 10.5
    trace.observe_line(line("read_frame_internal", stream=3, pts=2**63 - 1), progress=False)
    result = trace.finish()
    assert [group["phase"] for group in result["groups"]] == ["demux_receive", "parser_output"]
    assert [group["stream"] for group in result["groups"]] == [0, 3]
    assert result["groups"][0]["rows"] == [
        {
            "wall_ms": 250,
            "pts": None,
            "dts": -(2**63),
            "duration": 33,
            "size": 100,
            "flags": 1,
        }
    ]
    assert result["groups"][1]["rows"][0]["pts"] == 2**63 - 1
    encoded = json.dumps(result)
    assert "123abc" not in encoded and "rtmp" not in encoded and "video" not in encoded
    assert result["timebase"] == "flv-milliseconds"
    assert result["scope"] == "strict-reader-collector-not-network-or-decode"


@pytest.mark.parametrize(
    "bad",
    [
        b"PRIVATE " + line(),
        line() + b" rtmps://PRIVATE/key",
        line().replace(b"0x123abc", b"PRIVATE"),
        line().replace(b"flv", b"rtsp"),
        line().replace(b"ff_read_packet", b"IN delayed"),
        line(pts="PRIVATE"),
        line(pts=2**63),
        line(dts=-(2**63) - 1),
        line(duration=2**63),
        line(size=0),
        line(size=2**31),
        line(flags=2**31),
        line(stream=256),
        line(stream=-1),
        line(pts="nan"),
        line(pts="1.2"),
        line().replace(b"pts=17", b"pts=17\x00PRIVATE"),
        b"x" * 3000,
    ],
)
def test_malformed_or_secret_bearing_lines_are_not_retained(api, bad):
    trace = api["ReaderPacketTrace"](clock=lambda: 10.0)
    trace.observe_line(bad, progress=False)
    trace.observe_line(line(), progress=True)
    assert trace.finish()["groups"] == []
    assert "PRIVATE" not in repr(vars(trace))


def test_first_eight_last_eight_and_four_stream_global_bounds(api):
    clock = SimpleNamespace(now=100.0)
    trace = api["ReaderPacketTrace"](clock=lambda: clock.now)
    for index in range(70):
        clock.now = 100.0 + index / 100
        for stream in range(5):
            for phase in ("ff_read_packet", "read_frame_internal"):
                trace.observe_line(line(phase, stream=stream, pts=index), progress=False)
    result = trace.finish()
    assert len(result["groups"]) == 8
    assert sum(len(group["rows"]) for group in result["groups"]) == 128
    for group in result["groups"]:
        assert group["count"] == 70
        assert [row["pts"] for row in group["rows"]] == list(range(8)) + list(range(62, 70))
        assert group["first_ms"] == 0 and 689 <= group["last_ms"] <= 690
    assert result["flags"]["sampled"] and result["flags"]["streams_limited"]
    assert len(json.dumps(result).encode()) < api["PACKET_TRACE_BYTES"]


@pytest.mark.parametrize("bad_clock", [9.99, float("nan"), float("inf"), 10**310])
def test_invalid_or_regressing_clock_stops_optional_collection(api, bad_clock):
    clock = SimpleNamespace(now=10.0)
    trace = api["ReaderPacketTrace"](clock=lambda: clock.now)
    trace.observe_line(line(), progress=False)
    clock.now = bad_clock
    trace.observe_line(line(), progress=False)
    assert trace.finish()["flags"]["clock_invalid"]


def test_window_and_byte_limits_stop_inspection_and_finish_seals_collection(api):
    clock = SimpleNamespace(now=10.0)
    trace = api["ReaderPacketTrace"](clock=lambda: clock.now)
    trace.observe_line(line(), progress=False)
    clock.now = 30.001
    trace.observe_line(line(), progress=False)
    assert trace.finish()["flags"]["window_limited"]
    bounded = api["ReaderPacketTrace"](clock=lambda: 10.0)
    for _ in range(530):
        bounded.observe_line(b"x" * 1024, progress=False)
    before = bounded.finish()
    bounded.observe_line(line(), progress=False)
    assert bounded.finish() == before
    assert before["flags"]["inspection_limited"] and before["groups"] == []


@pytest.mark.parametrize(
    "path,bad",
    [
        (("scope",), []),
        (("version",), True),
        (("timebase",), "PRIVATE"),
        (("groups", 0, "phase"), {}),
        (("groups", 0, "stream"), True),
        (("groups", 0, "count"), 10**310),
        (("groups", 0, "first_ms"), -1),
        (("groups", 0, "last_ms"), 20001),
        (("flags", "sampled"), "PRIVATE"),
        (("groups", 0, "rows", 0, "pts"), "rtmp://PRIVATE"),
        (("groups", 0, "rows", 0, "dts"), float("nan")),
        (("groups", 0, "rows", 0, "duration"), 2**63),
        (("groups", 0, "rows", 0, "size"), 0),
        (("groups", 0, "rows", 0, "flags"), -1),
        (("groups", 0, "rows", 0, "wall_ms"), 20001),
    ],
)
def test_schema_is_total_for_malformed_numeric_and_unhashable_values(api, path, bad):
    value = sample(api)
    target = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = bad
    assert api["safe_reader_packet_trace"](value) is None


def test_unknown_nested_fields_duplicates_oversize_and_snapshot_copy(api, monkeypatch):
    original = sample(api)
    for path in [(), ("flags",), ("groups", 0), ("groups", 0, "rows", 0)]:
        value = deepcopy(original)
        target = value
        for part in path:
            target = target[part]
        target["PRIVATE"] = "rtmp://PRIVATE"
        assert api["safe_reader_packet_trace"](value) is None
    value = deepcopy(original)
    value["groups"] *= 2
    assert api["safe_reader_packet_trace"](value) is None
    value = deepcopy(original)
    value["groups"][0]["rows"] *= 17
    assert api["safe_reader_packet_trace"](value) is None
    projected = api["safe_reader_packet_trace"](original)
    projected["groups"][0]["rows"][0]["pts"] = 99
    assert original["groups"][0]["rows"][0]["pts"] == 17
    monkeypatch.setitem(api["safe_reader_packet_trace"].__globals__, "PACKET_TRACE_BYTES", 1)
    assert api["safe_reader_packet_trace"](original) is None


def test_real_reader_delegate_keeps_deadline_exception_pipe_cleanup_and_original_progress(
    api, monkeypatch
):
    source = load_self_test()
    namespace = source["run_capture_reader"].__globals__
    captured = []
    failure = subprocess.TimeoutExpired("PRIVATE", 15)

    class Child:
        def __init__(self):
            self.stdout = io.BytesIO(b"frame=82\nout_time_us=3669000\n")
            self.stderr = io.BytesIO(line() + b"\n" + b"x" * (1100 * 1024) + b"\n")
            self.returncode, self.calls = None, []

        def wait(self, timeout=None):
            self.calls.append(("wait", timeout))
            if timeout is not None:
                raise failure
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.calls.append(("kill",))
            self.returncode = -9

    child = Child()

    def popen(command, **kwargs):
        captured.append((deepcopy(command), kwargs))
        return child

    monkeypatch.setitem(
        namespace,
        "subprocess",
        SimpleNamespace(
            Popen=popen,
            DEVNULL=subprocess.DEVNULL,
            PIPE=subprocess.PIPE,
        ),
    )
    state = {"last_stage": "reset-live"}
    original = namespace["run_capture_reader"]
    api["install_reader_packet_trace"](namespace, state)
    wrapped = namespace["run_capture_reader"]
    api["install_reader_packet_trace"](namespace, state)
    assert namespace["run_capture_reader"] is wrapped and wrapped is not original
    command = [
        "ffmpeg",
        "-loglevel",
        "debug",
        "-i",
        "rtmp://127.0.0.1:1234/live/sink",
        "-frames:v",
        "90",
        "-c",
        "copy",
        "capture.flv",
    ]
    before = command[:]
    diagnostic = namespace["CaptureReaderProgress"]()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        wrapped(command, diagnostic, timeout=15)
    assert caught.value is failure and command == before
    index = before.index("-i")
    assert captured[0][0] == before[:index] + ["-fdebug", "ts"] + before[index:]
    assert child.calls == [("wait", 15), ("kill",), ("wait", None)]
    assert child.stdout.closed and child.stderr.closed
    assert diagnostic.snapshot()["reader_frames"] == 82
    assert diagnostic.snapshot()["reader_inspection_limited"]
    assert state["reader_packet_failure"][0] is failure
    assert len(state["reader_packet_failure"][2]["groups"]) == 1


def test_success_records_discard_and_only_first_failure_is_bound(api):
    source = load_self_test()
    calls = []
    error = subprocess.TimeoutExpired("PRIVATE", 15)

    def original(command, diagnostic, *, timeout):
        calls.append(command)
        diagnostic.observe_line(line(), progress=False)
        if len(calls) > 1:
            raise error
        return subprocess.CompletedProcess(command, 0)

    source["run_capture_reader"] = original
    state = {"last_stage": "stall-live"}
    api["install_reader_packet_trace"](source, state)
    command = ["ffmpeg", "-i", "PRIVATE", "-frames:v", "90"]
    source["run_capture_reader"](command, source["CaptureReaderProgress"](), timeout=15)
    assert "reader_packet_failure" not in state
    with pytest.raises(subprocess.TimeoutExpired):
        source["run_capture_reader"](command, source["CaptureReaderProgress"](), timeout=15)
    first = state["reader_packet_failure"]
    with pytest.raises(subprocess.TimeoutExpired):
        source["run_capture_reader"](command, source["CaptureReaderProgress"](), timeout=15)
    assert state["reader_packet_failure"] is first and calls[-1] == command
    final = RuntimeError("PRIVATE")
    final.__cause__ = error
    source["SELF_TEST_MEDIA_FAILURE"] = (final, {})
    projected = api["failed_reader_packet_trace"](source, state, {"stage": "stall-live"})
    assert projected is not None and "PRIVATE" not in json.dumps(projected)
    assert api["failed_reader_packet_trace"](source, state, {"stage": "other"}) is None
    final.__cause__ = subprocess.TimeoutExpired("PRIVATE", 15)
    assert api["failed_reader_packet_trace"](source, state, {"stage": "stall-live"}) is None
    final.__cause__ = final
    assert api["failed_reader_packet_trace"](source, state, {"stage": "stall-live"}) is None


@pytest.mark.parametrize("broken", ["observe_line", "finish"])
def test_optional_observer_failure_never_replaces_original_reader_failure(api, monkeypatch, broken):
    source = load_self_test()
    error = subprocess.TimeoutExpired("PRIVATE", 15)

    def fail(*args, **kwargs):
        raise ValueError("PRIVATE")

    monkeypatch.setattr(api["ReaderPacketTrace"], broken, fail)

    def original(command, diagnostic, *, timeout):
        diagnostic.observe_line(b"frame=82", progress=True)
        diagnostic.observe_line(line(), progress=False)
        raise error

    source["run_capture_reader"] = original
    api["install_reader_packet_trace"](source, {})
    diagnostic = source["CaptureReaderProgress"]()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        source["run_capture_reader"](["ffmpeg", "-i", "PRIVATE"], diagnostic, timeout=15)
    assert caught.value is error and diagnostic.snapshot()["reader_frames"] == 82


@pytest.mark.parametrize(
    "command",
    [["ffmpeg"], ["ffmpeg", "-i", "a", "-i", "b"], ["ffmpeg", "-fdebug", "ts", "-i", "a"]],
)
def test_unexpected_argv_bypasses_optional_hook_without_mutation(api, command):
    source = load_self_test()
    sentinel = object()
    source["run_capture_reader"] = lambda argv, diagnostic, timeout: sentinel
    api["install_reader_packet_trace"](source, {})
    assert (
        source["run_capture_reader"](command, source["CaptureReaderProgress"](), timeout=15)
        is sentinel
    )
