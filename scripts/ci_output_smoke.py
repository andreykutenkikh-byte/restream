"""Real CI-only RTMP output smoke through the public HTTP API.

The script deliberately prints no ingest or destination key and never includes
captured FFmpeg output in failures. It expects the project to be running with
the base, production, and CI Compose files and performs its own best-effort
cleanup on every exit path.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8088"
RECEIVER_SERVER = "rtmp://ci-rtmp-receiver:1935/ci-output"
RECEIVER_KEY = "ci-e2e"
RECEIVER_PATH = "ci-output/ci-e2e"
PUBLISHER_PID_FILE = "/tmp/ci-e2e-publisher.pid"  # noqa: S108 - isolated container tmpfs
PUBLISHER_SERVICE = "ci-rtmp-publisher"
PREVIEW_INDEX = "/api/ingest/preview/index.m3u8"
MAX_PLAYLIST_BYTES = 1024 * 1024
MAX_SEGMENT_BYTES = 8 * 1024 * 1024
_HLS_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.(?:m3u8|ts|mp4|mp)\Z")
_HLS_URI = re.compile(r'URI="([^"]+)"')
_SAFE_HLS_QUERY = frozenset({"_HLS_msn", "_HLS_part", "_HLS_skip"})

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
    "compose.production.yml",
    "-f",
    "compose.ci.yml",
)


class SmokeFailure(RuntimeError):
    """A secret-safe, actionable smoke failure."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    """Bounded response used for playlists and media without exposing private URLs."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class PreviewProbe:
    """Verified preview bytes plus the media playlist used for sustained reads."""

    playlist_bytes: int
    segment_bytes: int
    media_playlist_path: str


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
            error_payload: Any = None
            with suppress(Exception):
                error_payload = json.loads(exc.read().decode("utf-8"))
            if exc.code in expected:
                return error_payload
            code = "unknown"
            if isinstance(error_payload, dict):
                error = error_payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code", code))
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

    def raw_request(
        self,
        path: str,
        *,
        expected: Sequence[int] = (200,),
        headers: Mapping[str, str] | None = None,
        max_bytes: int,
    ) -> RawResponse:
        request = Request(  # noqa: S310 - fixed loopback HTTP origin
            BASE_URL + path,
            headers={"Accept": "*/*", **dict(headers or {})},
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=15)
        except HTTPError as exc:
            try:
                body = exc.read(max_bytes + 1)
                response_headers = {key.lower(): value for key, value in exc.headers.items()}
            finally:
                exc.close()
            if exc.code not in expected:
                raise SmokeFailure(f"preview API returned unexpected status {exc.code}") from None
            if len(body) > max_bytes:
                raise SmokeFailure("preview API error response exceeded its size limit") from None
            return RawResponse(exc.code, response_headers, body)

        try:
            body = response.read(max_bytes + 1)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            response_status = response.status
        finally:
            response.close()
        if response_status not in expected:
            raise SmokeFailure(f"preview API returned unexpected status {response_status}")
        if len(body) > max_bytes:
            raise SmokeFailure("preview API response exceeded its size limit")
        return RawResponse(response_status, response_headers, body)


def compose_exec(
    command: Sequence[str],
    *,
    service: str = "backend",
    detach: bool = False,
    environment: dict[str, str] | None = None,
    timeout: float = 30,
) -> str:
    args = [*COMPOSE, "exec", "-T"]
    if detach:
        args.append("-d")
    for key, value in (environment or {}).items():
        args.extend(("-e", f"{key}={value}"))
    args.extend((service, *command))
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


def _host_command(command: Sequence[str], *, timeout: float = 20) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Docker executable and controlled argv
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure("Docker runtime inspection did not complete") from exc
    if result.returncode != 0:
        raise SmokeFailure("Docker runtime inspection failed")
    return result.stdout.strip()


def compose_container_id(service: str) -> str:
    container_id = _host_command((*COMPOSE, "ps", "-q", service))
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise SmokeFailure(f"{service} container ID was unavailable")
    return container_id


def assert_hls_port_is_internal() -> None:
    container_id = compose_container_id("mediamtx")
    for template in (
        "{{json .HostConfig.PortBindings}}",
        "{{json .NetworkSettings.Ports}}",
    ):
        output = _host_command(("docker", "inspect", "--format", template, container_id))
        try:
            bindings = json.loads(output)
        except json.JSONDecodeError as exc:
            raise SmokeFailure("MediaMTX port inspection returned invalid data") from exc
        if isinstance(bindings, dict) and bindings.get("8888/tcp"):
            raise SmokeFailure("MediaMTX HLS port 8888 was published on the host")


