"""Run the worker exclusively on a permission-restricted Unix socket."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import uvicorn

from bootstrap_worker.api import WorkerSettings


def prepare_socket_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("BOOTSTRAP_SOCKET_PATH must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("BOOTSTRAP_SOCKET_PATH parent must be a regular directory")
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("BOOTSTRAP_SOCKET_PATH exists and is not a socket")
        effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if metadata.st_uid != effective_uid:
            raise ValueError("BOOTSTRAP_SOCKET_PATH is owned by another user")
        path.unlink()
    return str(path)


def main() -> None:
    settings = WorkerSettings.from_environment()
    socket_path = prepare_socket_path(settings.socket_path)
    previous_umask = os.umask(0o077)
    try:
        uvicorn.run(
            "bootstrap_worker.main:app",
            uds=socket_path,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            log_level="info",
            timeout_graceful_shutdown=70,
        )
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    main()
