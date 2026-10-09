"""Read-only OBS v5 output counters; never start, stop or change an output.

Configure a dedicated source-scoped monitoring token from the Restream panel.
The explicit output name selects Aitum Vertical separately from the main stream.
OBS credentials stay local and are requested without echo; only counters leave OBS.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect


def authentication(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def request(peer: Any, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in ("GetOutputList", "GetOutputStatus"):
        raise ValueError("read_only_request_required")
    identity = secrets.token_hex(16)
    peer.send(
        json.dumps(
            {"op": 6, "d": {"requestType": kind, "requestId": identity, "requestData": data or {}}}
        )
    )
    while True:
        reply = json.loads(peer.recv(timeout=5))
        value = reply.get("d", {})
        if reply.get("op") == 7 and value.get("requestId") == identity:
            if not value["requestStatus"]["result"]:
                raise ValueError("obs_output_unavailable")
            return dict(value["responseData"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Restream OBS monitor")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=4455)
    parser.add_argument("--list-outputs", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Invalid OBS port")
    with connect(f"ws://127.0.0.1:{args.port}", open_timeout=5, max_size=65536) as peer:
        hello = json.loads(peer.recv(timeout=5))["d"]
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = authentication(
                getpass.getpass("OBS WebSocket password (hidden): "),
                auth["salt"],
                auth["challenge"],
            )
        peer.send(json.dumps({"op": 1, "d": identify}))
        if json.loads(peer.recv(timeout=5))["op"] != 2:
            raise SystemExit("OBS authentication failed")
        outputs = request(peer, "GetOutputList")["outputs"]
        if args.list_outputs:
            for output in outputs:
                print(output["outputName"])
            return
        if not args.config:
            raise SystemExit("A source-scoped monitoring configuration is required")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        endpoint = urlsplit(config["endpoint"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path != "/obs-monitor/v1/sample"
        ):
            raise SystemExit("An HTTPS monitoring endpoint is required")
        if config["output_name"] not in [output["outputName"] for output in outputs]:
            raise SystemExit("Output name does not exist; use --list-outputs")
        sequence, boot = 0, secrets.token_hex(16)
        prior: dict[str, Any] = {}
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            while True:
                status = request(peer, "GetOutputStatus", {"outputName": config["output_name"]})
                if prior and (
                    status["outputDuration"] < prior["outputDuration"]
                    or status["outputActive"] != prior["outputActive"]
                ):
                    boot = secrets.token_hex(16)
                sequence += 1
                payload = {
                    "boot_id": boot,
                    "sequence": sequence,
                    "active": status["outputActive"],
                    "reconnecting": status["outputReconnecting"],
                    "duration_ms": status["outputDuration"],
                    "total_frames": status["outputTotalFrames"],
                    "dropped_frames": status["outputSkippedFrames"],
                    "bytes_sent": status["outputBytes"],
                    "congestion": status.get("outputCongestion"),
                }
                response = client.post(
                    config["endpoint"],
                    json=payload,
                    headers={"Authorization": "Bearer " + config["token"]},
                )
                if response.status_code in (401, 403):
                    raise SystemExit("Monitoring access revoked")
                if not response.is_success:
                    print("Monitoring update unavailable; counters remain unknown in the panel")
                prior = status
                time.sleep(2)


if __name__ == "__main__":
    main()
