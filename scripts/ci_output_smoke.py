"""Real CI-only RTMP output smoke through the public HTTP API.

The script deliberately prints no ingest or destination key and never includes
captured FFmpeg output in failures. It expects the project to be running with
``compose.ci.yml`` and performs its own best-effort cleanup on every exit path.
"""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8088"
RECEIVER_SERVER = "rtmp://ci-rtmp-receiver:1935/ci-output"
RECEIVER_KEY = "ci-e2e"
RECEIVER_PATH = "ci-output/ci-e2e"
PUBLISHER_PID_FILE = "/tmp/ci-e2e-publisher.pid"  # noqa: S108 - isolated container tmpfs

COMPOSE = (
    "docker",
    "compose",
    "-p",
    "adojapan-restream",
    "--env-file",
    ".env.ci",
    "-f",
    "compose.yml",
    "-f",
    "compose.ci.yml",
)


class SmokeFailure(RuntimeError):
    """A secret-safe, actionable smoke failure."""


class APIClient:
    def __init__(self) -> None:
        self._opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf_token = ""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        csrf: bool = False,
        expected: Sequence[int] = (200,),
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if csrf:
            headers["X-CSRF-Token"] = self.csrf_token
        request = Request(  # noqa: S310 - fixed loopback HTTP origin
            BASE_URL + path, data=data, headers=headers, method=method
        )
        try:
            response = self._opener.open(request, timeout=10)
        except HTTPError as exc:
            code = "unknown"
            with suppress(Exception):
                body = json.loads(exc.read().decode("utf-8"))
                code = str(body.get("error", {}).get("code", code))
            raise SmokeFailure(f"HTTP API {method} {path} failed: {exc.code} ({code})") from None
        if response.status not in expected:
            raise SmokeFailure(
                f"HTTP API {method} {path} returned unexpected status {response.status}"
            )
        body = response.read()
        return json.loads(body) if body else None

    def login(self) -> None:
        result = self.request(
            "POST",
            "/api/auth/login",
            {"login": "ci-admin", "password": "ci-only-password"},
        )
        self.csrf_token = str(result.get("csrf_token", ""))
        if not self.csrf_token:
            raise SmokeFailure("login did not return a CSRF token")


