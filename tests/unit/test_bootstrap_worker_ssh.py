from __future__ import annotations

import base64
from typing import Any

import pytest
from pydantic import SecretStr

import bootstrap_worker.ssh as ssh_module
from bootstrap_worker.errors import BootstrapError
from bootstrap_worker.host_keys import HostKeyVerifier
from bootstrap_worker.models import TargetIdentity, TimeoutPolicy
from bootstrap_worker.ssh import AsyncSSHConnector, AsyncSSHSession

FINGERPRINT = "SHA256:" + base64.b64encode(bytes(range(32))).decode().rstrip("=")
UPLOAD_PATH = "/tmp/job/enrollment.token"  # noqa: S108 - fake remote path


class FakeKey:
    def __init__(self, fingerprint: str = FINGERPRINT) -> None:
        self.fingerprint = fingerprint

    def get_algorithm(self) -> str:
        return "ssh-ed25519"

    def get_fingerprint(self, hash_name: str) -> str:
        assert hash_name == "sha256"
        return self.fingerprint


class FakeReader:
    def __init__(self, value: str) -> None:
        self.value = value

    async def read(self, size: int) -> str:
        chunk, self.value = self.value[:size], self.value[size:]
        return chunk


class FakeProcess:
    def __init__(self, stdout: str, stderr: str, exit_status: int = 0) -> None:
        self.stdout = FakeReader(stdout)
        self.stderr = FakeReader(stderr)
        self.exit_status = exit_status
        self.closed = False

    async def wait_closed(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeRemoteFile:
    def __init__(self) -> None:
        self.content = b""

    async def __aenter__(self) -> FakeRemoteFile:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def write(self, content: bytes) -> None:
        self.content += content


class FakeSFTP:
    def __init__(self) -> None:
        self.open_calls: list[tuple[str, str, object]] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.remote_file = FakeRemoteFile()

    async def __aenter__(self) -> FakeSFTP:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def open(self, path: str, mode: str, *, attrs: object) -> FakeRemoteFile:
        self.open_calls.append((path, mode, attrs))
        return self.remote_file

    async def chmod(self, path: str, mode: int) -> None:
        self.chmod_calls.append((path, mode))


class FakeConnection:
    def __init__(self, *, stdout: str = "ok\n", stderr: str = "") -> None:
        self.closed = False
        self.stdout = stdout
        self.stderr = stderr
        self.process_calls: list[tuple[str, dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []
        self.sftp = FakeSFTP()

    async def create_process(self, command: str, **kwargs: Any) -> FakeProcess:
        self.process_calls.append((command, kwargs))
        process = FakeProcess(self.stdout, self.stderr)
        self.processes.append(process)
        return process

    def start_sftp_client(self) -> FakeSFTP:
        return self.sftp

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def target() -> TargetIdentity:
    return TargetIdentity(
        address="node.example.com",
        port=22,
        resolved_ip="8.8.8.8",
        resolution_set=("8.8.8.8",),
    )


async def test_connector_uses_pinned_numeric_ip_and_password_only_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    connection = FakeConnection()

    async def fake_connect(host: str, **kwargs: Any) -> object:
        captured["host"] = host
        captured.update(kwargs)
        client = kwargs["client_factory"]()
        assert client.validate_host_public_key("node.example.com", "8.8.8.8", 22, FakeKey())
        return connection

    monkeypatch.setattr(ssh_module.asyncssh, "connect", fake_connect)
    callback_count = 0

    def verified(result: object) -> None:
        nonlocal callback_count
        assert result == verifier.result
        callback_count += 1

    verifier = HostKeyVerifier(expected_fingerprint=FINGERPRINT, pinned_fingerprint=None)
    session = await AsyncSSHConnector().connect(
        target=target(),
        username="root",
        password=SecretStr("ssh-private"),
        verifier=verifier,
        on_host_key_verified=verified,
        timeouts=TimeoutPolicy(),
    )
    assert isinstance(session, AsyncSSHSession)
    assert captured["host"] == "8.8.8.8"
    assert captured["host_key_alias"] == "node.example.com"
    assert captured["known_hosts"] is not None
    assert captured["password"] == "ssh-private"
    assert captured["password_auth"] is True
    assert captured["public_key_auth"] is False
    assert captured["kbdint_auth"] is False
    assert captured["client_keys"] is None
    assert captured["config"] is None
    assert callback_count == 1


async def test_connector_surfaces_safe_host_key_mismatch_without_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "SHA256:" + base64.b64encode(bytes(reversed(range(32)))).decode().rstrip("=")

    async def fake_connect(host: str, **kwargs: Any) -> object:
        del host
        client = kwargs["client_factory"]()
        assert not client.validate_host_public_key(
            "node.example.com", "8.8.8.8", 22, FakeKey(other)
        )
        raise OSError("raw transport detail")

    monkeypatch.setattr(ssh_module.asyncssh, "connect", fake_connect)
    with pytest.raises(BootstrapError) as captured:
        await AsyncSSHConnector().connect(
            target=target(),
            username="root",
            password=SecretStr("ssh-private"),
            verifier=HostKeyVerifier(
                expected_fingerprint=FINGERPRINT,
                pinned_fingerprint=None,
            ),
            on_host_key_verified=lambda result: None,
            timeouts=TimeoutPolicy(),
        )
    assert captured.value.code == "ssh_host_key_changed"
    assert "raw transport detail" not in str(captured.value)
    assert "ssh-private" not in str(captured.value)


async def test_remote_session_sends_sudo_secret_only_as_channel_input() -> None:
    connection = FakeConnection()
    session = AsyncSSHSession(connection)  # type: ignore[arg-type]
    result = await session.run(
        "sudo -S -p '' true",
        stdin=SecretStr("sudo-private"),
        timeout=10,
    )
    assert result.exit_status == 0
    command, kwargs = connection.process_calls[0]
    assert "sudo-private" not in command
    assert kwargs["input"] == "sudo-private\n"


async def test_remote_session_stops_before_buffering_unbounded_output() -> None:
    connection = FakeConnection(stdout="x" * 65_537)
    session = AsyncSSHSession(connection)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError) as captured:
        await session.run("safe-command", timeout=10)
    assert captured.value.code == "remote_command_failed"
    assert connection.processes[0].closed is True


async def test_remote_upload_is_created_exclusively_with_requested_mode() -> None:
    connection = FakeConnection()
    session = AsyncSSHSession(connection)  # type: ignore[arg-type]
    await session.put(UPLOAD_PATH, b"secret", mode=0o600, timeout=10)
    path, mode, attrs = connection.sftp.open_calls[0]
    assert path == UPLOAD_PATH
    assert mode == "xb"
    assert attrs.permissions == 0o600
    assert connection.sftp.remote_file.content == b"secret"
    assert connection.sftp.chmod_calls == [(path, 0o600)]


@pytest.mark.parametrize(
    "exception_type,expected_code",
    [(TimeoutError, "remote_command_timeout"), (OSError, "remote_command_failed")],
)
async def test_remote_session_distinguishes_timeout_and_closes_failed_channel(
    monkeypatch: pytest.MonkeyPatch, exception_type: type[Exception], expected_code: str
) -> None:
    async def failed_wait_closed(self: FakeProcess) -> None:
        raise exception_type("raw-remote-secret-detail")

    monkeypatch.setattr(FakeProcess, "wait_closed", failed_wait_closed)
    connection = FakeConnection()
    session = AsyncSSHSession(connection)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError) as captured:
        await session.run("safe-command", stdin=SecretStr("sudo-private"), timeout=10)
    assert captured.value.code == expected_code
    assert "raw-remote-secret-detail" not in str(captured.value)
    assert "sudo-private" not in str(captured.value)
    assert connection.processes[0].closed
