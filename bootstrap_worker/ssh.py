"""Bounded AsyncSSH transport with callback-based host-key validation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import asyncssh
from pydantic import SecretStr

from bootstrap_worker.errors import BootstrapError, safe_failure
from bootstrap_worker.host_keys import HostKeyVerifier
from bootstrap_worker.models import HostKeyResult, TargetIdentity, TimeoutPolicy

logging.getLogger("asyncssh").setLevel(logging.WARNING)

_OUTPUT_LIMIT_BYTES = 65_536
_READ_CHUNK_CHARACTERS = 8192


@dataclass(frozen=True, slots=True)
class RemoteResult:
    exit_status: int
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)


class RemoteSession(Protocol):
    async def run(
        self,
        command: str,
        *,
        stdin: SecretStr | None = None,
        timeout: float,
    ) -> RemoteResult: ...

    async def put(self, path: str, content: bytes, *, mode: int, timeout: float) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class SSHConnector(Protocol):
    async def connect(
        self,
        *,
        target: TargetIdentity,
        username: str,
        password: SecretStr,
        verifier: HostKeyVerifier,
        on_host_key_verified: Callable[[HostKeyResult], None],
        timeouts: TimeoutPolicy,
    ) -> RemoteSession: ...


class _PinnedSSHClient(asyncssh.SSHClient):
    def __init__(
        self,
        verifier: HostKeyVerifier,
        on_host_key_verified: Callable[[HostKeyResult], None],
    ) -> None:
        self.verifier = verifier
        self.on_host_key_verified = on_host_key_verified
        self.validation_error: BootstrapError | None = None

    def validate_host_public_key(
        self,
        host: str,
        addr: str,
        port: int,
        key: asyncssh.SSHKey,
    ) -> bool:
        del host, addr, port
        try:
            result = self.verifier.verify(
                key.get_algorithm(),
                key.get_fingerprint("sha256"),
            )
        except BootstrapError as exc:
            self.validation_error = exc
            return False
        self.on_host_key_verified(result)
        return True


def _empty_known_hosts(host: str, addr: str, port: int | None) -> tuple[Any, ...]:
    """Force AsyncSSH to invoke our validator instead of disabling validation."""

    del host, addr, port
    return ((), (), ())


async def _read_bounded(reader: Any) -> str:
    chunks: list[str] = []
    byte_count = 0
    while True:
        chunk = await reader.read(_READ_CHUNK_CHARACTERS)
        if not isinstance(chunk, str):
            raise safe_failure("remote_command_failed")
        if not chunk:
            return "".join(chunks)
        byte_count += len(chunk.encode("utf-8"))
        if byte_count > _OUTPUT_LIMIT_BYTES:
            raise safe_failure("remote_command_failed")
        chunks.append(chunk)


class AsyncSSHSession:
    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        self._connection = connection

    async def run(
        self,
        command: str,
        *,
        stdin: SecretStr | None = None,
        timeout: float,
    ) -> RemoteResult:
        input_value: str | None = None
        process: Any | None = None
        tasks: list[asyncio.Task[Any]] = []
        completed = False
        if stdin is not None:
            input_value = f"{stdin.get_secret_value()}\n"
        try:
            async with asyncio.timeout(timeout):
                process = await self._connection.create_process(
                    command,
                    input=input_value,
                    encoding="utf-8",
                )
                stdout_task = asyncio.create_task(_read_bounded(process.stdout))
                stderr_task = asyncio.create_task(_read_bounded(process.stderr))
                closed_task = asyncio.create_task(process.wait_closed())
                tasks = [stdout_task, stderr_task, closed_task]
                stdout, stderr, _ = await asyncio.gather(*tasks)
                completed = True
        except BootstrapError:
            raise
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise safe_failure("remote_command_failed") from exc
        finally:
            if process is not None and not completed:
                process.close()
            if any(not task.done() for task in tasks):
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            input_value = None
        exit_status = process.exit_status if process.exit_status is not None else -1
        return RemoteResult(exit_status=exit_status, stdout=stdout, stderr=stderr)

    async def put(self, path: str, content: bytes, *, mode: int, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                async with self._connection.start_sftp_client() as sftp:
                    attrs = asyncssh.SFTPAttrs(permissions=mode)
                    async with sftp.open(path, "xb", attrs=attrs) as remote_file:
                        await remote_file.write(content)
                    await sftp.chmod(path, mode)
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise safe_failure("remote_upload_failed") from exc

    def close(self) -> None:
        self._connection.close()

    async def wait_closed(self) -> None:
        await self._connection.wait_closed()


class AsyncSSHConnector:
    """Open a password-only SSH session to a previously pinned numeric IP."""

    _HOST_KEY_ALGORITHMS = (
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "rsa-sha2-512",
        "rsa-sha2-256",
    )

    async def connect(
        self,
        *,
        target: TargetIdentity,
        username: str,
        password: SecretStr,
        verifier: HostKeyVerifier,
        on_host_key_verified: Callable[[HostKeyResult], None],
        timeouts: TimeoutPolicy,
    ) -> RemoteSession:
        client = _PinnedSSHClient(verifier, on_host_key_verified)
        password_value = password.get_secret_value()
        try:
            connection = await asyncssh.connect(
                target.resolved_ip,
                port=target.port,
                username=username,
                password=password_value,
                client_factory=lambda: client,
                known_hosts=_empty_known_hosts,
                host_key_alias=target.address,
                server_host_key_algs=self._HOST_KEY_ALGORITHMS,
                client_keys=None,
                agent_path=None,
                config=None,
                host_based_auth=False,
                public_key_auth=False,
                kbdint_auth=False,
                password_auth=True,
                gss_auth=False,
                gss_kex=False,
                connect_timeout=timeouts.connect_seconds,
                login_timeout=timeouts.authentication_seconds,
                encoding="utf-8",
            )
        except asyncssh.PermissionDenied as exc:
            if client.validation_error is not None:
                raise client.validation_error from exc
            raise safe_failure("ssh_authentication_failed") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            if client.validation_error is not None:
                raise client.validation_error from exc
            raise safe_failure("ssh_connection_failed") from exc
        finally:
            password_value = ""
        if verifier.result is None:
            connection.close()
            await connection.wait_closed()
            raise safe_failure("ssh_host_key_unsupported")
        return AsyncSSHSession(connection)


__all__ = [
    "AsyncSSHConnector",
    "AsyncSSHSession",
    "RemoteResult",
    "RemoteSession",
    "SSHConnector",
]
