"""Restricted SSH media helper, installed only inside a source FFmpeg sandbox.

Reads one bounded launch request from SSH stdin. It has no database, admin
session, node credential, arbitrary command, or local control-plane fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.validation import validate_destination_url
from app.services.workers import DestinationSpec, build_ffmpeg_argv

MAX_REQUEST = 8192
LEASE_SECONDS = 15.0
STOP_SECONDS = 3.0


def parse_launch(raw: bytes) -> tuple[int, tuple[str, ...]]:
    """Only two URLs and a positive DB identity can cross the exec boundary."""
    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("Invalid media request")
    data: Any = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"destination_id", "input_url", "publish_url"}:
        raise ValueError("Invalid media request")
    identity = data["destination_id"]
    if type(identity) is not int or not 0 < identity < 2**31:
        raise ValueError("Invalid destination identity")
    source, output = data["input_url"], data["publish_url"]
    if not isinstance(source, str) or not isinstance(output, str):
        raise ValueError("Invalid media URLs")
    if any(ord(c) < 32 for c in source + output) or len(source) > 2048 or len(output) > 4096:
        raise ValueError("Invalid media URLs")
    parsed = urlsplit(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "rtmp"
        or parsed.netloc != "mediamtx:1935"
        or not re.fullmatch(r"/live/[A-Za-z0-9_-]{16,128}", parsed.path)
        or parsed.fragment
        or set(query) != {"user", "pass"}
        or any(len(v) != 1 or not v[0] for v in query.values())
    ):
        raise ValueError("Only the source's private MediaMTX input is allowed")
    # Revalidate the full output immediately before FFmpeg resolves/connects.
    validate_destination_url(output)
    spec = DestinationSpec(identity, source, output)
    return identity, build_ffmpeg_argv(spec, ffmpeg_executable="/usr/bin/ffmpeg")


def destination_lock(directory: Path, identity: int) -> int:
    import fcntl

    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError("Unsafe media lease directory")
    fd = os.open(
        directory / f"destination-{identity}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


async def supervise(
    reader: asyncio.StreamReader, argv: tuple[str, ...], *, lease_seconds: float = LEASE_SECONDS
) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        # These pipes belong to the SSH exec channel, not Docker's service logs.
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,
    )
    exited = asyncio.create_task(process.wait())

    async def commands() -> None:
        while True:
            try:
                command = await asyncio.wait_for(reader.readline(), timeout=lease_seconds)
            except (TimeoutError, ValueError):
                return
            if command == b"PING\n":
                continue
            if command == b"KILL\n":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            # EOF, TERM, malformed commands and expired leases all stop this worker.
            return

    control = asyncio.create_task(commands())
    try:
        await asyncio.wait((exited, control), return_when=asyncio.FIRST_COMPLETED)
    finally:
        control.cancel()
        await asyncio.gather(control, return_exceptions=True)
        if not exited.done():
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(exited), STOP_SECONDS)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        await exited
    return process.returncode if process.returncode is not None else 255


async def main() -> int:
    reader = asyncio.StreamReader(limit=MAX_REQUEST)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    lease: int | None = None
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        identity, argv = await asyncio.to_thread(parse_launch, raw)
        # Shared only by exec sessions in this dedicated, non-root sandbox.
        lease = destination_lock(Path("/tmp/adojapan-media-leases"), identity)  # noqa: S108
        return await supervise(reader, argv)
    except (ValueError, OSError, TimeoutError):
        # Input can contain secrets; never format an exception or the request.
        print("Media launch rejected or destination already owned", file=sys.stderr)
        return 75
    finally:
        if lease is not None:
            os.close(lease)
        transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
