"""Stage/collect opt-in evidence in the existing disposable Compose target only."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
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


def prepare_inside() -> None:
    """Executed through stdin in the Compose-owned synthetic target, not SSH."""
    import hashlib
    import json
    import os
    import re
    import subprocess
    import sys
    import time
    from pathlib import Path

    assert sys.platform == "linux" and os.geteuid() == 0 and Path("/.dockerenv").is_file()
    raw = sys.stdin.buffer.read(192 * 1024 + 1)
    assert len(raw) <= 192 * 1024
    data = json.loads(raw)
    stage = Path("/tmp/adojapan-ci-reader-evidence")  # noqa: S108 - exclusive root0700 CI stage
    # Refuse an existing directory; do not overwrite another attempt's evidence.
    stage.mkdir(mode=0o700)
    assert stage.resolve() == stage and stage.stat().st_uid == 0
    manifest = data["manifest"]
    manifest.update(version=1, purpose="synthetic-stuck-live-reader")
    manifest["created_epoch"] = int(time.time())
    manifest["expires_epoch"] = manifest["created_epoch"] + 1800
    for name, key in (("helper.py", "helper_sha256"), ("postmortem.py", "postmortem_sha256")):
        content = data[name].encode()
        assert 0 < len(content) <= 64 * 1024
        assert hashlib.sha256(content).hexdigest() == manifest[key]
        descriptor = os.open(
            stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    descriptor = os.open(
        stage / "marker.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(manifest, stream)
    versions = {}
    for name, command, pattern in (
        ("ffmpeg", ["/usr/bin/ffmpeg", "-version"], rb"ffmpeg version ([A-Za-z0-9.:+~_-]{1,80}) "),
        (
            "mediamtx_fixture",
            ["/usr/local/lib/adojapan-ci/reader/mediamtx", "--version"],
            rb"(v[0-9.]{1,24})",
        ),
    ):
        result = subprocess.run(  # noqa: S603 - fixed two executables, version queries only
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        match = re.match(pattern, result.stdout)
        assert match is not None
        versions[name] = match[1].decode("ascii")
    print(
        "Strict reader evidence identity: "
        + json.dumps({**manifest, "versions": versions}, sort_keys=True)
    )


def run(command: tuple[str, ...], *, data: bytes | None = None, timeout: int = 45) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed CI Compose argv; no shell
        command,
        cwd=ROOT,
        input=data,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("isolated reader evidence command failed")
    return result.stdout


def payload() -> bytes:
    manifest: dict[str, Any] = {
        "source_sha": run(("git", "rev-parse", "HEAD")).decode().strip(),
        "source_tree": run(("git", "rev-parse", "HEAD^{tree}")).decode().strip(),
        "self_test_sha256": hashlib.sha256(
            (ROOT / "deploy/moblin-relay/self-test").read_bytes()
        ).hexdigest(),
    }
    value: dict[str, Any] = {"manifest": manifest}
    for destination, source, field in (
        ("helper.py", "test-native-reader-evidence.py", "helper_sha256"),
        ("postmortem.py", "test-native-reader-postmortem.py", "postmortem_sha256"),
    ):
        raw = (ROOT / "deploy/moblin-relay" / source).read_bytes()
        value[destination] = raw.decode("utf-8")
        manifest[field] = hashlib.sha256(raw).hexdigest()
    return json.dumps(value).encode()


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true" or sys.argv[1:] not in (["prepare"], ["collect"]):
        print("Strict reader evidence: isolated GitHub CI only")
        return 2
    try:
        if sys.argv[1] == "prepare":
            identifier = run((*COMPOSE, "ps", "-q", "ci-ssh-target")).decode().strip()
            if not identifier or any(ch not in "0123456789abcdef" for ch in identifier):
                raise RuntimeError("isolated target unavailable")
            # Do not emit full inspect: environment/mounts may contain CI secrets.
            config = json.loads(
                run(("docker", "inspect", "--format", "{{json .HostConfig}}", identifier))
            )
            limits = {
                key: config[key]
                for key in ("NanoCpus", "Memory", "PidsLimit", "Tmpfs", "Privileged")
            }
            if (
                limits["NanoCpus"],
                limits["Memory"],
                limits["PidsLimit"],
                limits["Privileged"],
            ) != (
                1500000000,
                1536 * 1024**2,
                512,
                False,
            ):
                raise RuntimeError("isolated target limits changed")
            print(
                "Strict reader evidence runtime limits: " + json.dumps(limits, sort_keys=True),
                flush=True,
            )
            code = inspect.getsource(prepare_inside) + "\nprepare_inside()\n"
            output = run(
                (*COMPOSE, "exec", "-T", "ci-ssh-target", "python3", "-c", code), data=payload()
            )
        else:
            output = run(
                (
                    *COMPOSE,
                    "exec",
                    "-T",
                    "ci-ssh-target",
                    "python3",
                    "-B",
                    "/tmp/adojapan-ci-reader-evidence/postmortem.py",  # noqa: S108 - own stage
                )
            )
        if len(output) > 256 * 1024:
            raise RuntimeError("isolated evidence output exceeds bound")
        print(output.decode("ascii"), end="", flush=True)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        print("Strict reader evidence collection: ERROR (original acceptance outcome unchanged)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
