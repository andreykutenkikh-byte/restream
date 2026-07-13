from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from app.services.workers import (
    DestinationSpec,
    IngestSnapshot,
    ReconnectPolicy,
    WorkerManager,
    WorkerRuntimeConfig,
    WorkerState,
    WorkerStatus,
    build_ffmpeg_argv,
    check_stream_compatibility,
    redact_diagnostic,
)


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int,
        stderr: Sequence[str] = (),
        progress: Sequence[str] = (),
        auto_exit: int | None = None,
        terminate_exits: bool = True,
    ) -> None:
        self._pid = pid
        self._stderr = tuple(stderr)
        self._future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._progress: asyncio.Queue[str | None] = asyncio.Queue()
        for line in progress:
            self._progress.put_nowait(line)
        self.terminated = False
        self.killed = False
        self._terminate_exits = terminate_exits
        if auto_exit is not None:
            asyncio.get_running_loop().call_soon(self.exit, auto_exit)

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._future.result() if self._future.done() else None

    async def wait(self) -> int:
        return await asyncio.shield(self._future)

    def terminate(self) -> None:
        self.terminated = True
        if self._terminate_exits:
            self.exit(-15)

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)

    def exit(self, code: int) -> None:
        if not self._future.done():
            self._future.set_result(code)
            self._progress.put_nowait(None)

    def emit_progress(self, *lines: str) -> None:
        for line in lines:
            self._progress.put_nowait(line)

    async def iter_progress(self) -> AsyncIterator[str]:
        while True:
            line = await self._progress.get()
            if line is None:
                break
            yield line

    async def iter_stderr(self) -> AsyncIterator[str]:
        for line in self._stderr:
            await asyncio.sleep(0)
            yield line


class FakeLauncher:
    def __init__(self, processes: Sequence[FakeProcess]) -> None:
        self._processes = list(processes)
        self.calls: list[tuple[str, ...]] = []

    async def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.calls.append(tuple(argv))
        if not self._processes:
            raise RuntimeError("no fake process configured")
        return self._processes.pop(0)


def fast_runtime(**overrides: object) -> WorkerRuntimeConfig:
    values: dict[str, Any] = {
        "input_poll_seconds": 0.005,
        "process_poll_seconds": 0.005,
        "start_timeout_seconds": 0.03,
        "progress_timeout_seconds": 0.03,
        "terminate_grace_seconds": 0.005,
        "stop_timeout_seconds": 0.1,
        "diagnostic_lines": 4,
        "diagnostic_line_chars": 120,
    }
    values.update(overrides)
    return WorkerRuntimeConfig(**values)


async def wait_until(predicate: Callable[[], bool], limit_seconds: float = 0.5) -> None:
    deadline = time.monotonic() + limit_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.001)


def test_builds_fixed_exec_argv_for_stream_copy() -> None:
    spec = DestinationSpec(
        "youtube",
        "rtmp://mediamtx:1935/live/input-key",
        "rtmps://stream.example/live/output-key;not-a-shell-command",
    )

    argv = build_ffmpeg_argv(spec, ffmpeg_executable="/usr/bin/ffmpeg")

    assert argv[0] == "/usr/bin/ffmpeg"
    assert argv[-1] == spec.publish_url
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[argv.index("-f") + 1] == "flv"
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in argv
    assert "0:v:0?" in argv and "0:a:0?" in argv
    assert len([item for item in argv if "not-a-shell-command" in item]) == 1


def test_reconnect_policy_is_exponential_and_bounded() -> None:
    policy = ReconnectPolicy(
        initial_delay_seconds=1,
        max_delay_seconds=5,
        multiplier=2,
        max_fast_failures=4,
    )

    assert [policy.delay_for(index) for index in range(1, 6)] == [1, 2, 4, 5, 5]


def test_rejects_incompatible_codecs_without_transcoding() -> None:
    assert check_stream_compatibility(
        IngestSnapshot("live", "h264", "aac", metadata_known=True)
    ) == (True, None)
    compatible, video_error = check_stream_compatibility(
        IngestSnapshot("live", "vp9", "aac", metadata_known=True)
    )
    compatible_audio, audio_error = check_stream_compatibility(
        IngestSnapshot("live", "h264", "opus", metadata_known=True)
    )

    assert not compatible and "H.264" in (video_error or "")
    assert not compatible_audio and "AAC" in (audio_error or "")


