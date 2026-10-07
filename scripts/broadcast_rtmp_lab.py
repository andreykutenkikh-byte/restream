"""Real RTMP authentication and decoded multicast through the existing media lab."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from app.broadcast.media_runtime import launch
from scripts.broadcast_media_lab import Lab


class RtmpLab(Lab):
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
        lab.setup()
        lab.multicast()
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
