"""Move media execution and private MediaMTX transport to one pinned SSH host."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress

import asyncssh

from app.core.media_ssh import MediaSSHConfig
from app.services.workers import DestinationId, ProcessHandle

REMOTE_COMMAND = "/usr/local/libexec/restream-media-exec"


class SSHProcessHandle:
    def __init__(self, process: asyncssh.SSHClientProcess[bytes]) -> None:
        self._process = process
        self._heartbeat = asyncio.create_task(self._keep_lease(), name="ssh-media-lease")

    async def _keep_lease(self) -> None:
        try:
            while self._process.returncode is None:
                self._process.stdin.write(b"PING\n")
                await self._process.stdin.drain()
                await asyncio.sleep(3)
        except (OSError, asyncssh.Error):
            return

    @property
    def pid(self) -> None:
        # A remote process is never represented by an invented local PID.
        return None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        try:
            result = await self._process.wait(check=False)
            return result.returncode if result.returncode is not None else 255
        finally:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)

    def _control(self, command: bytes) -> None:
        with suppress(OSError, asyncssh.Error):
            self._process.stdin.write(command)

    def terminate(self) -> None:
        self._control(b"TERM\n")

    def kill(self) -> None:
        self._control(b"KILL\n")

    async def _lines(self, reader: asyncssh.SSHReader[bytes]) -> AsyncIterator[str]:
        buffer = bytearray()
        discarding = False
        while chunk := await reader.read(4096):
            for byte in chunk:
                if byte == 10:
                    if buffer and not discarding:
                        yield buffer.decode("utf-8", errors="replace").rstrip("\r")
                    buffer.clear()
                    discarding = False
                elif not discarding:
                    if len(buffer) < 4096:
                        buffer.append(byte)
                    else:
                        buffer.clear()
                        discarding = True

    def iter_progress(self) -> AsyncIterator[str]:
        return self._lines(self._process.stdout)

    def iter_stderr(self) -> AsyncIterator[str]:
        return self._lines(self._process.stderr)


class SSHMediaTransport:
    """No local launcher fallback; one DB/control process remains authoritative."""

    def __init__(self, config: MediaSSHConfig) -> None:
        self.config = config
        self._connection: asyncssh.SSHClientConnection | None = None
        self._listeners: list[asyncssh.SSHListener] = []
        self._lock = asyncio.Lock()
        self._maintenance: asyncio.Task[None] | None = None
        self._closed = False

    async def connection(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._closed:
                raise RuntimeError("SSH media transport is closed")
            if self._connection is not None and not self._connection.is_closed():
                return self._connection
            await self._disconnect()
            connection: asyncssh.SSHClientConnection | None = None
            try:
                async with asyncio.timeout(20):
                    connection = await asyncssh.connect(
                        self.config.host,
                        self.config.port,
                        username=self.config.username,
                        client_keys=[str(self.config.private_key)],
                        known_hosts=str(self.config.known_hosts),
                        agent_path=None,
                        config=None,
                        keepalive_interval=5,
                        keepalive_count_max=3,
                        login_timeout=10,
                    )
                    self._listeners.append(
                        await connection.forward_local_port(
                            "127.0.0.1", self.config.api_local_port, self.config.media_address, 9997
                        )
                    )
                    self._listeners.append(
                        await connection.forward_local_port(
                            "127.0.0.1", self.config.hls_local_port, self.config.media_address, 8888
                        )
                    )
                    # The SSH server must permit this exact private bind only.
                    self._listeners.append(
                        await connection.forward_remote_port(
                            self.config.auth_bind_address,
                            self.config.auth_remote_port,
                            "127.0.0.1",
                            8000,
                        )
                    )
                self._connection = connection
                return connection
            except BaseException as exc:
                if connection is not None:
                    connection.close()
                    await connection.wait_closed()
                await self._disconnect()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                # SSH errors must not echo untrusted remote messages into logs.
                raise RuntimeError("Pinned SSH media transport is unavailable") from None

    async def _disconnect(self) -> None:
        for listener in self._listeners:
            listener.close()
        self._listeners.clear()
        if self._connection is not None:
            self._connection.close()
            await self._connection.wait_closed()
        self._connection = None

    async def start(self) -> None:
        await self.connection()

        async def maintain() -> None:
            while True:
                await asyncio.sleep(2)
                with suppress(RuntimeError):
                    await self.connection()

        self._maintenance = asyncio.create_task(maintain(), name="ssh-media-transport")

    async def close(self) -> None:
        self._closed = True
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        async with self._lock:
            await self._disconnect()

    async def spawn(self, argv: Sequence[str]) -> ProcessHandle:
        raise RuntimeError("SSH media launch requires a destination identity")

    async def spawn_for_destination(
        self, destination_id: DestinationId, argv: Sequence[str]
    ) -> ProcessHandle:
        # This protocol transports values over encrypted stdin, never a shell command.
        if len(argv) != 19 or argv[8] != "-i":
            raise ValueError("Unsupported media command")
        payload = (
            json.dumps(
                {
                    "destination_id": int(destination_id),
                    "input_url": argv[9],
                    "publish_url": argv[18],
                }
            ).encode()
            + b"\n"
        )
        if len(payload) > 8192:
            raise ValueError("Media launch request is too large")
        connection = await self.connection()
        process: asyncssh.SSHClientProcess[bytes] | None = None
        try:
            process = await connection.create_process(REMOTE_COMMAND, encoding=None)
            process.stdin.write(payload)
            await process.stdin.drain()
            return SSHProcessHandle(process)
        except BaseException as exc:
            if process is not None:
                process.close()
                await process.wait_closed()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError("Remote media process could not be started") from None
