"""Dedicated MediaMTX and isolated copy publishers, driven by fenced node leases.

MediaMTX/FFmpeg raw diagnostics are deliberately discarded: both can print stream IDs
and destination credentials. Only bounded, parsed measurements leave this process.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.broadcast.envelope import open_envelope
from app.broadcast.media_diagnostics import DiagnosticCollector, Emit, read_diagnostics
from app.broadcast.models import MediaProfile, ResourceLimits, youtube_endpoint
from app.broadcast.selector import Selector


@dataclass(frozen=True)
class MediaPorts:
    rtsp: int
    rtmp: int
    srt: int
    api: int


def input_video_format(streams: list[dict[str, Any]]) -> tuple[int, int, float] | None:
    """Qualify copy-compatible media without imposing a portrait template."""
    try:
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        width, height = int(video["width"]), int(video["height"])
        fps = float(Fraction(video.get("r_frame_rate", "0/1")))
        if (
            video["codec_name"] == "h264"
            and audio["codec_name"] == "aac"
            and 128 <= width <= 3840
            and 128 <= height <= 3840
            and 1 <= fps <= 60
        ):
            return width, height, fps
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def launch(
    argv: list[str], *, progress: bool = False, feed: bool = False, diagnostics: Emit | None = None
) -> subprocess.Popen[str]:
    process = subprocess.Popen(  # noqa: S603 - argv comes from validated typed plans, never a shell
        argv,
        stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
        stdout=subprocess.PIPE if progress or diagnostics else subprocess.DEVNULL,
        stderr=subprocess.PIPE if diagnostics else subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if diagnostics:
        assert process.stderr is not None
        read_diagnostics(process.stderr, diagnostics)
        if not progress:
            assert process.stdout is not None
            read_diagnostics(process.stdout, diagnostics)
    return process


def stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class Publisher:
    def __init__(
        self,
        argv: list[str],
        *,
        feed: bool = False,
        fps: float = 30,
        diagnostics: Emit | None = None,
        exhausted_retry_seconds: float | None = None,
    ) -> None:
        self.argv = argv
        self.feed = feed
        self.fps = fps
        self.diagnostics = diagnostics
        self.exhausted_retry_seconds = exhausted_retry_seconds
        self.selector: Selector | None = None
        self.process: subprocess.Popen[str] | None = None
        self.frames = 0
        self.time_us = 0
        self.bytes = 0
        self.last_progress = 0.0
        self.first_progress = 0.0
        self.started = 0.0
        self.failures = 0
        self.retry_at = 0.0
        self.reader: threading.Thread | None = None
        self.tick()

    def _read(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            key, _, value = line[:256].strip().partition("=")
            if self.process is not process:
                return
            try:
                number = int(value)
            except ValueError:
                continue
            if key == "frame" and number > self.frames:
                self.frames = number
                self.last_progress = time.monotonic()
                self.first_progress = self.first_progress or self.last_progress
            if key == "out_time_us" and number >= 0:
                self.time_us = number
            if key == "total_size" and number >= 0:
                self.bytes = number

    def tick(self) -> None:
        now = time.monotonic()
        if self.selector and self.selector.error and self.process:
            if self.diagnostics:
                self.diagnostics(self.selector.error, None)
            stop(self.process)
        if self.process and self.process.poll() is None:
            # A blocked pipe reader can leave FFmpeg alive forever after the
            # selector stops producing packets. tick() is called with qualified
            # input, so recover this stalled copy publisher using the same
            # bounded retry policy as an exited process.
            if self.feed and now - max(self.started, self.last_progress) > 30:
                if self.diagnostics:
                    self.diagnostics("publisher_stalled", 30)
                stop(self.process)
            else:
                if now - self.started > 60 and now - self.last_progress < 3:
                    self.failures = 0
                return
        if self.process is not None:
            if self.diagnostics:
                self.diagnostics("process_exit", self.process.poll())
            self.failures = min(5, self.failures + 1)
            delay = min(30, 2**self.failures)
            if self.failures >= 5 and self.exhausted_retry_seconds is not None:
                delay = max(delay, self.exhausted_retry_seconds)
            self.retry_at = now + delay
            self.close()
            self.process = None
        if now < self.retry_at or (self.failures >= 5 and self.exhausted_retry_seconds is None):
            return
        self.frames, self.time_us, self.last_progress, self.first_progress = 0, 0, 0, 0
        self.bytes = 0
        self.started = now
        if self.selector:
            self.selector.close()
            self.selector = None
        options = {"diagnostics": self.diagnostics} if self.diagnostics else {}
        self.process = launch(self.argv, progress=True, feed=self.feed, **options)
        if self.failures and self.diagnostics:
            self.diagnostics("publisher_retry", self.failures)
        if self.feed:
            assert self.process.stdin is not None
            self.selector = Selector(
                cast(IO[bytes], cast(io.TextIOWrapper, self.process.stdin).buffer),
                fps=self.fps,
                diagnostics=self.diagnostics,
            )
        self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.reader.start()

    @property
    def connected(self) -> bool:
        return bool(
            self.process
            and self.process.poll() is None
            and self.frames > 0
            and time.monotonic() - self.last_progress < 3
        )

    def close(self) -> None:
        if self.process:
            stop(self.process)
        if self.selector:
            self.selector.close()
            self.selector = None
        if self.process:
            if self.process.stdin:
                with suppress(OSError, ValueError):  # Child is already reaped; pipe may be broken.
                    self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
        if self.reader:
            self.reader.join(timeout=3)


class PacketProbe:
    """A persistent reader preserves a moving PTS timeline between heartbeats."""

    def __init__(
        self,
        ffprobe: str,
        url: str,
        identity: str,
        *,
        video_format: tuple[int, int, float],
        b_frames: int | None = None,
        timeout_us: int = 3_000_000,
        diagnostics: Emit | None = None,
    ) -> None:
        self.identity = identity
        self.video_format = video_format
        self.b_frames = b_frames
        self.video_pts = 0.0
        self.audio_pts = 0.0
        self.video_frames = 0
        self.audio_packets = 0
        self.bytes = 0
        self.started = time.monotonic()
        self.last_packet = 0.0
        self.first_video_packet = 0.0
        self.last_video_packet = 0.0
        self.process = launch(
            [
                ffprobe,
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                str(timeout_us),
                "-show_packets",
                "-show_entries",
                "packet=codec_type,pts_time,size",
                "-of",
                "compact",
                url,
            ],
            progress=True,
            diagnostics=diagnostics,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                fields = dict(
                    item.split("=", 1) for item in line[:512].strip().split("|")[1:] if "=" in item
                )
                pts, size = float(fields["pts_time"]), int(fields["size"])
                if fields.get("codec_type") == "video":
                    self.video_pts, self.video_frames = pts, self.video_frames + 1
                    self.last_video_packet = time.monotonic()
                    self.first_video_packet = self.first_video_packet or self.last_video_packet
                elif fields.get("codec_type") == "audio":
                    self.audio_pts, self.audio_packets = pts, self.audio_packets + 1
                self.bytes += size
                self.last_packet = time.monotonic()
            except (ValueError, KeyError):
                continue

    def observation(self) -> dict[str, Any] | None:
        if self.process.poll() is not None or not self.video_frames or not self.audio_packets:
            return None
        if time.monotonic() - self.last_packet > 3:
            return None
        return {
            "source_identity": self.identity,
            "video_pts": self.video_pts,
            "audio_pts": self.audio_pts,
            "video_frames": self.video_frames,
            "audio_packets": self.audio_packets,
            "bitrate_bps": int(self.bytes * 8 / max(0.01, time.monotonic() - self.started)),
            "input_bytes": self.bytes,
            "input_epoch": int(self.started * 1000),
            "input_age_ms": int(max(0, time.monotonic() - self.last_packet) * 1000),
            "video_width": self.video_format[0],
            "video_height": self.video_format[1],
            "video_b_frames": self.b_frames,
            "video_fps": self.video_format[2],
        }

    def close(self) -> None:
        stop(self.process)
        self.reader.join(timeout=3)
        if self.process.stdout:
            self.process.stdout.close()


class MediaRuntime:
    def __init__(
        self,
        *,
        node_id: str,
        private_key: X25519PrivateKey,
        mediamtx: str,
        ffmpeg: str,
        ffprobe: str,
        directory: Path,
        ports: MediaPorts,
        srt_bind_host: str,
        rtmp_bind_host: str = "127.0.0.1",
        test_destinations: dict[str, str] | None = None,
    ) -> None:
        self.node_id, self.private_key = node_id, private_key
        self.ffmpeg, self.ffprobe, self.ports = ffmpeg, ffprobe, ports
        self.test_destinations = test_destinations or {}
        for url in self.test_destinations.values():
            if urlsplit(url).hostname != "127.0.0.1" or urlsplit(url).scheme != "rtmp":
                raise ValueError("Synthetic sinks must be loopback RTMP")
        directory.mkdir(parents=True, exist_ok=True)
        self.diagnostics = DiagnosticCollector(directory)
        self.plan: dict[str, Any] = {"routes": [], "exports": [], "sources": {}}
        self.generation = -1
        self.issued_at = ""
        self.fence_file = directory / "accepted-lease.json"
        self.fingerprint = ""
        self.output_fences: dict[str, int] = {}
        self.restarted = False
        if self.fence_file.exists():
            prior = json.loads(self.fence_file.read_text(encoding="utf-8"))
            self.generation, self.issued_at = prior["generation"], prior["issued_at"]
            self.fingerprint = prior["fingerprint"]
            self.output_fences = prior.get("output_fences", {})
            self.restarted = True
        self.expires_at = ""
        self.local_password = secrets.token_urlsafe(32)
        self.paths: dict[str, dict[str, Any]] = {}
        self.publishers: dict[str, tuple[str, Publisher]] = {}
        self.forwarders: dict[str, tuple[str, Publisher]] = {}
        self.direct_history: dict[str, tuple[str, float, int]] = {}
        self.source_switches: dict[str, dict[str, float]] = {}
        self.last_observations: list[dict[str, Any]] = []
        self.probes: dict[str, PacketProbe] = {}
        self.egress_lock = threading.RLock()
        self.ending = threading.Event()
        runtime = self

        class AuthHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Bounded auth request required")
                    payload = json.loads(self.rfile.read(length))
                    allowed = runtime.authorize(payload)
                except (ValueError, KeyError, TypeError):
                    allowed = False
                self.send_response(200 if allowed else 403)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.auth_server = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
        self.auth_thread = threading.Thread(target=self.auth_server.serve_forever, daemon=True)
        self.auth_thread.start()
        config = {
            "logLevel": "info",  # RTMP disconnect reasons are informational; only codes persist.
            "api": True,
            "apiAddress": f"127.0.0.1:{ports.api}",
            "authMethod": "http",
            "authHTTPAddress": f"http://127.0.0.1:{self.auth_server.server_port}",
            "authHTTPExclude": [{"action": "api"}],
            "rtspAddress": f"127.0.0.1:{ports.rtsp}",
            "rtspTransports": ["tcp"],
            "rtmpAddress": f"{rtmp_bind_host}:{ports.rtmp}",
            "srtAddress": f"{srt_bind_host}:{ports.srt}",
            "hls": False,
            "webrtc": False,
            "moq": False,
            "paths": {},
        }
        config_file = directory / "mediamtx.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        if os.name != "nt":
            config_file.chmod(0o600)
        self.media = launch(
            [mediamtx, str(config_file)], diagnostics=self.diagnostics.callback("mediamtx")
        )
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{ports.api}", timeout=2, trust_env=False
        )
        for _ in range(100):
            try:
                if self.http.get("/v3/config/global/get").is_success:
                    break
            except httpx.HTTPError:
                pass
            if self.media.poll() is not None:
                self.close()
                raise RuntimeError("media_server_failed")
            time.sleep(0.05)
        else:
            self.close()
            raise RuntimeError("media_server_unavailable")
        self.expiry_thread = threading.Thread(target=self._expiry_watchdog, daemon=True)
        self.expiry_thread.start()

    def _expiry_watchdog(self) -> None:
        while not self.ending.wait(0.25):
            self.expire_egress()

    def expire_egress(self) -> None:
        with self.egress_lock:
            now = datetime.now(UTC).isoformat()
            for route in self.plan["routes"]:
                lease = route.get("egress_lease")
                if lease and lease["expires_at"] <= now:
                    route["enabled"] = False
                    route["destination"] = route["egress_lease"] = None
                    existing = self.publishers.pop(route["id"], None)
                    if existing:
                        existing[1].close()
                        existing[1].argv.clear()

    def authorize(self, request: dict[str, Any]) -> bool:
        path, user, password = request.get("path"), request.get("user"), request.get("password", "")
        if (
            request.get("ip") in {"127.0.0.1", "::1"}
            and user == "local"
            and hmac.compare_digest(password, self.local_password)
            and path in self.paths
        ):
            return request.get("action") in {"read", "publish"}
        if not self.expires_at or self.expires_at < datetime.now(UTC).isoformat():
            return False
        if request.get("action") == "publish" and user == "phone":
            return any(
                path == f"source/{sid}/direct" and hmac.compare_digest(password, secret)
                for sid, secret in self.plan["sources"].items()
            )
        if request.get("action") == "read":
            return any(
                path == export["path"]
                and user == export["route_id"]
                and hmac.compare_digest(password, export["token"])
                for export in self.plan["exports"]
            )
        return False

    def local_url(self, path: str, *, protocol: str = "rtsp") -> str:
        port = self.ports.rtsp if protocol == "rtsp" else self.ports.rtmp
        if protocol == "rtsp":
            return f"rtsp://local:{self.local_password}@127.0.0.1:{port}/{path}"
        return f"rtmp://127.0.0.1:{port}/{path}?user=local&pass={self.local_password}"

    def _path(self, name: str, config: dict[str, Any]) -> None:
        if self.paths.get(name) == config:
            return
        verb = "patch" if name in self.paths else "add"
        # Auth callbacks can race the path API call, so publish permissions first.
        self.paths[name] = config
        method = "PATCH" if verb == "patch" else "POST"
        response = self.http.request(method, f"/v3/config/paths/{verb}/{name}", json=config)
        if not response.is_success:
            raise RuntimeError("media_path_configuration_failed")

    def accept(self, envelope: dict[str, Any]) -> None:
        with self.egress_lock:
            self._accept(envelope)

    def _accept(self, envelope: dict[str, Any]) -> None:
        context = envelope["context"]
        payload = open_envelope(self.private_key, envelope, self.node_id)
        now = datetime.now(UTC).isoformat()
        if (
            context["generation"] < self.generation
            or context["expires_at"] <= now
            or context["issued_at"] < self.issued_at
            or (self.restarted and context["issued_at"] <= self.issued_at)
        ):
            raise ValueError("Stale media lease")
        lifetime = (
            datetime.fromisoformat(context["expires_at"])
            - datetime.fromisoformat(context["issued_at"])
        ).total_seconds()
        if not 0 < lifetime <= 121:
            raise ValueError("Invalid media lease lifetime")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if context["generation"] == self.generation and self.fingerprint != fingerprint:
            raise ValueError("Generation cannot change intent")
        limits = ResourceLimits.model_validate(payload["limits"])
        if len(payload["exports"]) > limits.max_forwarded_routes:
            raise ValueError("Forwarding limit exceeded")
        enabled = [r for r in payload["routes"] if r["enabled"]]
        if len(enabled) > limits.max_publishers_per_node:
            raise ValueError("Publisher limit exceeded")
        for route in payload["routes"]:
            MediaProfile.model_validate(route["profile"])
            if route["egress_generation"] < self.output_fences.get(route["output_id"], 0):
                raise ValueError("Stale egress generation")
            lease = route["egress_lease"]
            if route["enabled"] != bool(lease and route["destination"]):
                raise ValueError("Egress assignment requires a credential lease")
            if lease and (
                lease["output_id"] != route["output_id"]
                or lease["node_id"] != self.node_id
                or lease["youtube_slot"] != route["youtube_slot"]
                or lease["generation"] != route["egress_generation"]
                or lease["expires_at"] <= now
                or (datetime.fromisoformat(lease["expires_at"]) - datetime.now(UTC)).total_seconds()
                > 301
            ):
                raise ValueError("Invalid egress lease")
            if route["destination"]:
                youtube_endpoint(route["destination"]["endpoint"])
        for route in payload["routes"]:
            self.output_fences[route["output_id"]] = route["egress_generation"]
        self.plan, self.generation = payload, context["generation"]
        self.issued_at, self.expires_at = context["issued_at"], context["expires_at"]
        self.fingerprint = fingerprint
        self.restarted = False
        temporary = self.fence_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "generation": self.generation,
                    "issued_at": self.issued_at,
                    "fingerprint": fingerprint,
                    "output_fences": self.output_fences,
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self.fence_file)
        if os.name != "nt":
            self.fence_file.chmod(0o600)
        wanted = set()
        for source_id, secret in payload["sources"].items():
            path = f"source/{source_id}/direct"
            wanted.add(path)
            self._path(
                path,
                {
                    "source": "publisher",
                    "overridePublisher": False,
                    "maxReaders": limits.max_publishers_per_node + limits.max_forwarded_routes + 2,
                    "srtPublishPassphrase": secret,
                },
            )
        for export in payload["exports"]:
            wanted.add(export["path"])
            self._path(
                export["path"],
                {
                    "source": self.local_url(f"source/{export['source_id']}/direct"),
                    "rtspTransport": "tcp",
                    "sourceOnDemand": False,
                    "maxReaders": 1,
                    "srtReadPassphrase": export["passphrase"],
                },
            )
        for route in payload["routes"]:
            if route["forward"]:
                path = f"forward/{route['id']}"
                wanted.add(path)
                self._path(
                    path, {"source": "publisher", "overridePublisher": False, "maxReaders": 4}
                )
        for path in self.paths.keys() - wanted:
            if path in self.probes:
                self.probes.pop(path).close()
            self.http.delete(f"/v3/config/paths/delete/{path}")
            del self.paths[path]
        enabled_ids = {r["id"] for r in enabled}
        for route_id in self.publishers.keys() - enabled_ids:
            removed = self.publishers.pop(route_id)[1]
            removed.close()
            removed.argv.clear()
        forward_ids = {r["id"] for r in payload["routes"] if r["media_enabled"] and r["forward"]}
        for route_id in self.forwarders.keys() - forward_ids:
            self.forwarders.pop(route_id)[1].close()

    def _copy(self, source: str, destination: str, *, srt: bool = False) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-stats_period",
            "0.25",
            *(
                ["-rw_timeout", "5000000"]
                if srt
                else ["-rtsp_transport", "tcp", "-timeout", "5000000"]
            ),
            "-i",
            source,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "flv",
            destination,
        ]

    def _selector_input(self, path: str) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-rw_timeout",
            "3000000",
            "-rtmp_live",
            "live",
            "-rtmp_buffer",
            "0",
            "-probesize",
            "4194304",
            "-analyzeduration",
            "3000000",
            "-i",
            self.local_url(path, protocol="rtmp"),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-flvflags",
            "no_duration_filesize",
            "-flush_packets",
            "1",
            "-f",
            "flv",
            "pipe:1",
        ]

    def _selector_publisher(self, destination: str) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-stats_period",
            "0.25",
            "-probesize",
            "262144",
            "-analyzeduration",
            "200000",
            "-f",
            "flv",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-flvflags",
            "no_duration_filesize",
            "-flush_packets",
            "1",
            "-f",
            "flv",
            destination,
        ]

    def _worker(
        self,
        mapping: dict[str, tuple[str, Publisher]],
        route_id: str,
        identity: str,
        argv: list[str],
        *,
        feed: bool = False,
        fps: float = 30,
    ) -> Publisher:
        existing = mapping.get(route_id)
        if existing and (existing[0] != identity or (feed and existing[1].fps != fps)):
            if mapping is self.publishers:
                self.source_switches[route_id] = {
                    "last_old_progress": existing[1].last_progress,
                    "began": time.monotonic(),
                }
            existing[1].close()
            existing = None
        if existing is None:
            collector = getattr(self, "diagnostics", None)
            options = (
                {
                    "diagnostics": collector.callback(
                        "publisher" if mapping is self.publishers else "forwarder", route_id
                    )
                }
                if collector
                else {}
            )
            # A remote ingress may start long after output sending is enabled.
            # Keep its reader reconnecting at a slow rate after the fast retry
            # budget; YouTube publishers retain their finite failure budget.
            worker = Publisher(
                argv,
                feed=feed,
                fps=fps,
                exhausted_retry_seconds=60 if mapping is self.forwarders else None,
                **options,
            )
            mapping[route_id] = (identity, worker)
        else:
            worker = existing[1]
            worker.tick()
        return worker

    def probe(self, path: str) -> dict[str, Any] | None:
        try:
            response = self.http.get(f"/v3/paths/get/{path}")
            if not response.is_success or not response.json().get("ready"):
                return None
            source_identity = str(response.json().get("source", {}).get("id", path))
            existing = self.probes.get(path)
            if (
                existing
                and existing.identity == source_identity
                and existing.process.poll() is None
            ):
                return existing.observation()
            if existing:
                existing.close()
            probe = subprocess.run(  # noqa: S603 - fixed flags and loopback, authenticated path
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-timeout",
                    "3000000",
                    "-read_intervals",
                    "%+0.4",
                    "-show_packets",
                    "-show_streams",
                    "-of",
                    "json",
                    self.local_url(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=6,
                check=False,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probe.returncode:
                return None
            result = json.loads(probe.stdout)
            video_format = input_video_format(result["streams"])
            if video_format is None:
                return None
            self.probes[path] = PacketProbe(
                self.ffprobe,
                self.local_url(path),
                source_identity,
                video_format=video_format,
                b_frames=next(s for s in result["streams"] if s["codec_type"] == "video").get(
                    "has_b_frames"
                ),
                diagnostics=self.diagnostics.callback("probe"),
            )
            return None
        except (httpx.HTTPError, subprocess.TimeoutExpired, ValueError, KeyError, StopIteration):
            return None

    def tick(self) -> list[dict[str, Any]]:
        self.expire_egress()
        observations = []
        probes: dict[str, dict[str, Any] | None] = {}
        for route in self.plan["routes"]:
            route_id = route["id"]
            existing = self.publishers.get(route_id)
            worker = existing[1] if existing else None
            selector = worker.selector if worker else None
            if selector and selector.rejection and getattr(self, "diagnostics", None):
                self.diagnostics.emit("selector", route_id, selector.rejection)
            status = {
                "route_id": route_id,
                "egress_generation": route["egress_generation"],
                "egress_lease_id": route["egress_lease"]["id"] if route["egress_lease"] else None,
                "runtime_secret_present": route["destination"] is not None,
                "publisher_retries": worker.failures if worker else 0,
                "publisher_epoch": int(worker.started * 1000) if worker else None,
                "publisher_progress_age_ms": int(
                    max(0, time.monotonic() - worker.last_progress) * 1000
                )
                if worker and worker.last_progress
                else None,
                "selector_queue_packets": len(selector.queue) if selector else None,
                "selector_queue_bytes": selector.queued_bytes if selector else None,
                "publisher_running": bool(
                    existing and existing[1].process and existing[1].process.poll() is None
                ),
            }
            if not route["media_enabled"]:
                observations.append({**status, "source_kind": "unknown"})
                continue
            direct_path = f"source/{route['source_id']}/direct"
            # Warm the compressed reader concurrently with format/PTS qualification.
            # It cannot select itself: the normal probe and direct streak still gate select().
            with self.egress_lock:
                live = self.publishers.get(route_id)
                lease = route["egress_lease"]
                if (
                    live
                    and live[1].selector
                    and (
                        direct_path not in live[1].selector.inputs
                        or live[1].selector.inputs[direct_path].error
                    )
                    and lease
                    and lease["expires_at"] > datetime.now(UTC).isoformat()
                ):
                    try:
                        ready = self.http.get(f"/v3/paths/get/{direct_path}")
                        if ready.is_success and ready.json().get("ready"):
                            live[1].selector.prepare(direct_path, self._selector_input(direct_path))
                    except httpx.HTTPError:
                        pass
            if direct_path not in probes:
                probes[direct_path] = self.probe(direct_path)
            direct = probes[direct_path]
            streak = 0
            if direct:
                previous = self.direct_history.get(route_id)
                streak = (
                    previous[2] + 1
                    if previous
                    and previous[0] == direct["source_identity"]
                    and direct["video_pts"] > previous[1]
                    else 1
                )
                self.direct_history[route_id] = (
                    direct["source_identity"],
                    direct["video_pts"],
                    streak,
                )
            else:
                self.direct_history.pop(route_id, None)
            selected, source_kind, measurement = direct_path, "direct", direct
            forward = route["forward"]
            if forward and (not direct or streak < 3):
                selected, source_kind = f"forward/{route_id}", "forwarded"
                query = urlencode(
                    {
                        "streamid": f"read:{forward['path']}:{route_id}:{forward['token']}",
                        "passphrase": forward["passphrase"],
                        "pbkeylen": "32",
                        "latency": "200000",
                        "mode": "caller",
                        "maxbw": str(route["profile"]["expected_bitrate_bps"] // 8 * 2),
                    },
                    safe=":/",  # FFmpeg 5 av_find_info_tag does not percent-decode stream IDs.
                )
                source = f"srt://{forward['host']}:{forward['port']}?{query}"
                self._worker(
                    self.forwarders,
                    route_id,
                    forward["path"],
                    self._copy(source, self.local_url(selected, protocol="rtmp"), srt=True),
                )
                measurement = self.probe(selected)
            if not measurement:
                observations.append(
                    {
                        **status,
                        "source_kind": "unknown",
                        "safe_error_code": "source_lost" if route_id in self.publishers else None,
                    }
                )
                continue
            if not route["enabled"]:
                observations.append({**status, "source_kind": source_kind, **measurement})
                continue
            dest = route["destination"]
            if dest is None:
                continue
            parsed = urlsplit(dest["endpoint"])
            destination = (
                self.test_destinations.get(route["output_id"] + ":" + route["youtube_slot"])
                or self.test_destinations.get(route["output_id"])
                or urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        f"{parsed.path}/{quote(dest['stream_key'], safe='')}",
                        parsed.query,
                        "",
                    )
                )
            )
            with self.egress_lock:
                lease = route["egress_lease"]
                if not lease or lease["expires_at"] <= datetime.now(UTC).isoformat():
                    continue
                worker = self._worker(
                    self.publishers,
                    route_id,
                    route["source_id"] + destination,
                    self._selector_publisher(destination),
                    feed=True,
                    fps=self.probes[selected].video_format[2],
                )
                selector = worker.selector
                if selector:
                    selector.prepare(selected, self._selector_input(selected))
                    selector.select(selected)
                    # Telemetry reports the committed input, never merely the requested one.
                    if selector.selected and selector.selected != selected:
                        source_kind = (
                            "forwarded" if selector.selected.startswith("forward/") else "direct"
                        )
                        measurement = self.probe(selector.selected)
                    elif not selector.selected:
                        measurement = None
                    if selector.events:
                        self.source_switches[route_id] = {
                            "gap_ms": float(selector.events[-1]["video_gap_ms"])
                        }
            if not measurement:
                observations.append({**status, "source_kind": "unknown"})
                continue
            if source_kind == "direct" and route_id in self.forwarders and worker.connected:
                self.forwarders.pop(route_id)[1].close()
            switch = self.source_switches.get(route_id)
            if switch and "last_old_progress" in switch and worker.first_progress:
                switch["gap_ms"] = 1000 * (worker.first_progress - switch["last_old_progress"])
            observation = {
                **status,
                "source_kind": source_kind,
                **measurement,
                "publisher_frames": worker.frames,
                "publisher_time_us": worker.time_us,
                "publisher_connected": worker.connected,
                "publisher_running": bool(worker.process and worker.process.poll() is None),
                "publisher_bytes": worker.bytes,
                "source_switch_gap_ms": max(0, switch["gap_ms"])
                if switch and "gap_ms" in switch
                else None,
                "egress_generation": lease["generation"],
                "egress_lease_id": lease["id"],
            }
            if worker.failures >= 5:
                observation["safe_error_code"] = "retry_exhausted"
            elif worker.failures:
                observation["safe_error_code"] = "publisher_failed"
            observations.append(observation)
        self.last_observations = observations
        return observations

    def close(self) -> None:
        self.ending.set()
        if hasattr(self, "expiry_thread"):
            self.expiry_thread.join(timeout=4)
        for probe in self.probes.values():
            probe.close()
        self.probes.clear()
        for _, worker in [*self.publishers.values(), *self.forwarders.values()]:
            worker.close()
        self.publishers.clear()
        self.forwarders.clear()
        self.plan = {"routes": [], "exports": [], "sources": {}}
        if hasattr(self, "media"):
            stop(self.media)
        self.auth_server.shutdown()
        self.auth_server.server_close()
        self.auth_thread.join(timeout=3)
        if hasattr(self, "http"):
            self.http.close()
        if hasattr(self, "diagnostics"):
            self.diagnostics.close()
