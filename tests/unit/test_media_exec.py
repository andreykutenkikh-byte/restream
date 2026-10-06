from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from scripts.media_exec import destination_lock, parse_launch, supervise


def launch(**overrides: object) -> bytes:
    values = {
        "destination_id": 1,
        "input_url": "rtmp://mediamtx:1935/live/abcdefghijklmnop?user=worker&pass=synthetic",
        "publish_url": "rtmps://8.8.8.8/live/synthetic-key",
    }
    values.update(overrides)
    return json.dumps(values).encode() + b"\n"


def test_fixed_stream_copy_command() -> None:
    identity, argv = parse_launch(launch())
    assert identity == 1 and argv[0] == "/usr/bin/ffmpeg"
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[-1] == "rtmps://8.8.8.8/live/synthetic-key"


@pytest.mark.parametrize(
    "overrides",
    [
        {"destination_id": True},
        {"destination_id": 0},
        {"destination_id": "1"},
        {"shell": "id"},
        {"input_url": "file:///etc/passwd"},
        {"input_url": "rtmp://8.8.8.8/live/abcdefghijklmnop?user=u&pass=p"},
        {"input_url": "rtmp://mediamtx:1935/live/abcdefghijklmnop?user=u&pass=p&pass=q"},
        {"input_url": "rtmp://mediamtx:1935/live/abcdefghijklmnop?user=u&pass="},
        {"publish_url": "rtmp://127.0.0.1/live/key"},
        {"publish_url": "rtmp://169.254.169.254/live/key"},
        {"publish_url": "rtmp://8.8.8.8/live/key;command"},
        {"publish_url": "file:///tmp/video"},
        {"publish_url": "x" * 9000},
    ],
)
def test_rejects_unsafe_launch_values(overrides: dict) -> None:
    with pytest.raises(ValueError):
        parse_launch(launch(**overrides))


@pytest.mark.skipif(os.name != "posix", reason="Linux media sandbox uses flock")
def test_destination_fencing_and_release(tmp_path: Path) -> None:
    directory = tmp_path / "leases"
    first = destination_lock(directory, 1)
    try:
        with pytest.raises(BlockingIOError):
            destination_lock(directory, 1)
        different = destination_lock(directory, 2)
        os.close(different)
    finally:
        os.close(first)
    os.close(destination_lock(directory, 1))


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group lifecycle")
@pytest.mark.parametrize("command", [b"", b"TERM\n", b"KILL\n", b"invalid\n", None])
async def test_lost_controller_or_stop_reaps_process(command: bytes | None, tmp_path: Path) -> None:
    pidfile = tmp_path / "worker.pid"
    code = (
        "import os,pathlib,time,sys;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
    )
    reader = asyncio.StreamReader()
    task = asyncio.create_task(
        supervise(reader, (sys.executable, "-c", code, str(pidfile)), lease_seconds=0.2)
    )
    try:
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        if command is not None:
            reader.feed_data(command)
            if not command:
                reader.feed_eof()
        result = await asyncio.wait_for(task, 5)
        assert result in (-signal.SIGTERM, -signal.SIGKILL)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