def test_worker_start_is_idempotent_and_stop_is_graceful() -> None:
    async def scenario() -> None:
        secret = "real-output-key"
        publish_url = f"rtmps://user:password@stream.example/live/{secret}?token=abc"
        process = FakeProcess(
            pid=101,
            stderr=(f"failed to write {publish_url}: token={secret}",),
            progress=("out_time_us=1000", "progress=continue"),
        )
        launcher = FakeLauncher([process])
        transitions: list[WorkerState] = []

        async def on_status(_: object, status: WorkerStatus) -> None:
            transitions.append(status.state)

        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx:1935/live/input-secret",
                publish_url,
                (secret, "password", "abc", "input-secret"),
            ),
            lambda: IngestSnapshot("live", "h264", "aac", metadata_known=True),
            launcher=launcher,
            runtime=fast_runtime(),
            status_callback=on_status,
        )

        await asyncio.gather(manager.start("youtube"), manager.start("youtube"))
        await wait_until(lambda: manager.status("youtube").state is WorkerState.LIVE)

        assert len(launcher.calls) == 1
        assert manager.status("youtube").pid == 101

        stopped = await manager.stop("youtube")
        assert stopped.state is WorkerState.STOPPED
        assert process.terminated and not process.killed
        assert WorkerState.CONNECTING in transitions
        assert WorkerState.LIVE in transitions
        assert transitions[-1] is WorkerState.STOPPED

        diagnostics = " ".join(stopped.diagnostics)
        assert secret not in diagnostics
        assert "password" not in diagnostics
        assert "token=abc" not in diagnostics

    asyncio.run(scenario())


def test_enabled_worker_waits_for_input_then_starts_automatically() -> None:
    async def scenario() -> None:
        ingest = {"live": False}
        process = FakeProcess(pid=102, progress=("out_time_us=1000",))
        launcher = FakeLauncher([process])
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: ingest["live"],
            launcher=launcher,
            runtime=fast_runtime(),
        )

        await manager.start(1)
        await wait_until(lambda: manager.status(1).state is WorkerState.WAITING_FOR_INPUT)
        assert launcher.calls == []

        ingest["live"] = True
        manager.notify_ingest_changed()
        await wait_until(lambda: manager.status(1).state is WorkerState.LIVE)
        assert len(launcher.calls) == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_process_without_progress_never_becomes_live_and_times_out() -> None:
    async def scenario() -> None:
        process = FakeProcess(pid=150)
        transitions: list[WorkerState] = []

        async def on_status(_: object, status: WorkerStatus) -> None:
            transitions.append(status.state)

        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=FakeLauncher([process]),
            runtime=fast_runtime(start_timeout_seconds=0.04),
            status_callback=on_status,
        )

        await manager.start("silent")
        await wait_until(lambda: manager.status("silent").pid == 150)
        await asyncio.sleep(0.015)
        assert manager.status("silent").state is WorkerState.CONNECTING
        assert WorkerState.LIVE not in transitions

        await wait_until(lambda: manager.status("silent").state is WorkerState.RECONNECTING)
        status = manager.status("silent")
        assert process.terminated
        assert "start timeout" in (status.last_error or "")
        assert WorkerState.LIVE not in transitions
        await manager.shutdown()

    asyncio.run(scenario())


def test_unreachable_or_rejecting_destination_never_becomes_live() -> None:
    async def scenario(exit_code: int) -> None:
        process = FakeProcess(pid=160 + exit_code, auto_exit=exit_code)
        transitions: list[WorkerState] = []

        async def on_status(_: object, status: WorkerStatus) -> None:
            transitions.append(status.state)

        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=FakeLauncher([process]),
            runtime=fast_runtime(),
            status_callback=on_status,
        )

        await manager.start(f"failure-{exit_code}")
        await wait_until(
            lambda: manager.status(f"failure-{exit_code}").state is WorkerState.RECONNECTING
        )
        assert WorkerState.LIVE not in transitions
        assert f"code {exit_code}" in (manager.status(f"failure-{exit_code}").last_error or "")
        await manager.shutdown()

    asyncio.run(scenario(1))
    asyncio.run(scenario(2))


def test_positive_output_progress_is_required_before_live() -> None:
    async def scenario() -> None:
        process = FakeProcess(pid=170)
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=FakeLauncher([process]),
            runtime=fast_runtime(start_timeout_seconds=0.1),
        )

        await manager.start("progress")
        await wait_until(lambda: manager.status("progress").pid == 170)
        process_started_at = manager.status("progress").started_at
        process.emit_progress("total_size=128", "progress=continue")
        await asyncio.sleep(0.015)
        assert manager.status("progress").state is WorkerState.CONNECTING

        process.emit_progress("total_size=256", "progress=continue")
        await wait_until(lambda: manager.status("progress").state is WorkerState.LIVE)
        status = manager.status("progress")
        assert process_started_at is not None
        assert status.started_at == process_started_at
        assert status.live_since is not None
        assert status.live_since >= process_started_at
        await manager.shutdown()

    asyncio.run(scenario())


