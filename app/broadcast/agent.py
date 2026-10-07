"""Explicitly installed v2 agent; never bootstraps or upgrades a legacy node."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.broadcast.envelope import public_key
from app.broadcast.media_control import MediaHeartbeat, Observation
from app.broadcast.media_runtime import MediaPorts, MediaRuntime
from app.broadcast.models import CAPABILITIES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("keygen", "run"))
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    cipher = Fernet(os.environ["BROADCAST_LOCAL_MASTER_KEY"].encode())
    if args.action == "keygen":
        private = X25519PrivateKey.generate()
        raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        fd = os.open(args.key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(cipher.encrypt(raw))
        print(public_key(private))
        return
    if args.config is None:
        raise SystemExit("An explicit node configuration is required")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    parsed = urlsplit(config["control_url"])
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("An HTTPS control URL is required")
    private = X25519PrivateKey.from_private_bytes(cipher.decrypt(args.key_file.read_bytes()))
    runtime = MediaRuntime(
        node_id=config["node_id"],
        private_key=private,
        mediamtx=config["mediamtx"],
        ffmpeg=config["ffmpeg"],
        ffprobe=config["ffprobe"],
        directory=Path(config["runtime_directory"]),
        ports=MediaPorts(**config["ports"]),
        srt_bind_host=config["srt_bind_host"],
        rtmp_bind_host=config.get("rtmp_bind_host", "127.0.0.1"),
    )
    boot_id, sequence = secrets.token_hex(16), 0
    reset_observations = False
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            while True:
                observations = runtime.tick()
                sequence += 1
                data = MediaHeartbeat(
                    boot_id=boot_id,
                    sequence=sequence,
                    public_key=public_key(private),
                    capabilities=sorted(CAPABILITIES),
                    plan_generation=max(0, runtime.generation),
                    rtmp_port=runtime.ports.rtmp
                    if config.get("rtmp_bind_host") == "0.0.0.0"  # noqa: S104 - explicit opt-in
                    else None,
                    observations=[]
                    if reset_observations
                    else [Observation.model_validate(o) for o in observations],
                )
                try:
                    response = client.post(
                        config["control_url"].rstrip("/") + "/broadcast-agent/v2/heartbeat",
                        json=data.model_dump(),
                        headers={"Authorization": f"Bearer {os.environ['BROADCAST_NODE_TOKEN']}"},
                    )
                    if response.status_code in (401, 403):
                        raise SystemExit("Media node authorization ended")
                    if response.is_success:
                        runtime.accept(response.json())
                        reset_observations = False
                    elif response.status_code == 409:
                        reset_observations = True
                except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidTag):
                    pass  # Preserve established media if the controller is unavailable.
                time.sleep(2)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
