from __future__ import annotations

import asyncio
import json
from pathlib import Path

import asyncssh
import pytest

from app.core.media_ssh import MediaSSHConfig
from app.services.media_ssh import REMOTE_COMMAND, SSHMediaTransport
from app.services.workers import DestinationSpec, build_ffmpeg_argv


class TestSSHServer(asyncssh.SSHServer):
    __test__ = False

    def __init__(self, client_key: asyncssh.SSHKey):
        self.client_key = client_key.convert_to_public()

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return username == "restream-media" and key == self.client_key

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return dest_host == "127.0.0.1"

    def server_requested(self, listen_host, listen_port):
        return listen_host == "127.0.0.1"


async def test_real_ssh_pinned_identity_tunnels_and_worker_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_file, hosts_file = tmp_path / "key", tmp_path / "known_hosts"
    client_key.write_private_key(key_file)
    key_file.chmod(0o600)
    received = []

    async def worker(process):
        assert process.command == REMOTE_COMMAND
        received.append(json.loads(await process.stdin.readline()))
        process.stdout.write(b"out_time_us=12345\nprogress=continue\n")
        while True:
            line = await process.stdin.readline()
            if line == b"PING\n":
                continue
            assert line == b"TERM\n"
            process.exit(0)
            return

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: TestSSHServer(client_key),
        server_host_keys=[host_key],
        process_factory=worker,
        encoding=None,
    )
    port = server.get_port()
    hosts_file.write_bytes(f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key())
    hosts_file.chmod(0o600)
    original_connect = asyncssh.connect

    async def connect_local(host, ssh_port, **kwargs):
        # Production still requires an operator-pinned public IP. Only this test
        # substitutes the in-process SSH server while retaining all auth options.
        assert host == "8.8.8.8" and ssh_port == port
        return await original_connect("127.0.0.1", port, **kwargs)

    monkeypatch.setattr(asyncssh, "connect", connect_local)
    # Reserve available listener numbers; release before the SSH forwards bind.
    reservations = [
        await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0) for _ in range(3)
    ]
    local_ports = [s.sockets[0].getsockname()[1] for s in reservations]
    for reservation in reservations:
        reservation.close()
        await reservation.wait_closed()
    config = MediaSSHConfig(
        "8.8.8.8",
        "restream-media",
        key_file,
        hosts_file,
        "127.0.0.1",
        "127.0.0.1",
        port=port,
        api_local_port=local_ports[0],
        hls_local_port=local_ports[1],
        auth_remote_port=local_ports[2],
    )
    transport = SSHMediaTransport(config)
    echo_servers = []

    async def echo(reader, writer):
        try:
            writer.write(await reader.readline())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        for endpoint in (9997, 8888, 8000):
            echo_servers.append(await asyncio.start_server(echo, "127.0.0.1", endpoint))
        await transport.start()
        for local_port in local_ports:
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"synthetic-api-hls-auth\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 5) == b"synthetic-api-hls-auth\n"
            writer.close()
            await writer.wait_closed()
        process = await transport.spawn_for_destination(
            42, build_ffmpeg_argv(DestinationSpec(42, "synthetic-input", "synthetic-output"))
        )
        progress = process.iter_progress()
        assert await asyncio.wait_for(anext(progress), 5) == "out_time_us=12345"
        assert process.pid is None
        process.terminate()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert received == [
            {
                "destination_id": 42,
                "input_url": "synthetic-input",
                "publish_url": "synthetic-output",
            }
        ]
        await transport.close()
        # A changed host key fails closed even though the endpoint is reachable.
        hosts_file.write_bytes(
            f"[127.0.0.1]:{port} ".encode()
            + asyncssh.generate_private_key("ssh-ed25519").export_public_key()
        )
        rejected = SSHMediaTransport(config)
        with pytest.raises(RuntimeError, match="Pinned SSH media transport is unavailable"):
            await rejected.start()
        await rejected.close()
    finally:
        await transport.close()
        for echo_server in echo_servers:
            echo_server.close()
            await echo_server.wait_closed()
        server.close()
        await server.wait_closed()