def test_stalled_progress_terminates_process_and_reconnects() -> None:
    async def scenario() -> None:
        first = FakeProcess(pid=180, progress=("out_time_us=1000",))
        second = FakeProcess(pid=181)
        launcher = FakeLauncher([first, second])
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=launcher,
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.001,
                max_delay_seconds=0.002,
                max_fast_failures=3,
                stable_after_seconds=10,
            ),
            runtime=fast_runtime(progress_timeout_seconds=0.025),
        )

        await manager.start("stalled")
        await wait_until(lambda: manager.status("stalled").state is WorkerState.LIVE)
        await wait_until(lambda: len(launcher.calls) == 2)

        status = manager.status("stalled")
        assert first.terminated
        assert status.state is WorkerState.RECONNECTING
        assert status.restart_count == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_process_exit_after_progress_reconnects() -> None:
    async def scenario() -> None:
        first = FakeProcess(pid=190, progress=("out_time=00:00:00.100000",))
        second = FakeProcess(pid=191, progress=("out_time_us=2000",))
        launcher = FakeLauncher([first, second])
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=launcher,
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.001,
                max_delay_seconds=0.002,
                max_fast_failures=3,
                stable_after_seconds=10,
            ),
            runtime=fast_runtime(),
        )

        await manager.start("exit")
        await wait_until(lambda: manager.status("exit").state is WorkerState.LIVE)
        first.exit(1)
        await wait_until(
            lambda: (
                manager.status("exit").state is WorkerState.LIVE
                and manager.status("exit").restart_count == 1
            )
        )
        assert len(launcher.calls) == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_fast_failures_stop_at_limit_without_restart_loop() -> None:
    async def scenario() -> None:
        processes = [FakeProcess(pid=200 + index, auto_exit=1) for index in range(3)]
        launcher = FakeLauncher(processes)
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=launcher,
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.001,
                max_delay_seconds=0.002,
                multiplier=2,
                max_fast_failures=3,
                stable_after_seconds=10,
            ),
            runtime=fast_runtime(),
        )

        await manager.start("failing")
        await wait_until(lambda: manager.status("failing").state is WorkerState.FAILED)

        status = manager.status("failing")
        assert len(launcher.calls) == 3
        assert status.restart_count == 2
        assert status.consecutive_failures == 3
        assert status.last_exit_code == 1
        assert not status.desired_running

    asyncio.run(scenario())


def test_input_loss_before_stable_progress_preserves_fast_failure_count() -> None:
    async def scenario() -> None:
        ingest = {"available": True}
        first = FakeProcess(pid=230, auto_exit=1)
        interrupted = FakeProcess(pid=231)
        final = FakeProcess(pid=232, auto_exit=1)
        launcher = FakeLauncher([first, interrupted, final])
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: ingest["available"],
            launcher=launcher,
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.001,
                max_delay_seconds=0.002,
                max_fast_failures=2,
                stable_after_seconds=10,
            ),
            runtime=fast_runtime(),
        )

        await manager.start("preserve-failures")
        await wait_until(lambda: len(launcher.calls) == 2)
        ingest["available"] = False
        await wait_until(
            lambda: manager.status("preserve-failures").state is WorkerState.WAITING_FOR_INPUT
        )
        assert interrupted.terminated
        assert manager.status("preserve-failures").consecutive_failures == 1

        ingest["available"] = True
        manager.notify_ingest_changed()
        await wait_until(lambda: manager.status("preserve-failures").state is WorkerState.FAILED)

        status = manager.status("preserve-failures")
        assert len(launcher.calls) == 3
        assert status.consecutive_failures == 2
        assert not status.desired_running

    asyncio.run(scenario())


def test_incompatible_metadata_fails_before_process_spawn() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher([])
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: IngestSnapshot("live", "h265", "aac", metadata_known=True),
            launcher=launcher,
            runtime=fast_runtime(),
        )

        await manager.start("unsupported")
        await wait_until(lambda: manager.status("unsupported").state is WorkerState.FAILED)
        assert launcher.calls == []
        assert "H.264" in (manager.status("unsupported").last_error or "")

    asyncio.run(scenario())


def test_stop_escalates_to_kill_after_grace_period() -> None:
    async def scenario() -> None:
        process = FakeProcess(
            pid=303,
            progress=("out_time=00:00:00.040000",),
            terminate_exits=False,
        )
        manager = WorkerManager(
            lambda destination_id: DestinationSpec(
                destination_id,
                "rtmp://mediamtx/live/input",
                "rtmps://example/live/output",
            ),
            lambda: True,
            launcher=FakeLauncher([process]),
            runtime=fast_runtime(),
        )

        await manager.start("slow-stop")
        await wait_until(lambda: manager.status("slow-stop").state is WorkerState.LIVE)
        await manager.stop("slow-stop")

        assert process.terminated
        assert process.killed

    asyncio.run(scenario())


def test_diagnostics_are_bounded_and_secret_safe() -> None:
    raw = (
        "publish rtmps://alice:password@example.test/live/stream-secret"
        "?token=query-secret failed; password=hunter2"
    )

    safe = redact_diagnostic(
        raw,
        secret_values=("stream-secret", "query-secret", "hunter2", "password"),
    )

    assert "stream-secret" not in safe
    assert "query-secret" not in safe
    assert "hunter2" not in safe
    assert "alice" not in safe
    assert "[REDACTED]" in safe
