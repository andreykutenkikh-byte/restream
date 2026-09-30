"""Loopback-only three-relay media acceptance with real FFmpeg/MediaMTX processes."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import SecretStr

from app.broadcast.envelope import public_key
from app.broadcast.media_control import MediaControl, MediaHeartbeat, MediaNodeEnable, Observation
from app.broadcast.media_runtime import MediaPorts, MediaRuntime, launch, stop
from app.broadcast.models import (
    CAPABILITIES,
    MediaProfile,
    OutputCreate,
    ResourceLimits,
    SessionCreate,
)
from app.broadcast.store import BroadcastStore
from app.core.security import generate_master_key
from app.db import Database, utc_now


def run_command(argv: list[str], *, timeout: int = 90) -> str:
    result = subprocess.run(  # noqa: S603 - caller constructs validated local fixture argv
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("synthetic_media_command_failed")
    return result.stdout


def unused_ports(count: int) -> list[int]:
    listeners = []
    try:
        for _ in range(count):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return [int(s.getsockname()[1]) for s in listeners]
    finally:
        for listener in listeners:
            listener.close()


class Lab:
    def __init__(self, mediamtx: str, ffmpeg: str, ffprobe: str, directory: Path) -> None:
        self.mediamtx, self.ffmpeg, self.ffprobe, self.directory = (
            mediamtx,
            ffmpeg,
            ffprobe,
            directory,
        )
        directory.mkdir(parents=True, exist_ok=True)
        self.report: dict[str, Any] = {
            "production_mutations": False,
            "real_youtube": "NOT_RUN",
            "mediamtx": run_command([mediamtx, "--version"]).strip(),
            "ffmpeg": run_command([ffmpeg, "-version"]).splitlines()[0],
            "stages": {},
        }
        self.database = Database(directory / "control.sqlite")
        self.database.migrate()
        self.store = BroadcastStore(self.database, generate_master_key())
        self.control = MediaControl(self.store, test_loopback=True)
        self.runtimes: dict[str, MediaRuntime] = {}
        self.keys: dict[str, X25519PrivateKey] = {}
        self.sequence: dict[str, int] = {}
        self.processes: list[subprocess.Popen[str]] = []
        self.outputs: list[str] = []
        self.routes: list[str] = []
        self.ports = unused_ports(16)
        self.sink_api, self.sink_rtmp, self.sink_rtsp = self.ports[12:15]
        self.sink_secrets = {str(i): secrets.token_urlsafe(24) for i in range(5)}
        self.reader_secret = secrets.token_urlsafe(24)
        self.session_id = ""
        self.source_id = ""
        self.source_process: subprocess.Popen[str] | None = None
        self.boot_ids = {node: secrets.token_hex(16) for node in ("relay-a", "relay-b", "relay-c")}

    def setup(self) -> None:
        now = utc_now()
        with self.database.connect() as db:
            for node in self.boot_ids:
                db.execute(
                    "INSERT INTO restream_nodes(id,display_name,address,resolved_ip,ssh_port,"
                    "ssh_username,status,created_at,updated_at) VALUES (?,?,?,'127.0.0.1',22,"
                    "'synthetic','ready',?,?)",
                    (node, node, f"{node}.example", now, now),
                )
        self.session_id = self.store.create_session(
            SessionCreate(
                name="Synthetic portrait source",
                ingress_node_id="relay-a",
                profile=MediaProfile(expected_bitrate_bps=6_000_000),
            ),
            secrets.token_hex(16),
        )
        for i, node in enumerate(self.boot_ids):
            output = self.store.create_output(
                self.session_id,
                OutputCreate(
                    name=f"Synthetic {node}",
                    node_id=node,
                    primary_url="rtmps://a.rtmps.youtube.com/live2",
                    backup_url="rtmps://b.rtmps.youtube.com/live2?backup=1",
                    stream_key=SecretStr(f"synthetic-independent-{i}"),
                ),
                secrets.token_hex(16),
            )
            self.outputs.append(output)
        snap = self.store.snapshot()["sessions"][0]
        self.source_id = snap["source_id"]
        route_map = {o["id"]: o["routes"][0]["id"] for o in snap["outputs"]}
        self.routes = [route_map[output] for output in self.outputs]
        self._sink()
        for i, node in enumerate(self.boot_ids):
            key = X25519PrivateKey.generate()
            ports = MediaPorts(*self.ports[i * 4 : i * 4 + 4])
            self.keys[node], self.sequence[node] = key, 0
            self.control.enable(
                node,
                MediaNodeEnable(
                    public_key=public_key(key),
                    srt_host="127.0.0.1",
                    srt_port=ports.srt,
                    limits=ResourceLimits(),
                ),
            )
            self.runtimes[node] = MediaRuntime(
                node_id=node,
                private_key=key,
                mediamtx=self.mediamtx,
                ffmpeg=self.ffmpeg,
                ffprobe=self.ffprobe,
                directory=self.directory / node,
                ports=ports,
                srt_bind_host="127.0.0.1",
                test_destinations={
                    output: self.destination(str(j)) for j, output in enumerate(self.outputs)
                },
            )
            self.exchange(node, [])
        fixture = self.directory / "source.mp4"
        run_command(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1080x1920:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=997:sample_rate=48000",
                "-t",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                "3000k",
                "-maxrate",
                "3000k",
                "-bufsize",
                "6000k",
                "-g",
                "60",
                "-keyint_min",
                "60",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                str(fixture),
            ],
            timeout=180,
        )
        self.source_process = self.phone("relay-a")
        self.processes.append(self.source_process)
        for output in self.outputs:
            self.store.intent(output, True, secrets.token_hex(16))

    def _sink(self) -> None:
        permissions = []
        users = [{"user": "any", "ips": ["127.0.0.1"], "permissions": [{"action": "api"}]}]
        for name, secret in self.sink_secrets.items():
            users.append(
                {
                    "user": f"sink{name}",
                    "pass": secret,
                    "ips": ["127.0.0.1"],
                    "permissions": [{"action": "publish", "path": f"out/{name}"}],
                }
            )
            permissions.append({"action": "read", "path": f"out/{name}"})
        users.append(
            {
                "user": "reader",
                "pass": self.reader_secret,
                "ips": ["127.0.0.1"],
                "permissions": permissions,
            }
        )
        config = {
            "logLevel": "error",
            "api": True,
            "apiAddress": f"127.0.0.1:{self.sink_api}",
            "rtspAddress": f"127.0.0.1:{self.sink_rtsp}",
            "rtspTransports": ["tcp"],
            "rtmpAddress": f"127.0.0.1:{self.sink_rtmp}",
            "srt": False,
            "hls": False,
            "webrtc": False,
            "moq": False,
            "authInternalUsers": users,
            "paths": {f"out/{i}": {} for i in self.sink_secrets},
        }
        path = self.directory / "synthetic-sink.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        process = launch([self.mediamtx, str(path)])
        self.processes.append(process)
        for _ in range(100):
            try:
                if httpx.get(
                    f"http://127.0.0.1:{self.sink_api}/v3/config/global/get", timeout=1
                ).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError("synthetic_sink_unavailable")

    def destination(self, name: str) -> str:
        return f"rtmp://127.0.0.1:{self.sink_rtmp}/out/{name}?user=sink{name}&pass={self.sink_secrets[name]}"

    def phone(self, node: str) -> subprocess.Popen[str]:
        runtime = self.runtimes[node]
        secret = runtime.plan["sources"][self.source_id]
        query = urlencode(
            {
                "streamid": f"publish:source/{self.source_id}/direct:phone:{secret}",
                "passphrase": secret,
                "pbkeylen": "32",
                "latency": "200000",
            },
            safe=":/",  # Compatible with FFmpeg 5 and newer SRT URL parsers.
        )
        return launch(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                str(self.directory / "source.mp4"),
                "-c",
                "copy",
                "-f",
                "mpegts",
                f"srt://127.0.0.1:{runtime.ports.srt}?{query}",
            ]
        )

    def exchange(self, node: str, observations: list[dict[str, Any]]) -> None:
        runtime = self.runtimes[node]
        self.sequence[node] += 1
        envelope = self.control.heartbeat(
            node,
            MediaHeartbeat(
                boot_id=self.boot_ids[node],
                public_key=public_key(self.keys[node]),
                capabilities=sorted(CAPABILITIES),
                sequence=self.sequence[node],
                plan_generation=max(0, runtime.generation),
                observations=[Observation.model_validate(o) for o in observations],
            ),
        )
        runtime.accept(envelope)

    def step(self) -> None:
        for node, runtime in self.runtimes.items():
            self.exchange(node, runtime.tick())
        time.sleep(0.4)

    def wait(self, predicate: Any, *, seconds: int = 90) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.step()
            if predicate():
                return
        raise RuntimeError("media_readiness_timeout")

    def ready(self, node: str, route: str) -> bool:
        worker = self.runtimes[node].publishers.get(route)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT valid_samples FROM broadcast_media_observations WHERE route_id=?", (route,)
            ).fetchone()
        return bool(worker and worker[1].connected and row and row[0] >= 2)

    def record(self, indices: list[int], label: str) -> dict[str, Any]:
        jobs = []
        for index in indices:
            path = self.directory / f"{label}-{index}.mkv"
            source = f"rtsp://reader:{self.reader_secret}@127.0.0.1:{self.sink_rtsp}/out/{index}"
            jobs.append(
                (
                    path,
                    launch(
                        [
                            self.ffmpeg,
                            "-hide_banner",
                            "-nostdin",
                            "-loglevel",
                            "error",
                            "-y",
                            "-rtsp_transport",
                            "tcp",
                            "-i",
                            source,
                            "-t",
                            "6",
                            "-c",
                            "copy",
                            str(path),
                        ]
                    ),
                )
            )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and any(p.poll() is None for _, p in jobs):
            self.step()
        for _, process in jobs:
            if process.poll() is None:
                stop(process)
                raise RuntimeError("recording_timeout")
            if process.returncode:
                raise RuntimeError("recording_failed")
        reports = [self.inspect(path) for path, _ in jobs]
        hashes = [set(report.pop("frame_hashes")) for report in reports]
        common = len(set.intersection(*hashes))
        if len(indices) > 1 and common < 90:
            raise RuntimeError("shared_timeline_not_proven")
        return {"outputs": reports, "common_decoded_video_frames": common}

    def inspect(self, path: Path) -> dict[str, Any]:
        data = json.loads(
            run_command(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_packets",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
        assert (video["codec_name"], audio["codec_name"], video["width"], video["height"]) == (
            "h264",
            "aac",
            1080,
            1920,
        )
        assert int(video["nb_read_frames"]) >= 90
        assert video["avg_frame_rate"] == "30/1"
        gaps = {}
        for kind in ("video", "audio"):
            packets = [p for p in data["packets"] if p["codec_type"] == kind]
            assert len(packets) >= 90
            for field in ("pts_time", "dts_time"):
                values = [float(p[field]) for p in packets if field in p]
                assert len(values) >= 90 and all(
                    b >= a for a, b in zip(values, values[1:], strict=False)
                )
            pts = [float(p["pts_time"]) for p in packets]
            gaps[kind] = max(b - a for a, b in zip(pts, pts[1:], strict=False))
            assert gaps[kind] < 0.1
        keys = [
            i
            for i, p in enumerate(p for p in data["packets"] if p["codec_type"] == "video")
            if "K" in p["flags"]
        ]
        assert len(keys) >= 2 and max(b - a for a, b in zip(keys, keys[1:], strict=False)) <= 60
        run_command([self.ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])
        hashes = run_command(
            [self.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"]
        )
        return {
            "frames": int(video["nb_read_frames"]),
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "max_packet_gap_seconds": gaps,
            "decode": "PASS",
            "gop_max": 60,
            "frame_hashes": [
                line.rsplit(",", 1)[-1].strip()
                for line in hashes.splitlines()
                if not line.startswith("#")
            ],
        }

    def multicast(self) -> None:
        self.wait(
            lambda: all(
                self.ready(node, route)
                for node, route in zip(self.boot_ids, self.routes, strict=True)
            )
        )
        self.report["stages"]["multicast"] = self.record([0, 1, 2], "multicast")
        a = self.runtimes["relay-a"].publishers[self.routes[0]][1]
        c = self.runtimes["relay-c"].publishers[self.routes[2]][1]
        assert a.process and c.process and self.source_process
        pids = (a.process.pid, c.process.pid, self.source_process.pid)
        counts = (a.frames, c.frames)
        self.store.intent(self.outputs[1], False, secrets.token_hex(16))
        self.wait(
            lambda: (
                self.routes[1] not in self.runtimes["relay-b"].publishers
                and a.frames > counts[0] + 90
                and c.frames > counts[1] + 90
            )
        )
        assert (a.process.pid, c.process.pid, self.source_process.pid) == pids
        assert self.source_process.poll() is None
        self.store.intent(self.outputs[1], True, secrets.token_hex(16))
        self.wait(lambda: self.ready("relay-b", self.routes[1]))
        bad = self.runtimes["relay-b"]
        old_destination = bad.test_destinations[self.outputs[1]]
        bad.test_destinations[self.outputs[1]] = f"rtmp://127.0.0.1:{self.sink_rtmp}/out/forbidden"
        counts = (a.frames, c.frames)
        self.wait(lambda: a.frames > counts[0] + 120 and c.frames > counts[1] + 120)
        assert not bad.publishers[self.routes[1]][1].connected
        assert (a.process.pid, c.process.pid, self.source_process.pid) == pids
        bad.test_destinations[self.outputs[1]] = old_destination
        self.wait(lambda: self.ready("relay-b", self.routes[1]))
        self.report["stages"]["failure_isolation"] = {
            "status": "PASS",
            "source_pid_preserved": True,
            "other_publisher_pids_preserved": True,
            "unaffected_frames_during_failure": [a.frames - counts[0], c.frames - counts[1]],
            "independent_stop_restart": "PASS",
            "destination_failure_reconnect": "PASS",
        }

    def close(self) -> None:
        for runtime in self.runtimes.values():
            runtime.close()
        for process in reversed(self.processes):
            stop(process)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    lab = Lab(args.mediamtx, args.ffmpeg, args.ffprobe, args.directory)
    try:
        lab.setup()
        lab.multicast()
        lab.report["status"] = "PASS"
    except BaseException as exc:
        lab.report["status"] = "FAIL"
        lab.report["failure"] = type(exc).__name__
        lab.report["observations"] = {
            node: runtime.last_observations for node, runtime in lab.runtimes.items()
        }
        raise
    finally:
        lab.close()
        (args.directory / "report.json").write_text(
            json.dumps(lab.report, indent=2), encoding="utf-8"
        )
        print(json.dumps(lab.report))


if __name__ == "__main__":
    main()
