"""Dedicated MediaMTX and isolated copy publishers, driven by fenced node leases.

MediaMTX/FFmpeg raw diagnostics are deliberately discarded: both can print stream IDs
and destination credentials. Only bounded, parsed measurements leave this process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.broadcast.envelope import open_envelope
from app.broadcast.models import MediaProfile, ResourceLimits, youtube_endpoint


@dataclass(frozen=True)
class MediaPorts:
    rtsp: int
    rtmp: int
    srt: int
    api: int


def launch(argv: list[str], *, progress: bool = False) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - argv comes from validated typed plans, never a shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if progress else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class Publisher:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.process: subprocess.Popen[str] | None = None
        self.frames = 0
        self.time_us = 0
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

    def tick(self) -> None:
        now = time.monotonic()
        if self.process and self.process.poll() is None:
            if now - self.started > 60 and now - self.last_progress < 3:
                self.failures = 0
            return
        if self.process is not None:
            self.failures += 1
            self.retry_at = now + min(30, 2**self.failures)
            self.process = None
        if now < self.retry_at or self.failures >= 5:
            return
        self.frames, self.time_us, self.last_progress, self.first_progress = 0, 0, 0, 0
        self.started = now
        self.process = launch(self.argv, progress=True)
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
            if self.process.stdout:
                self.process.stdout.close()
        if self.reader:
            self.reader.join(timeout=3)


class PacketProbe:
    """A persistent reader preserves a moving PTS timeline between heartbeats."""

    def __init__(self, ffprobe: str, url: str, identity: str) -> None:
        self.identity = identity
        self.video_pts = 0.0
        self.audio_pts = 0.0
        self.video_frames = 0
        self.audio_packets = 0
        self.bytes = 0
        self.started = time.monotonic()
        self.last_packet = 0.0
        self.process = launch(
            [
                ffprobe,
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "3000000",
                "-show_packets",
                "-show_entries",
                "packet=codec_type,pts_time,size",
                "-of",
                "compact",
                url,
            ],
            progress=True,
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
        test_destinations: dict[str, str] | None = None,
    ) -> None:
        self.node_id, self.private_key = node_id, private_key
        self.ffmpeg, self.ffprobe, self.ports = ffmpeg, ffprobe, ports
        self.test_destinations = test_destinations or {}
        for url in self.test_destinations.values():
            if urlsplit(url).hostname != "127.0.0.1" or urlsplit(url).scheme != "rtmp":
                raise ValueError("Synthetic sinks must be loopback RTMP")
        directory.mkdir(parents=True, exist_ok=True)
        self.plan: dict[str, Any] = {"routes": [], "exports": [], "sources": {}}
        self.generation = -1
        self.issued_at = ""
        self.fence_file = directory / "accepted-lease.json"
        self.fingerprint = ""
        if self.fence_file.exists():
            prior = json.loads(self.fence_file.read_text(encoding="utf-8"))
            self.generation, self.issued_at = prior["generation"], prior["issued_at"]
            self.fingerprint = prior["fingerprint"]
        self.expires_at = ""
        self.local_password = secrets.token_urlsafe(32)
        self.paths: dict[str, dict[str, Any]] = {}
        self.publishers: dict[str, tuple[str, Publisher]] = {}
        self.forwarders: dict[str, tuple[str, Publisher]] = {}
        self.direct_history: dict[str, tuple[str, float, int]] = {}
        self.source_switches: dict[str, dict[str, float]] = {}
        self.last_observations: list[dict[str, Any]] = []
        self.probes: dict[str, PacketProbe] = {}
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
            "logLevel": "error",
            "api": True,
            "apiAddress": f"127.0.0.1:{ports.api}",
            "authMethod": "http",
            "authHTTPAddress": f"http://127.0.0.1:{self.auth_server.server_port}",
            "authHTTPExclude": [{"action": "api"}],
            "rtspAddress": f"127.0.0.1:{ports.rtsp}",
            "rtspTransports": ["tcp"],
            "rtmpAddress": f"127.0.0.1:{ports.rtmp}",
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
        self.media = launch([mediamtx, str(config_file)])
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
        context = envelope["context"]
        payload = open_envelope(self.private_key, envelope, self.node_id)
        now = datetime.now(UTC).isoformat()
        if (
            context["generation"] < self.generation
            or context["expires_at"] <= now
            or context["issued_at"] < self.issued_at
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
            if route["destination"]:
                youtube_endpoint(route["destination"]["endpoint"])
        self.plan, self.generation = payload, context["generation"]
        self.issued_at, self.expires_at = context["issued_at"], context["expires_at"]
        self.fingerprint = fingerprint
        temporary = self.fence_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "generation": self.generation,
                    "issued_at": self.issued_at,
                    "fingerprint": fingerprint,
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self.fence_file)
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
            self.publishers.pop(route_id)[1].close()
        forward_ids = {r["id"] for r in enabled if r["forward"]}
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

    def _worker(
        self,
        mapping: dict[str, tuple[str, Publisher]],
        route_id: str,
        identity: str,
        argv: list[str],
    ) -> Publisher:
        existing = mapping.get(route_id)
        if existing and existing[0] != identity:
            if mapping is self.publishers:
                self.source_switches[route_id] = {
                    "last_old_progress": existing[1].last_progress,
                    "began": time.monotonic(),
                }
            existing[1].close()
            existing = None
        if existing is None:
            worker = Publisher(argv)
            mapping[route_id] = (identity, worker)
        else:
            worker = existing[1]
            worker.tick()
        return worker

    def probe(self, path: str, profile: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self.http.get(f"/v3/paths/get/{path}")
            if not response.is_success or not response.json().get("ready"):
                return None
            source_identity = str(response.json().get("source", {}).get("id", path))
            existing = self.probes.get(path)
            if existing and existing.identity == source_identity:
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
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if probe.returncode:
                return None
            result = json.loads(probe.stdout)
            video = next(s for s in result["streams"] if s["codec_type"] == "video")
            audio = next(s for s in result["streams"] if s["codec_type"] == "audio")
            if (
                video["codec_name"] != "h264"
                or audio["codec_name"] != "aac"
                or video["width"] != profile["width"]
                or video["height"] != profile["height"]
            ):
                return None
            numerator, denominator = video.get("r_frame_rate", "0/1").split("/")
            if (
                float(denominator) == 0
                or abs(float(numerator) / float(denominator) - profile["fps"]) > 0.1
            ):
                return None
            self.probes[path] = PacketProbe(self.ffprobe, self.local_url(path), source_identity)
            return None
        except (httpx.HTTPError, subprocess.TimeoutExpired, ValueError, KeyError, StopIteration):
            return None

    def tick(self) -> list[dict[str, Any]]:
        observations = []
        probes: dict[str, dict[str, Any] | None] = {}
        for route in self.plan["routes"]:
            route_id = route["id"]
            if not route["enabled"]:
                observations.append({"route_id": route_id, "source_kind": "unknown"})
                continue
            direct_path = f"source/{route['source_id']}/direct"
            if direct_path not in probes:
                probes[direct_path] = self.probe(direct_path, route["profile"])
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
                    }
                )
                source = f"srt://{forward['host']}:{forward['port']}?{query}"
                self._worker(
                    self.forwarders,
                    route_id,
                    forward["path"],
                    self._copy(source, self.local_url(selected, protocol="rtmp"), srt=True),
                )
                measurement = self.probe(selected, route["profile"])
            if not measurement:
                observations.append(
                    {
                        "route_id": route_id,
                        "source_kind": "unknown",
                        "safe_error_code": "source_lost" if route_id in self.publishers else None,
                    }
                )
                continue
            dest = route["destination"]
            parsed = urlsplit(dest["endpoint"])
            destination = self.test_destinations.get(route["output_id"]) or urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"{parsed.path}/{quote(dest['stream_key'], safe='')}",
                    parsed.query,
                    "",
                )
            )
            worker = self._worker(
                self.publishers,
                route_id,
                selected + destination,
                self._copy(self.local_url(selected), destination),
            )
            if source_kind == "direct" and route_id in self.forwarders and worker.connected:
                self.forwarders.pop(route_id)[1].close()
            switch = self.source_switches.get(route_id)
            if switch and worker.first_progress:
                switch["gap_ms"] = 1000 * (worker.first_progress - switch["last_old_progress"])
            observation = {
                "route_id": route_id,
                "source_kind": source_kind,
                **measurement,
                "publisher_frames": worker.frames,
                "publisher_time_us": worker.time_us,
                "publisher_connected": worker.connected,
            }
            if worker.failures >= 5:
                observation["safe_error_code"] = "retry_exhausted"
            observations.append(observation)
        self.last_observations = observations
        return observations

    def close(self) -> None:
        for probe in self.probes.values():
            probe.close()
        self.probes.clear()
        for _, worker in [*self.publishers.values(), *self.forwarders.values()]:
            worker.close()
        self.publishers.clear()
        self.forwarders.clear()
        if hasattr(self, "media"):
            stop(self.media)
        self.auth_server.shutdown()
        self.auth_server.server_close()
        self.auth_thread.join(timeout=3)
        if hasattr(self, "http"):
            self.http.close()
