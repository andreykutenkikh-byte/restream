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
        auto_exit: int | None = None,
        terminate_exits: bool = True,
    ) -> None:
        self._pid = pid
        self._stderr = tuple(stderr)
        self._future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
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
        "live_after_seconds": 0.0,
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
        process = FakeProcess(pid=102)
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
        process = FakeProcess(pid=303, terminate_exits=False)
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
