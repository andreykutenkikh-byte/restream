#!/usr/bin/python3
"""One CI-only diagnostic prefix of the real native self-test, not acceptance."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

STAGED = {
    "self-test": Path("/tmp/adojapan-ci-clock-self-test.py"),  # noqa: S108
    "normalizer.py": Path("/tmp/adojapan-ci-startup-normalizer.py"),  # noqa: S108
    "wrapper.py": Path("/tmp/adojapan-ci-startup-wrapper.py"),  # noqa: S108
    "renderer.py": Path("/tmp/adojapan-ci-startup-renderer.py"),  # noqa: S108
    "reader.py": Path("/tmp/adojapan-ci-startup-reader.py"),  # noqa: S108
    "slate.txt": Path("/tmp/adojapan-ci-startup-slate.txt"),  # noqa: S108
}
PURPOSE = "adojapan-ci-native-startup-diagnostic"
STOP = "CI_DIAGNOSTIC_INITIAL_PREFIX_FINISHED"
WORK_SECONDS = 240


class DiagnosticFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise DiagnosticFailure(code)


def read_private(path, *, maximum=512 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == 0
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600
            and 0 < before.st_size <= maximum,
            "UNSAFE_STAGED_FILE",
        )
        value = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        require(
            len(value) == before.st_size
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            "STAGED_FILE_CHANGED",
        )
    return value


def save_new(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def renderer_for_stage(data, stage):
    """Change only the private renderer's asset location, retaining source bytes."""
    require(
        re.fullmatch(r"/tmp/adojapan-ci-startup-[A-Za-z0-9_-]{8,64}", str(stage)),  # noqa: S108
        "PRIVATE_RENDERER_STAGE_PATH",
    )
    original = b'SLATE_FILE = "/var/lib/moblin-relay/slate.mp4"'
    require(data.count(original) == 1, "PRIVATE_RENDERER_SLATE_ANCHOR_CHANGED")
    replacement = ("SLATE_FILE = " + repr(str(stage / "slate.mp4"))).encode("ascii")
    return data.replace(original, replacement, 1)


def stage_sources(stage):
    hashes = {}
    for name, source in STAGED.items():
        data = read_private(source)
        hashes[name] = hashlib.sha256(data).hexdigest()
        if name == "renderer.py":
            save_new(stage / "renderer-source.py", data)
            data = renderer_for_stage(data, stage)
            hashes["renderer-staged.py"] = hashlib.sha256(data).hexdigest()
        save_new(stage / name, data, 0o755 if name == "wrapper.py" else 0o600)
    manifest = {
        "version": 1,
        "purpose": PURPOSE,
        "normalizer_sha256": hashes["normalizer.py"],
        "wrapper_sha256": hashes["wrapper.py"],
    }
    save_new(stage / "manifest.json", json.dumps(manifest).encode("ascii"))
    return hashes


def slate_command(stage):
    # Same installer profile and source text, directed only into our private stage.
    return [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x111827:s=1080x1920:r=30:d=12",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-vf",
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"textfile={stage}/slate.txt:fontcolor=white:fontsize=72:line_spacing=28:"
        "x=(w-text_w)/2:y=(h-text_h)/2",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-profile:v",
        "main",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-b:v",
        "8M",
        "-minrate",
        "8M",
        "-maxrate",
        "8M",
        "-bufsize",
        "16M",
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1:filler=1:bframes=0",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-t",
        "12",
        "-shortest",
        "-movflags",
        "+faststart",
        str(stage / "slate.mp4"),
    ]


