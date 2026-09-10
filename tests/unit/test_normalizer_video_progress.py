from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

NORMALIZER = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/moblin-relay-normalize"


def load():
    loader = importlib.machinery.SourceFileLoader("video_progress_test", str(NORMALIZER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def progress():
    module = load()
    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    reader = module.VideoProgress(pipe, 0.0)
    try:
        yield module, reader, write_fd
    finally:
        reader.close()
        os.close(write_fd)


def test_complete_numeric_video_block_required(progress):
    _, reader, writer = progress
    reader.sample(0.1)  # Empty live pipe must never block the supervisor.
    os.write(writer, b"frame=12\nfps=30\n")
    reader.sample(0.2)
    assert not reader.has_video()
    os.write(writer, b"progress=continue\n")
    reader.sample(0.3)
    assert reader.frames == 12 and reader.last_growth == 0.3


def test_split_crlf_and_multiple_blocks(progress):
    _, reader, writer = progress
    os.write(writer, b"frame=   15\r\nprogress=cont")
    reader.sample(0.5)
    assert reader.frames == 0
    os.write(writer, b"inue\r\nframe=30\nprogress=continue\n")
    reader.sample(1.0)
    assert reader.frames == 30 and reader.last_growth == 1.0


@pytest.mark.parametrize("value", [b"", b"PRIVATE", b"-2", b"NaN", b"1.0", b"9" * 13])
def test_invalid_counts_cannot_refresh_video_health(progress, value):
    _, reader, writer = progress
    os.write(writer, b"frame=" + value + b"\nprogress=continue\n")
    reader.sample(3.0)
    assert not reader.has_video() and reader.stalled(3.0)
    assert not hasattr(reader, "raw_log")


def test_regression_repeats_and_audio_progress_do_not_refresh_video(progress):
    _, reader, writer = progress
    os.write(writer, b"frame=90\nprogress=continue\n")
    reader.sample(0.5)
    for now, frame in ((1.0, 90), (2.0, 89), (3.0, 90)):
        os.write(writer, f"frame={frame}\nout_time_us=9000000\nprogress=continue\n".encode())
        reader.sample(now)
    assert reader.frames == 90 and reader.last_growth == 0.5 and reader.stalled(3.0)


def test_oversized_line_is_bounded_and_cannot_smuggle_frame_suffix(progress):
    _, reader, writer = progress
    os.write(writer, b"x" * 1024 + b"frame=999\nprogress=continue\n")
    reader.sample(3.0)
    assert reader.frames == 0 and len(reader.pending) <= reader.LINE_LIMIT
    os.write(writer, b"frame=30\nprogress=continue\n")
    reader.sample(3.1)
    assert reader.frames == 30


def test_audio_byte_growth_no_longer_masks_stopped_video(progress):
    module, reader, writer = progress
    watchdog = module.MediaWatchdog(("same-output", 100), 0.0)
    os.write(writer, b"frame=90\nprogress=continue\n")
    reader.sample(0.0)
    for step in range(1, 61):
        now = step * 0.05
        # Counterfactual: the previous aggregate-only gate accepts audio forever.
        keep, probe = watchdog.observe_output(True, ("same-output", 100 + step), now)
        assert keep and not probe
        reader.sample(now)
        if reader.stalled(now):
            assert not watchdog.reject(module.RESTART_REASON_VIDEO_STALLED)
            break
    else:
        pytest.fail("audio-only output concealed stalled video")
    assert now == 2.5
    assert watchdog.failure_reason == "video-stalled"
    assert watchdog.confirmed_stall_gate("11111111-2222-4333-8444-555555555555") is None
    assert not module.SOURCE_RESET_ELIGIBLE_REASONS


def test_short_video_pause_recovers_without_restart(progress):
    _, reader, writer = progress
    for now, frame in ((0.0, 1), (0.5, 15), (2.4, 16), (2.9, 30)):
        os.write(writer, f"frame={frame}\nprogress=continue\n".encode())
        reader.sample(now)
        assert not reader.stalled(now)
    assert reader.frames == 30


def test_eof_does_not_reset_frame_age_or_wait_for_more_data():
    module = load()
    read_fd, write_fd = os.pipe()
    reader = module.VideoProgress(os.fdopen(read_fd, "rb", buffering=0), 0.0)
    os.write(write_fd, b"frame=30\nprogress=continue\n")
    os.close(write_fd)
    try:
        reader.sample(0.1)
        assert reader.frames == 30 and reader.ended
        reader.sample(3.0)
        assert reader.last_growth == 0.1 and reader.stalled(3.0)
    finally:
        reader.close()


def test_progress_read_error_is_bounded_and_cannot_refresh_health(progress, monkeypatch):
    module, reader, _writer = progress

    def fail_read(*_args):
        raise OSError("PRIVATE")

    monkeypatch.setattr(module, "os", SimpleNamespace(read=fail_read))
    reader.sample(3.0)
    assert reader.ended and not reader.has_video() and reader.stalled(3.0)


def test_one_drain_has_fixed_byte_budget_even_if_child_never_stops_writing(progress, monkeypatch):
    module, reader, _writer = progress
    consumed = []

    def endless_read(_fd, count):
        consumed.append(count)
        return b"x" * count

    monkeypatch.setattr(module, "os", SimpleNamespace(read=endless_read))
    reader.sample(3.0)
    assert sum(consumed) == reader.READ_LIMIT
    assert len(reader.pending) <= reader.LINE_LIMIT and not reader.has_video()


def test_progress_is_private_pipe_and_video_remains_stream_copy():
    module = load()
    argv = module.build_ffmpeg_argv(18554, 11936)
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert module.OUTPUT_START_TIMEOUT_SECONDS == 6.0
    assert module.OUTPUT_IDLE_FALLBACK_SECONDS == 2.5
    source = NORMALIZER.read_text(encoding="utf-8")
    assert "stdout=subprocess.PIPE" in source
    assert "video_progress.has_video()" in source
    assert "video_progress.stalled(now)" in source
    assert "video_progress.close()" in source


@pytest.mark.parametrize(
    ("frames", "expected_reason"), [(90, "video-stalled"), (0, "output-start-timeout")]
)
def test_real_supervisor_releases_audio_only_child_without_resetting_source(
    monkeypatch, frames, expected_reason
):
    module = load()
    source_id = "11111111-2222-4333-8444-555555555555"
    clock = [0.0]
    handlers, children, reasons, states = {}, [], [], []

    def sleep(seconds):
        clock[0] += seconds
        assert clock[0] < 8, "supervisor did not release stalled video"

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(
        module,
        "signal",
        SimpleNamespace(
            SIGHUP=1,
            SIGINT=2,
            SIGTERM=15,
            signal=lambda number, handler: handlers.update({number: handler}),
        ),
    )
    monkeypatch.setattr(module, "make_parent_death_setup", lambda _pid: lambda: None)
    monkeypatch.setattr(module, "emit_state_event", states.append)

    def restarted(reason):
        reasons.append(reason)
        handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)

    monkeypatch.setattr(module, "emit_restart_reason", restarted)
    monkeypatch.setattr(
        module, "kick_srt_source", lambda *_a, **_k: pytest.fail("unproven SRT reset")
    )

    class Reader:
        def __init__(self, _port, path, _parser):
            self.ingest = path != module.OUTPUT_METRICS_PATH
            self.counter = 0

        def sample(self):
            self.counter += 1000  # Both network input and AAC output remain active.
            return True, (source_id if self.ingest else "same-output", self.counter)

        def close(self):
            pass

    monkeypatch.setattr(module, "MetricsReader", Reader)

    class Child:
        def __init__(self, _argv, **kwargs):
            assert kwargs["stdout"] == module.subprocess.PIPE
            read_fd, write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.writer = write_fd
            self.stopped = False
            os.write(write_fd, f"frame={frames}\nprogress=continue\n".encode())
            children.append(self)

        def poll(self):
            return 0 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self, **_kwargs):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Child)
    try:
        assert module.run_supervisor(18554, 11936, 19998, source_id) == 0
        assert reasons == [expected_reason]
        assert ("bridge-active" in states) is bool(frames)
        assert len(children) == 1
        assert children[0].stopped and children[0].stdout.closed
    finally:
        for child in children:
            child.stdout.close()
            os.close(child.writer)
