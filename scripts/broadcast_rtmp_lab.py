"""Real RTMP authentication and decoded multicast through the existing media lab."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from app.broadcast.media_runtime import launch, stop
from app.broadcast.models import MediaProfile
from app.db import utc_now
from scripts.broadcast_media_lab import Lab, run_command, unused_ports


class RtmpLab(Lab):
    def diagnostics(self) -> None:
        runtime = self.runtimes["relay-a"]
        port = unused_ports(1)[0]
        failed_reader = launch(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                f"rtmp://127.0.0.1:{port}/DIAGNOSTIC_SECRET_CANARY",
                "-f",
                "null",
                "-",
            ],
            diagnostics=runtime.diagnostics.callback("probe"),
        )
        self.processes.append(failed_reader)
        assert failed_reader.wait(timeout=15) != 0

        def captured() -> bool:
            with self.database.connect() as db:
                return bool(
                    db.execute(
                        "SELECT 1 FROM broadcast_diagnostic_events WHERE code='connection_refused'"
                    ).fetchone()
                )

        self.wait(captured, seconds=20)
        with self.database.connect() as db:
            samples = db.execute("SELECT payload_json FROM broadcast_quality_history").fetchall()
            assert any(json.loads(row[0]).get("input_bitrate_bps", 0) for row in samples)
            assert "DIAGNOSTIC_SECRET_CANARY" not in "\n".join(db.iterdump())
        for path in self.directory.rglob("diagnostics.jsonl*"):
            assert "DIAGNOSTIC_SECRET_CANARY" not in path.read_text()
        self.report["diagnostics"] = {
            "status": "PASS",
            "quality_samples": len(samples),
            "real_process_error_captured": True,
            "secret_excluded": True,
        }

    def idle_ingress(self, label: str = "idle_ingress") -> None:
        def measured() -> bool:
            with self.database.connect() as db:
                row = db.execute(
                    "SELECT source_kind,video_frames,audio_packets,valid_samples "
                    "FROM broadcast_media_observations WHERE route_id=?",
                    (self.routes[0],),
                ).fetchone()
                return bool(
                    row
                    and row["source_kind"] == "direct"
                    and row["video_frames"] > 90
                    and row["audio_packets"] > 90
                    and row["valid_samples"] >= 3
                )

        self.wait(measured)
        assert self.source_process and self.source_process.poll() is None
        for runtime in self.runtimes.values():
            assert not runtime.publishers and not runtime.forwarders
            assert not runtime.plan["exports"]
            for route in runtime.plan["routes"]:
                assert not route["enabled"]
                assert route["destination"] is route["egress_lease"] is route["forward"] is None
        with self.database.connect() as db:
            assert not db.execute(
                "SELECT 1 FROM broadcast_egress_leases WHERE state='ACTIVE' AND expires_at>?",
                (utc_now(),),
            ).fetchone()
            assert not db.execute("SELECT 1 FROM broadcast_forwarding WHERE enabled=1").fetchone()
        self.report["stages"][label] = {
            "status": "PASS",
            "measured_direct_video_and_audio": True,
            "publishers": 0,
            "forwarders": 0,
            "egress_leases": 0,
        }
        for output in self.outputs:
            self.store.intent(output, True, secrets.token_hex(16))

    def landscape(self) -> None:
        for output in self.outputs:
            self.store.intent(output, False, secrets.token_hex(16))
        self.wait(
            lambda: all(not r.publishers and not r.forwarders for r in self.runtimes.values())
        )
        assert self.source_process
        stop(self.source_process)
        fixture = self.directory / "landscape.mp4"
        run_command(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(self.directory / "source.mp4"),
                "-vf",
                "transpose=1,fps=60",
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
                "120",
                "-keyint_min",
                "120",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-c:a",
                "copy",
                str(fixture),
            ],
            timeout=180,
        )
        self.fixture_profile = MediaProfile(width=1920, height=1080, fps=60, gop=120)
        # Keep the control-plane portrait template: orientation must come from media.
        self.source_process = self.phone("relay-a", fixture=fixture)
        self.processes.append(self.source_process)
        self.idle_ingress("idle_landscape")
        self.wait(
            lambda: all(self.ready(n, r) for n, r in zip(self.boot_ids, self.routes, strict=True))
        )
        self.report["stages"]["landscape_multicast"] = self.record([0, 1, 2], "landscape")

    def phone(self, node: str, *, fixture: Path | None = None) -> subprocess.Popen[str]:
        runtime = self.runtimes[node]
        secret = runtime.plan["sources"][self.source_id]
        server = f"rtmp://127.0.0.1:{runtime.ports.rtmp}/source/{self.source_id}"
        args = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(fixture or self.directory / "source.mp4"),
            "-c",
            "copy",
            "-f",
            "flv",
        ]
        bad = launch([*args, server + "/direct?user=phone&pass=synthetic-invalid"])
        self.processes.append(bad)
        assert bad.wait(timeout=15) != 0
        self.report["rtmp_invalid_key"] = "REJECTED"
        key = "direct?" + urlencode({"user": "phone", "pass": secret})
        return launch([*args, server + "/" + key])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    lab = RtmpLab(args.mediamtx, "ffmpeg", "ffprobe", args.directory)
    try:
        lab.setup(start_outputs=False)
        lab.idle_ingress()
        lab.multicast()
        lab.landscape()
        lab.diagnostics()
        lab.report["status"] = "PASS"
        lab.report["input_protocol"] = "RTMP"
    except BaseException as exc:
        lab.report["status"] = "FAIL"
        lab.report["failure"] = type(exc).__name__
        raise
    finally:
        lab.close()
        (args.directory / "report.json").write_text(json.dumps(lab.report, indent=2))
        print(json.dumps(lab.report))


if __name__ == "__main__":
    main()