def install_prefix(api, stage, mediamtx, stage_file):
    """Keep real startup gates/cleanup; stop only after the original LIVE checks."""
    api.update(
        NORMALIZER=stage / "wrapper.py",
        RENDERER=stage / "renderer.py",
        SLATE=stage / "slate.mp4",
        MEDIAMTX=mediamtx,
        RESULT_FILE=stage / "prefix-result.json",
        SELF_TEST_PROGRESS_FILE=stage / "prefix-progress.json",
        SELF_TEST_STAGE_FILE=str(stage_file),
    )
    original_mark = api["mark_self_test_stage"]
    original_write_configs = api["write_configs"]
    state = {"initial_completed": False, "last_stage": "startup"}

    def write_configs(*args, **kwargs):
        # Keep /tmp noexec: the fixed interpreter reads this private script.
        wrapper = str(stage / "wrapper.py")
        require(
            re.fullmatch(r"/tmp/adojapan-ci-startup-[A-Za-z0-9_-]{8,64}/wrapper\.py", wrapper),  # noqa: S108
            "PRIVATE_HOOK_STAGE_PATH",
        )
        sink_path, dut_path = original_write_configs(*args, **kwargs)
        config = json.loads(read_private(dut_path))
        ingest = config["paths"][api["INGEST_PATH"]]
        require(ingest.get("runOnAvailable") == wrapper, "PRIVATE_HOOK_ANCHOR_CHANGED")
        ingest["runOnAvailable"] = "/usr/bin/python3 " + wrapper
        api["atomic_json"](dut_path, config)
        return sink_path, dut_path

    def no_stale_work():
        # Never delete residues from the preceding acceptance attempt.
        api["validate_test_root"]()
        if any(api["TEST_ROOT"].glob(".run-*")):
            raise api["TestFailure"]("PRIOR_NATIVE_WORKDIR_REMAINS")
        return 0

    def mark(name, *, strict_segment_index=None):
        original_mark(name, strict_segment_index=strict_segment_index)
        # Only fixed original stage names are recorded, never exception strings.
        state["last_stage"] = name
        if name == "auth-exclusive" and not state["initial_completed"]:
            state["initial_completed"] = True
            raise api["TestFailure"](STOP)

    api["cleanup_stale_workdirs"] = no_stale_work
    api["mark_self_test_stage"] = mark
    api["write_configs"] = write_configs
    return state


def numeric_startup(value):
    keys = (
        "elapsed_ms",
        "spawn_ms",
        "post_spawn_ms",
        "reads",
        "failed",
        "absent",
        "present",
        "growth",
        "video_frames",
        "first_output_ms",
        "first_growth_ms",
        "max_read_ms",
    )
    source = value if isinstance(value, dict) else {}
    return {
        key: item if type(item := source.get(key)) is int and 0 <= item <= 10**9 else None
        for key in keys
    }


def prefix_summary(result, progress, state, code):
    require(isinstance(result, dict) and isinstance(progress, dict), "PREFIX_RESULT_SHAPE")
    clean = (
        result.get("workdir_removed") is True
        and not result.get("cleanup_failure")
        and type(result.get("secret_configs_wiped")) is int
        and (
            result["secret_configs_wiped"] >= 1
            or (
                result["secret_configs_wiped"] == 0
                and not state["initial_completed"]
                and state.get("last_stage") in {"startup", "assets"}
            )
        )
    )
    stopped_as_planned = code == 1 and state["initial_completed"] and result.get("failure") == STOP
    return {
        "status": "PREFIX_OBSERVED_NO_STARTUP_FAILURE"
        if stopped_as_planned and clean
        else "ORIGINAL_PREFIX_FAILURE",
        "acceptance": False,
        "attempts": 1,
        "original_exit": code,
        "first_start_timeout": progress.get("failure_initial_live_reason")
        == "output-start-timeout",
        "startup": numeric_startup(progress.get("failure_startup")),
        "cleanup_passed": clean,
        "media_oracle_or_deadline_changed": False,
        "diagnostic_log_level_variant": True,
    }


def failure_location(progress, allowed_stages):
    """Keep the original fixed checkpoint and source lines, never exception text."""
    source = progress if type(progress) is dict else {}
    stage = source.get("stage")
    lines = source.get("failure_lines")
    return {
        "stage": stage if type(stage) is str and stage in allowed_stages else None,
        "failure_lines": lines
        if type(lines) is list
        and len(lines) <= 8
        and all(type(line) is int and 1 <= line <= 20000 for line in lines)
        else [],
    }


