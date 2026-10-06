from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncssh
import pytest

from app.core.media_ssh import MediaSSHConfig
from app.services.media_ssh import REMOTE_COMMAND, SSHMediaTransport, SSHProcessHandle
from app.services.workers import DestinationSpec, build_ffmpeg_argv


@pytest.fixture
def config(tmp_path: Path) -> MediaSSHConfig:
    key = tmp_path / "key"
    hosts = tmp_path / "known_hosts"
    for file in (key, hosts):
        file.write_text("synthetic fixture")
        file.chmod(0o600)
    return MediaSSHConfig("8.8.8.8", "restream-media", key, hosts, "172.22.0.3", "172.22.0.1")


def test_opt_in_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_EXECUTION_MODE", raising=False)
    assert MediaSSHConfig.from_env() is None
    monkeypatch.setenv("MEDIA_EXECUTION_MODE", "sshh")
    with pytest.raises(ValueError):
        MediaSSHConfig.from_env()
    monkeypatch.setenv("MEDIA_EXECUTION_MODE", "ssh")
    with pytest.raises(ValueError, match="Incomplete"):
        MediaSSHConfig.from_env()


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "example.com"},
        {"host": "127.0.0.1"},
        {"media_address": "8.8.8.8"},
        {"auth_bind_address": "0.0.0.0"},  # noqa: S104 -- rejected wildcard fixture
        {"auth_bind_address": "::"},
        {"username": "a;command"},
        {"api_local_port": 22},
        {"api_local_port": 8000},
        {"api_local_port": 18888},
        {"private_key": Path("relative")},
    ],
)
def test_rejects_unsafe_endpoints(config: MediaSSHConfig, changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(config, **changes)


def fake_connection() -> Mock:
    connection = Mock()
    connection.is_closed.return_value = False
    connection.wait_closed = AsyncMock()
    connection.forward_local_port = AsyncMock(side_effect=lambda *args: Mock())
    connection.forward_remote_port = AsyncMock(side_effect=lambda *args: Mock())
    return connection


async def test_only_pinned_key_and_private_tunnels(
    monkeypatch: pytest.MonkeyPatch, config: MediaSSHConfig
) -> None:
    connection = fake_connection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(asyncssh, "connect", connect)
    transport = SSHMediaTransport(config)
    await transport.start()
    kwargs = connect.call_args.kwargs
    assert kwargs["known_hosts"] == str(config.known_hosts)
    assert kwargs["client_keys"] == [str(config.private_key)]
    assert kwargs["agent_path"] is None and kwargs["config"] is None
    assert connection.forward_local_port.call_args_list[0].args == (
        "127.0.0.1",
        19997,
        "172.22.0.3",
        9997,
    )
    connection.forward_remote_port.assert_awaited_once_with("172.22.0.1", 18088, "127.0.0.1", 8000)
    assert await transport.connection() is connection
    connect.assert_awaited_once()
    listeners = list(transport._listeners)
    await transport.close()
    assert all(listener.close.called for listener in listeners)
    connection.close.assert_called_once()
    with pytest.raises(RuntimeError, match="closed"):
        await transport.connection()


async def test_partial_tunnel_failure_closes_connection_and_redacts_error(
    monkeypatch: pytest.MonkeyPatch, config: MediaSSHConfig
) -> None:
    connection = fake_connection()
    connection.forward_remote_port.side_effect = OSError("remote secret MUST_NOT_LEAK")
    monkeypatch.setattr(asyncssh, "connect", AsyncMock(return_value=connection))
    transport = SSHMediaTransport(config)
    with pytest.raises(RuntimeError, match="^Pinned SSH media transport is unavailable$"):
        await transport.start()
    connection.close.assert_called_once()
    assert not transport._listeners and transport._connection is None
    with pytest.raises(RuntimeError, match="identity"):
        await transport.spawn(["ffmpeg"])


async def test_reconnect_and_encrypted_stdin_launch(
    monkeypatch: pytest.MonkeyPatch, config: MediaSSHConfig
) -> None:
    first, second = fake_connection(), fake_connection()
    monkeypatch.setattr(asyncssh, "connect", AsyncMock(side_effect=[first, second]))
    transport = SSHMediaTransport(config)
    await transport.connection()
    first.is_closed.return_value = True
    process = Mock(returncode=0)
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock(return_value=SimpleNamespace(returncode=0))
    second.create_process = AsyncMock(return_value=process)
    argv = build_ffmpeg_argv(DestinationSpec(7, "input-secret", "output-secret"))
    handle = await transport.spawn_for_destination(7, argv)
    second.create_process.assert_awaited_once_with(REMOTE_COMMAND, encoding=None)
    payload = json.loads(process.stdin.write.call_args_list[0].args[0])
    assert payload == {
        "destination_id": 7,
        "input_url": "input-secret",
        "publish_url": "output-secret",
    }
    assert handle.pid is None and await handle.wait() == 0
    first.close.assert_called_once()
    await transport.close()


async def test_cancelled_launch_closes_remote_channel(
    monkeypatch: pytest.MonkeyPatch, config: MediaSSHConfig
) -> None:
    connection = fake_connection()
    process = Mock()
    process.stdin.drain = AsyncMock(side_effect=asyncio.CancelledError)
    process.wait_closed = AsyncMock()
    connection.create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncssh, "connect", AsyncMock(return_value=connection))
    transport = SSHMediaTransport(config)
    with pytest.raises(asyncio.CancelledError):
        await transport.spawn_for_destination(7, build_ffmpeg_argv(DestinationSpec(7, "in", "out")))
    process.close.assert_called_once()
    await transport.close()


async def test_remote_lines_are_bounded_and_stop_uses_stdin() -> None:
    process = Mock(returncode=0)
    process.stdin.drain = AsyncMock()
    process.stdout.read = AsyncMock(side_effect=[b"x" * 4096, b"x\nframe=1\n", b""])
    process.wait = AsyncMock(return_value=SimpleNamespace(returncode=0))
    handle = SSHProcessHandle(process)
    assert [line async for line in handle.iter_progress()] == ["frame=1"]
    handle.terminate()
    handle.kill()
    assert [call.args[0] for call in process.stdin.write.call_args_list] == [b"TERM\n", b"KILL\n"]
    await handle.wait()
