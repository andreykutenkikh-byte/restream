#!/usr/bin/python3
"""Minimal HTTP relay heartbeat used only by the disposable CI SSH host.

Production relay code deliberately permits only the fixed HTTPS control
origin. This fixture therefore implements the smallest protocol peer needed
to prove that a freshly installed native credential reaches the test backend.
It never prints, persists, or places that credential in process arguments.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

TOKEN_PATH = Path("/etc/adojapan-relay-agent/node.token")
STATE_ROOT = Path("/run/adojapan-ci-relay-agent")
HEARTBEAT_URL = "http://backend:8000/relay-agent/v1/heartbeat"
MAX_ATTEMPTS = 120


def heartbeat_payload() -> bytes:
    return json.dumps(
        {
            "agent_version": "1.2.6",
            "protocol_version": 1,
            "hostname": "ci-native-moblin-relay",
            "relay": {
                "service_state": "inactive",
                "enabled": False,
                "main_process": "stopped",
                "srt_listener": "closed",
                "source": "NONE",
                "youtube_forward": "inactive",
                "overall": "ok",
                "youtube_url_configured": False,
                "youtube_key_configured": False,
                "healthy": True,
                "portrait_profile": True,
                "error_code": None,
            },
            "host": {
                "uptime_seconds": 100.0,
                "load_1m": 0.1,
                "cpu_percent": 2.5,
                "memory_total_bytes": 2_147_483_648,
                "memory_available_bytes": 1_073_741_824,
                "disk_total_bytes": 21_474_836_480,
                "disk_free_bytes": 10_737_418_240,
            },
            "current_command_id": None,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def main() -> int:
    get_effective_uid = cast(Callable[[], int], getattr(os, "geteuid", lambda: 0))
    if get_effective_uid() == 0:
        return 2
    try:
        token = TOKEN_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return 1
    if not token or any(character.isspace() for character in token):
        return 1
    payload = heartbeat_payload()
    for _ in range(MAX_ATTEMPTS):
        request = urllib.request.Request(  # noqa: S310 - fixed CI-only HTTP origin
            HEARTBEAT_URL,
            data=payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed CI-only HTTP origin
                request, timeout=10
            ) as response:
                body = response.read(65_537)
                if response.status != 200 or len(body) > 65_536:
                    return 1
                decoded = json.loads(body)
                if not isinstance(decoded, dict) or decoded.get("status") != "ok":
                    return 1
                (STATE_ROOT / "heartbeat.ok").write_text("ok\n", encoding="ascii")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                (STATE_ROOT / "quiescent.ok").write_text("revoked\n", encoding="ascii")
                token = ""
                return 0
        except (OSError, TimeoutError, ValueError):
            pass
        time.sleep(5.2)
    token = ""
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