def runtime_usage_sample() -> dict[str, tuple[str, str]]:
    usage: dict[str, tuple[str, str]] = {}
    for service in ("backend", "mediamtx"):
        container_id = compose_container_id(service)
        output = _host_command(
            (
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}",
                container_id,
            ),
            timeout=30,
        )
        cpu, separator, memory = output.partition("|")
        if (
            separator != "|"
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", cpu)
            or not re.fullmatch(
                r"[0-9]+(?:\.[0-9]+)?[KMGT]?i?B / [0-9]+(?:\.[0-9]+)?[KMGT]?i?B",
                memory,
            )
        ):
            raise SmokeFailure("Docker runtime usage returned an invalid sample")
        usage[service] = (cpu, memory)
    return usage


def _public_preview_path(reference: str) -> str:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise SmokeFailure("preview playlist exposed a non-local media reference")
    if "%" in parsed.path:
        raise SmokeFailure("preview playlist exposed an encoded media path")

    path = unquote(parsed.path)
    prefix = "/api/ingest/preview/"
    if path.startswith(prefix):
        asset = path.removeprefix(prefix)
    elif path.startswith("/") or "/" in path or "\\" in path:
        raise SmokeFailure("preview playlist exposed an invalid media path")
    else:
        asset = path
    if not _HLS_ASSET.fullmatch(asset):
        raise SmokeFailure("preview playlist exposed an unsupported media asset")

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if any(
        key not in _SAFE_HLS_QUERY or len(value) > 32 or not re.fullmatch(r"[A-Za-z0-9.+-]*", value)
        for key, value in query_items
    ):
        raise SmokeFailure("preview playlist exposed an unsafe media query")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{prefix}{asset}{query}"


