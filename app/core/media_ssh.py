"""Explicit configuration for the optional, pinned SSH media transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MediaSSHConfig:
    host: str
    username: str
    private_key: Path
    known_hosts: Path
    media_address: str
    auth_bind_address: str
    port: int = 22
    api_local_port: int = 19997
    hls_local_port: int = 18888
    auth_remote_port: int = 18088

    def __post_init__(self) -> None:
        # Use operator-selected addresses, never DNS learned from a node or request.
        if not ip_address(self.host).is_global:
            raise ValueError("MEDIA_SSH_HOST must be a public IP address")
        for value in (self.media_address, self.auth_bind_address):
            address = ip_address(value)
            if not address.is_private or address.is_unspecified or address.is_multicast:
                raise ValueError("SSH media endpoints must use explicit private addresses")
        if not self.username or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in self.username
        ):
            raise ValueError("Invalid media SSH username")
        for path in (self.private_key, self.known_hosts):
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise ValueError("Media SSH key and known_hosts must be mounted regular files")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise ValueError("Media SSH key and known_hosts must be private")
        if not 1 <= self.port <= 65535:
            raise ValueError("Invalid media SSH port")
        for port in (self.api_local_port, self.hls_local_port, self.auth_remote_port):
            if not 1024 <= port <= 65535:
                raise ValueError("Invalid media SSH port")
        if self.api_local_port == self.hls_local_port or 8000 in (
            self.api_local_port,
            self.hls_local_port,
        ):
            raise ValueError("Media tunnel ports must not overlap")

    @classmethod
    def from_env(cls) -> MediaSSHConfig | None:
        mode = os.getenv("MEDIA_EXECUTION_MODE", "local")
        if mode == "local":
            return None
        if mode != "ssh":
            raise ValueError("MEDIA_EXECUTION_MODE must be local or ssh")
        try:
            return cls(
                host=os.environ["MEDIA_SSH_HOST"],
                username=os.environ["MEDIA_SSH_USER"],
                private_key=Path(os.environ["MEDIA_SSH_KEY_FILE"]),
                known_hosts=Path(os.environ["MEDIA_SSH_KNOWN_HOSTS_FILE"]),
                media_address=os.environ["MEDIA_SSH_MEDIAMTX_ADDRESS"],
                auth_bind_address=os.environ["MEDIA_SSH_AUTH_BIND_ADDRESS"],
                port=int(os.getenv("MEDIA_SSH_PORT", "22")),
            )
        except (KeyError, ValueError):
            raise ValueError("Incomplete or invalid SSH media configuration") from None