def compose_exec(
    command: Sequence[str],
    *,
    detach: bool = False,
    environment: dict[str, str] | None = None,
    timeout: float = 30,
) -> str:
    args = [*COMPOSE, "exec", "-T"]
    if detach:
        args.append("-d")
    for key, value in (environment or {}).items():
        args.extend(("-e", f"{key}={value}"))
    args.extend(("backend", *command))
    try:
        result = subprocess.run(  # noqa: S603 - fixed Compose executable and controlled argv
            args,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure("Docker Compose command did not complete") from exc
    if result.returncode != 0:
        # stdout/stderr can contain private RTMP URLs. Never add them to the exception.
        raise SmokeFailure("command inside the backend container failed")
    return result.stdout.strip()


def wait_for(
    description: str,
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 40,
    interval: float = 0.5,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        with suppress(Exception):
            last = probe()
            if predicate(last):
                return last
        time.sleep(interval)
    raise SmokeFailure(f"timed out waiting for {description}")


def destination(client: APIClient, destination_id: int) -> dict[str, Any] | None:
    response = client.request("GET", "/api/destinations")
    return next(
        (item for item in response.get("items", ()) if int(item["id"]) == destination_id),
        None,
    )


def receiver_path() -> dict[str, Any] | None:
    source = """
import sys
import urllib.error
import urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=4).read().decode())
except urllib.error.HTTPError as exc:
    if exc.code == 404:
        print("null")
    else:
        raise
"""
    encoded_path = RECEIVER_PATH.replace("/", "%2F")
    output = compose_exec(
        (
            "python",
            "-c",
            source,
            f"http://ci-rtmp-receiver:9997/v3/paths/get/{encoded_path}",
        )
    )
    payload = json.loads(output)
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise SmokeFailure("receiver API returned an invalid path document")
    return cast(dict[str, Any], payload)


def receiver_bytes(payload: dict[str, Any] | None) -> int:
    if not payload:
        return 0
    return int(payload.get("inboundBytes", payload.get("bytesReceived", 0)) or 0)


def launch_publisher(ingest_key: str) -> None:
    script = f"""
set -eu
echo $$ > {PUBLISHER_PID_FILE}
exec ffmpeg -nostdin -hide_banner -loglevel error -re \\
  -f lavfi -i testsrc=size=320x180:rate=15 \\
  -f lavfi -i sine=frequency=440:sample_rate=44100 \\
  -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \\
  -c:a aac -f flv \\
  "rtmp://mediamtx:1935/live/${{CI_INGEST_KEY}}" \\
  >/tmp/ci-e2e-publisher.log 2>&1
"""
    compose_exec(
        ("sh", "-ec", script),
        detach=True,
        environment={"CI_INGEST_KEY": ingest_key},
    )


def probe_received_media() -> tuple[int, int]:
    output = compose_exec(
        (
            "ffprobe",
            "-v",
            "error",
            "-read_intervals",
            "%+2",
            "-count_packets",
            "-show_entries",
            "stream=codec_type,codec_name,nb_read_packets",
            "-of",
            "json",
            f"{RECEIVER_SERVER}/{RECEIVER_KEY}",
        ),
        timeout=25,
    )
    payload = json.loads(output)
    streams = payload.get("streams", ())
    video_packets = max(
        (
            int(stream.get("nb_read_packets", 0))
            for stream in streams
            if stream.get("codec_type") == "video" and stream.get("codec_name") == "h264"
        ),
        default=0,
    )
    audio_packets = max(
        (
            int(stream.get("nb_read_packets", 0))
            for stream in streams
            if stream.get("codec_type") == "audio" and stream.get("codec_name") == "aac"
        ),
        default=0,
    )
    if video_packets <= 0:
        raise SmokeFailure("receiver did not expose H.264 video packets")
    if audio_packets <= 0:
        raise SmokeFailure("receiver did not expose AAC audio packets")
    return video_packets, audio_packets


def process_snapshot() -> dict[str, Any]:
    source = f"""
import json
from pathlib import Path
pid_file = Path({PUBLISHER_PID_FILE!r})
publisher = int(pid_file.read_text().strip()) if pid_file.exists() else None
ffmpeg = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        if (entry / 'comm').read_text().strip() == 'ffmpeg':
            ffmpeg.append(int(entry.name))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
print(json.dumps({{'publisher': publisher, 'ffmpeg': sorted(ffmpeg)}}))
"""
    payload = json.loads(compose_exec(("python", "-c", source)))
    if not isinstance(payload, dict):
        raise SmokeFailure("process inspection returned an invalid document")
    return cast(dict[str, Any], payload)


def stop_publisher() -> None:
    source = f"""
import os
import signal
from pathlib import Path
path = Path({PUBLISHER_PID_FILE!r})
if path.exists():
    try:
        os.kill(int(path.read_text().strip()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass
"""
    with suppress(SmokeFailure):
        compose_exec(("python", "-c", source))


def remove_test_files() -> None:
    source = f"""
from pathlib import Path
for name in ({PUBLISHER_PID_FILE!r}, '/tmp/ci-e2e-publisher.log'):
    Path(name).unlink(missing_ok=True)
"""
    with suppress(SmokeFailure):
        compose_exec(("python", "-c", source))


def main() -> int:
    client = APIClient()
    destination_id: int | None = None
    completed = False
    try:
        client.login()
        print("E2E: authenticated through the HTTP API and obtained CSRF protection")

        ingest = client.request("GET", "/api/ingest")
        ingest_key = str(ingest.get("stream_key", ""))
        if not ingest_key:
            raise SmokeFailure("ingest configuration did not include a stream key")

        created = client.request(
            "POST",
            "/api/destinations",
            {
                "name": "CI isolated receiver",
                "server_url": RECEIVER_SERVER,
                "stream_key": RECEIVER_KEY,
                "enabled": False,
            },
            csrf=True,
            expected=(201,),
        )
        created_id = int(created["id"])
        destination_id = created_id
        client.request("POST", f"/api/destinations/{created_id}/start", csrf=True)
        print("E2E: created and started the isolated destination through the HTTP API")

        launch_publisher(ingest_key)
        wait_for(
            "active H.264/AAC ingest",
            lambda: client.request("GET", "/api/ingest/status"),
            lambda item: item.get("state") == "live",
        )
        live = wait_for(
            "destination state live",
            lambda: destination(client, created_id),
            lambda item: bool(item and item.get("state") == "live"),
            timeout=50,
        )
        if live.get("state") != "live":
            raise SmokeFailure("destination never reported confirmed media progress")

        first = wait_for(
            "receiver media bytes",
            receiver_path,
            lambda item: bool(item and item.get("ready") and receiver_bytes(item) > 0),
        )
        first_bytes = receiver_bytes(first)
        time.sleep(2)
        second = receiver_path()
        second_bytes = receiver_bytes(second)
        if second_bytes <= first_bytes:
            raise SmokeFailure("receiver media byte counter did not grow")

        video_packets, audio_packets = probe_received_media()
        print(
            "E2E: receiver confirmed H.264/AAC packets and growing media bytes "
            f"(video={video_packets}, audio={audio_packets}, bytes={second_bytes})"
        )

        client.request("POST", f"/api/destinations/{created_id}/stop", csrf=True)
        wait_for(
            "destination state stopped",
            lambda: destination(client, created_id),
            lambda item: bool(item and item.get("state") == "stopped"),
        )
        wait_for("receiver sink path removal", receiver_path, lambda item: item is None)

        snapshot = process_snapshot()
        if snapshot.get("publisher") is None or snapshot.get("ffmpeg") != [snapshot["publisher"]]:
            raise SmokeFailure("worker FFmpeg or child process remained after stop")

        stop_publisher()
        wait_for(
            "synthetic publisher termination",
            process_snapshot,
            lambda item: not item.get("ffmpeg"),
        )

        client.request(
            "DELETE",
            f"/api/destinations/{created_id}",
            csrf=True,
            expected=(204,),
        )
        wait_for(
            "destination deletion",
            lambda: destination(client, created_id),
            lambda item: item is None,
        )
        destination_id = None
        completed = True
        print("E2E: worker stopped without orphans, sink path vanished, destination deleted")
        return 0
    finally:
        if destination_id is not None and client.csrf_token:
            with suppress(Exception):
                client.request("POST", f"/api/destinations/{destination_id}/stop", csrf=True)
            with suppress(Exception):
                client.request(
                    "DELETE",
                    f"/api/destinations/{destination_id}",
                    csrf=True,
                    expected=(204,),
                )
        stop_publisher()
        with suppress(Exception):
            wait_for(
                "FFmpeg cleanup",
                process_snapshot,
                lambda item: not item.get("ffmpeg"),
                timeout=8,
            )
        remove_test_files()
        if not completed:
            print("E2E: best-effort cleanup completed", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"E2E failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