def _playlist_references(body: bytes) -> tuple[list[str], list[str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SmokeFailure("preview playlist was not UTF-8") from exc
    if not text.lstrip().startswith("#EXTM3U"):
        raise SmokeFailure("preview response was not an HLS playlist")

    playlists: list[str] = []
    media: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        references: list[str]
        if not line.startswith("#"):
            references = [line]
        elif line.startswith(("#EXT-X-PART:", "#EXT-X-MEDIA:", "#EXT-X-RENDITION-REPORT:")):
            references = _HLS_URI.findall(line)
        else:
            continue
        for reference in references:
            safe_path = _public_preview_path(reference)
            collection = playlists if urlsplit(safe_path).path.endswith(".m3u8") else media
            if safe_path not in collection:
                collection.append(safe_path)
    return playlists, media


def fetch_preview_segment(
    client: APIClient, ingest_key: str, *, timeout: float = 35
) -> PreviewProbe:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        playlist_path = PREVIEW_INDEX
        visited: set[str] = set()
        playlist_bytes = 0
        for _ in range(4):
            if playlist_path in visited:
                break
            visited.add(playlist_path)
            playlist = client.raw_request(
                playlist_path,
                expected=(200, 404, 409, 502, 503, 504),
                headers={"Accept": "application/vnd.apple.mpegurl"},
                max_bytes=MAX_PLAYLIST_BYTES,
            )
            if playlist.status != 200:
                break
            content_type = playlist.headers.get("content-type", "").lower()
            if "mpegurl" not in content_type:
                raise SmokeFailure("preview playlist returned an invalid content type")
            playlist_bytes += len(playlist.body)
            lower_body = playlist.body.lower()
            if (
                ingest_key.encode() in playlist.body
                or b"mediamtx" in lower_body
                or b"cookiecheck=" in lower_body
                or re.search(rb"(?:^|[?&])session=", lower_body)
            ):
                raise SmokeFailure("preview playlist exposed private upstream data")

            nested, media = _playlist_references(playlist.body)
            for segment_path in media:
                try:
                    segment = client.raw_request(
                        segment_path,
                        expected=(200, 206),
                        headers={"Accept": "video/mp4, video/mp2t"},
                        max_bytes=MAX_SEGMENT_BYTES,
                    )
                except SmokeFailure:
                    continue
                segment_type = segment.headers.get("content-type", "").lower()
                if segment.body and segment_type.startswith(("video/mp4", "video/mp2t")):
                    return PreviewProbe(
                        playlist_bytes=playlist_bytes,
                        segment_bytes=len(segment.body),
                        media_playlist_path=playlist_path,
                    )

            next_playlist = next(
                (candidate for candidate in nested if candidate not in visited),
                None,
            )
            if next_playlist is None:
                break
            playlist_path = next_playlist
        time.sleep(0.5)
    raise SmokeFailure("timed out waiting for an authenticated HLS media segment")


def fetch_preview_media_cycle(client: APIClient, playlist_path: str) -> int:
    """Fetch a current media playlist and one non-empty media object."""

    playlist = client.raw_request(
        playlist_path,
        expected=(200,),
        headers={"Accept": "application/vnd.apple.mpegurl"},
        max_bytes=MAX_PLAYLIST_BYTES,
    )
    nested, media = _playlist_references(playlist.body)
    if nested or not media:
        raise SmokeFailure("preview media playlist did not contain a media object")
    for segment_path in media:
        segment = client.raw_request(
            segment_path,
            expected=(200, 206, 404),
            headers={"Accept": "video/mp4, video/mp2t"},
            max_bytes=MAX_SEGMENT_BYTES,
        )
        if segment.status not in {200, 206}:
            continue
        segment_type = segment.headers.get("content-type", "").lower()
        if segment.body and segment_type.startswith(("video/mp4", "video/mp2t")):
            return len(segment.body)
    raise SmokeFailure("preview media playlist did not yield a current media object")


def sample_active_preview_usage(
    client: APIClient, media_playlist_path: str
) -> tuple[dict[str, tuple[str, str]], int]:
    """Measure backend/MediaMTX while authenticated preview requests remain active."""

    stop = threading.Event()
    first_cycle = threading.Event()
    failures: list[Exception] = []
    cycles = 0

    def consume() -> None:
        nonlocal cycles
        try:
            while not stop.is_set():
                fetch_preview_media_cycle(client, media_playlist_path)
                cycles += 1
                first_cycle.set()
                stop.wait(0.1)
        except Exception as exc:  # noqa: BLE001 - converted to one secret-safe failure below
            failures.append(exc)
            first_cycle.set()

    consumer = threading.Thread(target=consume, name="ci-preview-consumer", daemon=True)
    consumer.start()
    if not first_cycle.wait(timeout=10):
        stop.set()
        raise SmokeFailure("preview traffic did not start before runtime sampling")
    if failures:
        stop.set()
        raise SmokeFailure("preview traffic failed before runtime sampling") from None
    try:
        usage = runtime_usage_sample()
    finally:
        stop.set()
        consumer.join(timeout=10)
    if consumer.is_alive():
        raise SmokeFailure("preview traffic did not stop after runtime sampling")
    if failures or cycles < 1:
        raise SmokeFailure("preview traffic failed during runtime sampling") from None
    return usage, cycles


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
  -g 30 -keyint_min 30 -sc_threshold 0 \\
  -c:a aac -f flv \\
  "rtmp://mediamtx:1935/live/${{CI_INGEST_KEY}}" \\
  >/tmp/ci-e2e-publisher.log 2>&1
"""
    compose_exec(
        ("sh", "-ec", script),
        service=PUBLISHER_SERVICE,
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


def process_snapshot(*, service: str = "backend") -> dict[str, Any]:
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
    payload = json.loads(compose_exec(("python", "-c", source), service=service))
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
        compose_exec(("python", "-c", source), service=PUBLISHER_SERVICE)


def remove_test_files() -> None:
    source = f"""
from pathlib import Path
for name in ({PUBLISHER_PID_FILE!r}, '/tmp/ci-e2e-publisher.log'):
    Path(name).unlink(missing_ok=True)
"""
    with suppress(SmokeFailure):
        compose_exec(("python", "-c", source), service=PUBLISHER_SERVICE)


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

        assert_hls_port_is_internal()
        anonymous = APIClient()
        anonymous.raw_request(
            PREVIEW_INDEX,
            expected=(401,),
            max_bytes=64 * 1024,
        )
        print("E2E: preview rejected an unauthenticated request and port 8888 stayed internal")

        launch_publisher(ingest_key)
        initial_status = wait_for(
            "active H.264/AAC ingest with received bytes",
            lambda: client.request("GET", "/api/ingest/status"),
            lambda item: (
                item.get("state") == "live" and int(item.get("bytes_received", 0) or 0) > 0
            ),
        )
        initial_bytes = int(initial_status.get("bytes_received", 0) or 0)
        measured_status = wait_for(
            "growing ingest bytes and calculated bitrate",
            lambda: client.request("GET", "/api/ingest/status"),
            lambda item: (
                item.get("state") == "live"
                and int(item.get("bytes_received", 0) or 0) > initial_bytes
                and int(item.get("bitrate_bps", 0) or 0) > 0
            ),
            timeout=25,
            interval=1,
        )
        measured_bytes = int(measured_status.get("bytes_received", 0) or 0)
        measured_bitrate = int(measured_status.get("bitrate_bps", 0) or 0)
        metadata = measured_status.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SmokeFailure("ingest status returned invalid metadata")
        video_codec = str(measured_status.get("video_codec") or metadata.get("video_codec") or "")
        audio_codec = str(measured_status.get("audio_codec") or metadata.get("audio_codec") or "")
        if video_codec.lower() != "h264" or audio_codec.lower() != "aac":
            raise SmokeFailure("ingest status did not identify H.264/AAC")
        if metadata.get("bitrate_bps") != measured_status.get("bitrate_bps"):
            raise SmokeFailure("ingest bitrate fields were inconsistent")
        print(
            "E2E: ingest status confirmed H.264/AAC, growing bytes, and rolling bitrate "
            f"(bytes={measured_bytes}, bitrate_bps={measured_bitrate})"
        )

        preview = fetch_preview_segment(client, ingest_key)
        usage, preview_cycles = sample_active_preview_usage(client, preview.media_playlist_path)
        print(
            "E2E: authenticated preview returned playlist and media bytes "
            f"(playlist_bytes={preview.playlist_bytes}, segment_bytes={preview.segment_bytes})"
        )
        print(
            "E2E: isolated active-preview runtime usage "
            f"preview_cycles={preview_cycles}; "
            f"backend_cpu={usage['backend'][0]} backend_memory={usage['backend'][1]}; "
            f"mediamtx_cpu={usage['mediamtx'][0]} mediamtx_memory={usage['mediamtx'][1]}"
        )

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
        try:
            live = wait_for(
                "destination state live",
                lambda: destination(client, created_id),
                lambda item: bool(item and item.get("state") == "live"),
                timeout=50,
            )
        except SmokeFailure:
            current = destination(client, created_id) or {}
            sink = receiver_path()
            processes = process_snapshot()
            state = str(current.get("state", "unknown"))
            if state not in {
                "stopped",
                "waiting_for_input",
                "connecting",
                "live",
                "reconnecting",
                "failed",
            }:
                state = "unknown"
            raise SmokeFailure(
                "destination did not confirm live media "
                f"(state={state}, restarts={int(current.get('restart_count', 0) or 0)}, "
                f"receiver_ready={bool(sink and sink.get('ready'))}, "
                f"receiver_bytes={receiver_bytes(sink)}, "
                f"ffmpeg_processes={len(processes.get('ffmpeg', ()))})"
            ) from None
        if live.get("state") != "live":
            raise SmokeFailure("destination never reported confirmed media progress")

        first = wait_for(
            "receiver media bytes",
            receiver_path,
            lambda item: bool(item and item.get("ready") and receiver_bytes(item) > 0),
        )
        first_bytes = receiver_bytes(first)

        limited = client.request(
            "POST",
            "/api/destinations",
            {
                "name": "CI destination limit probe",
                "server_url": RECEIVER_SERVER,
                "stream_key": "ci-limit-probe",
                "enabled": False,
            },
            csrf=True,
            expected=(409,),
        )
        if not isinstance(limited, dict) or limited.get("error", {}).get("code") != (
            "destination_limit_reached"
        ):
            raise SmokeFailure("second destination did not return destination_limit_reached")
        first_after_limit = destination(client, created_id)
        if not first_after_limit or first_after_limit.get("state") != "live":
            raise SmokeFailure("first destination stopped during destination-limit probe")
        print("E2E: second destination rejected with 409 while the first remained live")

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

        backend_snapshot = process_snapshot()
        publisher_snapshot = process_snapshot(service=PUBLISHER_SERVICE)
        if backend_snapshot.get("ffmpeg"):
            raise SmokeFailure("worker FFmpeg or child process remained after stop")
        if publisher_snapshot.get("publisher") is None or publisher_snapshot.get("ffmpeg") != [
            publisher_snapshot["publisher"]
        ]:
            raise SmokeFailure("synthetic publisher process state was inconsistent")

        stop_publisher()
        wait_for(
            "synthetic publisher termination",
            lambda: process_snapshot(service=PUBLISHER_SERVICE),
            lambda item: not item.get("ffmpeg"),
        )
        offline = wait_for(
            "offline ingest with reset bitrate",
            lambda: client.request("GET", "/api/ingest/status"),
            lambda item: item.get("state") == "offline" and item.get("bitrate_bps") is None,
            timeout=30,
            interval=1,
        )
        offline_metadata = offline.get("metadata", {})
        if (
            not isinstance(offline_metadata, dict)
            or offline_metadata.get("bitrate_bps") is not None
        ):
            raise SmokeFailure("offline ingest retained a metadata bitrate")
        client.raw_request(
            PREVIEW_INDEX,
            expected=(404, 409),
            max_bytes=64 * 1024,
        )
        print("E2E: publisher stop reset bitrate and made the authenticated preview offline")

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
                "worker FFmpeg cleanup",
                process_snapshot,
                lambda item: not item.get("ffmpeg"),
                timeout=8,
            )
        with suppress(Exception):
            wait_for(
                "publisher FFmpeg cleanup",
                lambda: process_snapshot(service=PUBLISHER_SERVICE),
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
