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
DIAGNOSTIC_PATH = STATE_ROOT / "heartbeat.status"
DIAGNOSTIC_CODES = frozenset(
    {
        "accepted",
        "attempts_exhausted",
        "http_auth_rejected",
        "http_other",
        "http_payload_rejected",
        "http_protocol_conflict",
        "http_rate_limited",
        "http_server_error",
        "network_error",
        "response_invalid",
        "revoked",
        "starting",
        "token_invalid",
        "token_unreadable",
        "unexpected_root",
    }
)


def write_diagnostic(code: str) -> None:
    """Persist one constant, secret-free state without affecting the peer."""

    if code not in DIAGNOSTIC_CODES:
        return
    temporary = DIAGNOSTIC_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(f"{code}\n", encoding="ascii")
        temporary.chmod(0o600)
        temporary.replace(DIAGNOSTIC_PATH)
    except OSError:
        pass


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
        write_diagnostic("unexpected_root")
        return 2
    write_diagnostic("starting")
    try:
        token = TOKEN_PATH.read_text(encoding="ascii").strip()
    except OSError:
        write_diagnostic("token_unreadable")
        return 1
    if not token or any(character.isspace() for character in token):
        write_diagnostic("token_invalid")
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
                    write_diagnostic("response_invalid")
                    return 1
                decoded = json.loads(body)
                if not isinstance(decoded, dict) or decoded.get("status") != "ok":
                    write_diagnostic("response_invalid")
                    return 1
                (STATE_ROOT / "heartbeat.ok").write_text("ok\n", encoding="ascii")
                write_diagnostic("accepted")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                write_diagnostic("http_auth_rejected")
                (STATE_ROOT / "quiescent.ok").write_text("revoked\n", encoding="ascii")
                write_diagnostic("revoked")
                token = ""
                return 0
            if exc.code == 409:
                write_diagnostic("http_protocol_conflict")
            elif exc.code == 422:
                write_diagnostic("http_payload_rejected")
            elif exc.code == 429:
                write_diagnostic("http_rate_limited")
            elif 500 <= exc.code <= 599:
                write_diagnostic("http_server_error")
            else:
                write_diagnostic("http_other")
        except (OSError, TimeoutError, ValueError):
            write_diagnostic("network_error")
        time.sleep(5.2)
    token = ""
    write_diagnostic("attempts_exhausted")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