def main():
    require(
        os.environ.get("CI_NATIVE_STARTUP") == "isolated-fixture"
        and sys.platform == "linux"
        and os.geteuid() == 0
        and Path("/.dockerenv").is_file(),
        "ISOLATED_LINUX_CI_REQUIRED",
    )
    os.umask(0o077)
    os.environ.pop("MOBLIN_RELAY_SELF_TEST_STAGE_FILE", None)
    # Keep private evidence if original cleanup failed; CI container teardown owns
    # the residue. Never remove code still referenced by an unreaped child.
    with contextlib.nullcontext(tempfile.mkdtemp(prefix="adojapan-ci-startup-")) as temporary:
        stage = Path(temporary)
        hashes = stage_sources(stage)
        require(
            stage.parent == Path("/tmp")  # noqa: S108 - unique root-private mkdtemp
            and re.fullmatch(r"adojapan-ci-startup-[A-Za-z0-9_-]{8,64}", stage.name),
            "PRIVATE_STAGE_PATH",
        )
        stage_noexec = bool(os.statvfs(stage).f_flag & os.ST_NOEXEC)
        # Verified image artifact, independent of successful native installation.
        reader = runpy.run_path(str(stage / "reader.py"), run_name="_startup_reader_verifier")
        reader["verify_reader_binary"]()
        version = subprocess.run(  # noqa: S603 - fixed local binary, no credentials
            ["/usr/bin/ffmpeg", "-version"],
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout.splitlines()[0]
        match = re.match(rb"ffmpeg version ([A-Za-z0-9.:+~_-]{1,80}) ", version)
        require(match is not None, "FFMPEG_VERSION_UNAVAILABLE")
        slate = subprocess.run(  # noqa: S603 - generated private asset only
            slate_command(stage),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        require(slate.returncode == 0, "SLATE_GENERATION_FAILED")
        namespace = runpy.run_path(str(stage / "self-test"), run_name="_startup_prefix")
        api = namespace["main"].__globals__
        stage_file = Path(f"/run/moblin-relay-self-test.{uuid.uuid4()}.stage")
        save_new(stage_file, b"startup\n")
        stage_identity = stage_file.stat()
        state = install_prefix(api, stage, reader["MEDIAMTX"], stage_file)
        sys.argv = [str(stage / "self-test")]
        # Never expose the original diagnostic log or synthetic credentials.
        try:
            with (
                open(os.devnull, "w") as silent,
                contextlib.redirect_stdout(silent),
                contextlib.redirect_stderr(silent),
            ):
                code = namespace["main"]()
        finally:
            current = stage_file.lstat()
            require(
                (current.st_dev, current.st_ino) == (stage_identity.st_dev, stage_identity.st_ino)
                and stat.S_ISREG(current.st_mode)
                and current.st_nlink == 1,
                "STAGE_IDENTITY_CHANGED",
            )
            stage_file.unlink()
        result = json.loads(read_private(stage / "prefix-result.json", maximum=2 * 1024**2))
        progress = json.loads(read_private(stage / "prefix-progress.json", maximum=2 * 1024**2))
        report = prefix_summary(result, progress, state, code)
        report["stage_noexec"] = stage_noexec
        report["hook_launch"] = "python-interpreter-noexec-compatible"
        report["checkpoint"] = failure_location(progress, api["SELF_TEST_STAGES"])
        report["source_hashes"] = hashes
        report["ffmpeg_version"] = match[1].decode("ascii")
        if (stage / "normalizer-phases.json").exists():
            wrapper = runpy.run_path(str(stage / "wrapper.py"), run_name="_startup_phase_validator")
            phases = json.loads(
                read_private(stage / "normalizer-phases.json", maximum=4096),
                object_pairs_hook=wrapper["unique_object"],
            )
            report["first_child_phases"] = wrapper["validated_report"](phases)
        else:
            report["first_child_phases"] = None
        report["phase_evidence_available"] = report["first_child_phases"] is not None
        report["startup_input"] = api["safe_startup_input_failure"](
            progress.get("failure_startup_input")
        )
    report["private_stage_removed"] = False
    if report["cleanup_passed"]:
        require(
            stage.resolve(strict=True) == stage and not stage.is_symlink(), "STAGE_IDENTITY_CHANGED"
        )
        shutil.rmtree(stage)
        report["private_stage_removed"] = True
    print(
        json.dumps({"native_startup_diagnostic": report}, allow_nan=False, separators=(",", ":")),
        flush=True,
    )
    return (
        0
        if report["status"] == "PREFIX_OBSERVED_NO_STARTUP_FAILURE"
        and report["phase_evidence_available"]
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        code = str(error) if isinstance(error, DiagnosticFailure) else type(error).__name__
        print(
            json.dumps(
                {
                    "native_startup_diagnostic": {
                        "status": "SETUP_OR_COLLECTION_FAILURE",
                        "code": code,
                    }
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
