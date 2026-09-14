"""Prove relay credential ownership across real, isolated systemd Exec commands."""

from __future__ import annotations

import argparse
import errno
import importlib.machinery
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

sys.dont_write_bytecode = True

RUN_ROOT = Path("/run")
PROBE_ID = re.compile(r"[0-9a-f]{32}\Z")
CASES = ("old", "fixed")
PYTHON = "/usr/bin/python3"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"


class ProbeFailure(Exception):
    """A fixed-message failure; never include credential or subprocess output."""


def account() -> tuple[str, int, int]:
    import pwd

    try:
        selected = pwd.getpwnam("moblin-relay")
    except KeyError:
        selected = pwd.getpwnam("nobody")
    if selected.pw_uid == 0 or selected.pw_gid == 0:
        raise ProbeFailure("nonroot probe account unavailable")
    return selected.pw_name, selected.pw_uid, selected.pw_gid


def paths(probe_id: str, case: str) -> tuple[Path, Path]:
    if PROBE_ID.fullmatch(probe_id) is None or case not in CASES:
        raise ProbeFailure("invalid isolated probe identity")
    runtime = RUN_ROOT / f"relay-owner-{probe_id}-{case}"
    staging = RUN_ROOT / f"relay-owner-{probe_id}.stage"
    return runtime, staging


def require_run_root() -> None:
    entry = RUN_ROOT.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != 0 or stat.S_IMODE(entry.st_mode) & 0o022:
        raise ProbeFailure("unsafe probe runtime parent")


def write_new_file(path: Path, payload: bytes, mode: int, gid: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchown(output.fileno(), 0, gid)
        os.fchmod(output.fileno(), mode)
        output.write(payload)


def prepare(probe_id: str, case: str) -> None:
    if os.geteuid() != 0:
        raise ProbeFailure("probe preparation requires root")
    require_run_root()
    runtime, _ = paths(probe_id, case)
    _, uid, gid = account()
    if case == "fixed":
        runtime.mkdir(mode=0o750)
    entry = runtime.lstat()
    if (
        not stat.S_ISDIR(entry.st_mode)
        or entry.st_uid not in ({0, uid} if case == "old" else {0})
        or entry.st_gid not in {0, gid}
        or stat.S_IMODE(entry.st_mode) != 0o750
        or list(runtime.iterdir())
    ):
        raise ProbeFailure("unsafe initial probe directory")
    os.chown(runtime, 0, gid)
    os.chmod(runtime, 0o750)  # noqa: S103 -- service group traverses the synthetic runtime directory
    token = bytearray(secrets.token_urlsafe(32).encode("ascii"))
    try:
        write_new_file(runtime / "control-api.token", bytes(token) + b"\n", 0o640, gid)
    finally:
        token[:] = b"\0" * len(token)
    if runtime.stat().st_uid != 0 or (runtime / "control-api.token").stat().st_uid != 0:
        raise ProbeFailure("root preparation ownership failed")


def read_probe_token(normalizer: Path, token_file: Path) -> bytearray:
    loader = importlib.machinery.SourceFileLoader("_relay_ownership_normalizer", str(normalizer))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ProbeFailure("unable to load staged normalizer")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    reader = cast(Callable[[Path], bytearray], module.read_control_token)
    return reader(token_file)


def check(probe_id: str, case: str) -> None:
    runtime, staging = paths(probe_id, case)
    _, uid, gid = account()
    if os.geteuid() != uid or os.getegid() != gid:
        raise ProbeFailure("probe did not run as the service account")
    directory = runtime.lstat()
    token_file = runtime / "control-api.token"
    token_metadata = token_file.lstat()
    expected_uid = uid if case == "old" else 0
    if (
        directory.st_uid != expected_uid
        or token_metadata.st_uid != expected_uid
        or directory.st_gid != gid
        or token_metadata.st_gid != gid
        or stat.S_IMODE(directory.st_mode) != 0o750
        or stat.S_IMODE(token_metadata.st_mode) != 0o640
    ):
        raise ProbeFailure("unexpected systemd ownership result")
    accepted = False
    token = bytearray()
    try:
        token = read_probe_token(staging / "moblin-relay-normalize", token_file)
        accepted = len(token) == 43
    except ValueError:
        pass
    finally:
        token[:] = b"\0" * len(token)
    if accepted != (case == "fixed"):
        raise ProbeFailure("credential reader did not match ownership result")
    print(
        json.dumps(
            {
                "case": case,
                "euid": os.geteuid(),
                "dir_uid": directory.st_uid,
                "token_uid": token_metadata.st_uid,
                "dir_mode": "0750",
                "token_mode": "0640",
                "accepted": accepted,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def remove_known_directory(
    path: Path,
    names: set[str],
    owners: set[int],
    groups: set[int],
    *,
    allow_mountpoint: bool = False,
) -> None:
    """Delete only validated known fixture files; preserve all unexpected entries."""
    require_run_root()
    if (
        path.parent != RUN_ROOT
        or re.fullmatch(r"relay-owner-[0-9a-f]{32}(?:-(?:old|fixed)|\.stage)", path.name) is None
    ):
        raise ProbeFailure("invalid probe cleanup scope")
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid not in owners
        or before.st_gid not in groups
        or stat.S_IMODE(before.st_mode) not in {0o700, 0o750, 0o755}
    ):
        raise ProbeFailure("unsafe probe cleanup directory")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProbeFailure("probe directory changed during cleanup")
        unsafe = False
        for name in os.listdir(descriptor):
            if name not in names:
                unsafe = True
                continue
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_uid not in owners
                or entry.st_gid not in groups
                or stat.S_IMODE(entry.st_mode) not in {0o600, 0o640, 0o644}
                or entry.st_nlink != 1
            ):
                unsafe = True
                continue
            os.unlink(name, dir_fd=descriptor)
        if unsafe or os.listdir(descriptor):
            raise ProbeFailure("unexpected probe contents preserved")
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProbeFailure("probe directory changed before removal")
        try:
            path.rmdir()
        except OSError as exc:
            if not allow_mountpoint or exc.errno != errno.EBUSY or os.listdir(descriptor):
                raise
    finally:
        os.close(descriptor)


def cleanup(probe_id: str, case: str, *, allow_mountpoint: bool = False) -> None:
    if os.geteuid() != 0:
        raise ProbeFailure("probe cleanup requires root")
    runtime, _ = paths(probe_id, case)
    _, uid, gid = account()
    remove_known_directory(
        runtime,
        {"control-api.token"},
        {0, uid} if case == "old" else {0},
        {0, gid},
        allow_mountpoint=allow_mountpoint,
    )


def command(probe_id: str, case: str) -> list[str]:
    runtime, staging = paths(probe_id, case)
    username, _, gid = account()
    helper = f"{PYTHON} -I {staging}/probe.py"
    arguments = f" --probe-id {probe_id} --case {case}"
    argv = [
        SYSTEMD_RUN,
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={runtime.name}.service",
        "--property=Type=oneshot",
        f"--property=User={username}",
        f"--property=Group={gid}",
        "--property=UMask=0027",
        "--property=TimeoutStartSec=20s",
        "--property=TimeoutStopSec=5s",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=PrivateTmp=yes",
        "--property=NoNewPrivileges=yes",
        f"--property=ReadOnlyPaths=-{runtime}",
        f"--property=ExecStartPre=+{helper} --action prepare{arguments}",
        f"--property=ExecStopPost=+{helper} --action cleanup{arguments}",
    ]
    if case == "old":
        argv.extend(
            [f"--property=RuntimeDirectory={runtime.name}", "--property=RuntimeDirectoryMode=0750"]
        )
    return argv + [
        PYTHON,
        "-I",
        str(staging / "probe.py"),
        "--action",
        "check",
        "--probe-id",
        probe_id,
        "--case",
        case,
    ]


def run_command(argv: list[str], *, timeout: float = 40) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed executables and validated generated fixture paths
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def stop_probe_unit(probe_id: str, case: str) -> None:
    runtime, _ = paths(probe_id, case)
    unit = runtime.name + ".service"
    run_command([SYSTEMCTL, "stop", unit], timeout=15)
    state = run_command(
        [SYSTEMCTL, "show", "--property=LoadState", "--property=ActiveState", unit], timeout=5
    )
    properties = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
    if (
        state.returncode != 0
        or properties.get("LoadState") not in {"loaded", "not-found"}
        or properties.get("ActiveState") not in {"inactive", "failed"}
    ):
        raise ProbeFailure("isolated probe unit did not stop")


def source_bytes(path: Path) -> bytes:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ProbeFailure("probe source must be an absolute nonsymlink path")
    entry = path.lstat()
    if not stat.S_ISREG(entry.st_mode) or not 1 <= entry.st_size <= 2_000_000:
        raise ProbeFailure("invalid probe source file")
    return path.read_bytes()


def run_probe(normalizer: Path) -> None:
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise ProbeFailure("root and a running systemd manager are required")
    require_run_root()
    normalizer_source = source_bytes(normalizer)
    helper_source = source_bytes(Path(__file__).resolve(strict=True))
    probe_id = uuid.uuid4().hex
    _, staging = paths(probe_id, "fixed")
    staging.mkdir(mode=0o755)
    try:
        os.chmod(staging, 0o755)  # noqa: S103 -- only nonsecret source code is staged for the reader
        write_new_file(staging / "probe.py", helper_source, 0o644, 0)
        write_new_file(staging / "moblin-relay-normalize", normalizer_source, 0o644, 0)
        for case in CASES:
            result = run_command(command(probe_id, case))
            if result.returncode != 0:
                allowed_failures = {
                    "systemd-runtime-ownership:FAIL: " + message
                    for message in (
                        "probe preparation requires root",
                        "unsafe probe runtime parent",
                        "invalid isolated probe identity",
                        "unsafe initial probe directory",
                        "root preparation ownership failed",
                        "probe did not run as the service account",
                        "unexpected systemd ownership result",
                        "credential reader did not match ownership result",
                        "probe cleanup requires root",
                        "unsafe probe cleanup directory",
                        "unexpected probe contents preserved",
                    )
                }
                for line in result.stderr.splitlines():
                    if line in allowed_failures or re.fullmatch(
                        r"systemd-runtime-ownership:os-error:(?:prepare|check|cleanup):[0-9]{1,3}",
                        line,
                    ):
                        print(line, file=sys.stderr)
                raise ProbeFailure("isolated systemd ownership case failed")
            try:
                report = json.loads(result.stdout)
            except (ValueError, TypeError) as exc:
                raise ProbeFailure("invalid isolated ownership report") from exc
            _, uid, _ = account()
            expected_uid = uid if case == "old" else 0
            expected = {
                "case": case,
                "euid": uid,
                "dir_uid": expected_uid,
                "token_uid": expected_uid,
                "dir_mode": "0750",
                "token_mode": "0640",
                "accepted": case == "fixed",
            }
            if report != expected:
                raise ProbeFailure("isolated ownership evidence mismatch")
            runtime, _ = paths(probe_id, case)
            if (runtime / "control-api.token").exists() or (
                runtime / "control-api.token"
            ).is_symlink():
                raise ProbeFailure("systemd stop hook left a synthetic runtime credential")
            # The stop-hook namespace can retain an empty ReadOnlyPaths bind
            # mount. Remove that empty fixture from the controller namespace.
            cleanup(probe_id, case)
            if runtime.exists() or runtime.is_symlink():
                raise ProbeFailure("isolated runtime directory cleanup incomplete")
            print("systemd-runtime-ownership:PASS " + json.dumps(expected, sort_keys=True))
    finally:
        cleanup_failed = False
        for case in CASES:
            try:
                stop_probe_unit(probe_id, case)
                cleanup(probe_id, case)
            except (OSError, subprocess.SubprocessError, ProbeFailure):
                cleanup_failed = True
        if cleanup_failed:
            raise ProbeFailure("isolated probe cleanup incomplete")
        remove_known_directory(staging, {"probe.py", "moblin-relay-normalize"}, {0}, {0})
    print("systemd-runtime-ownership:cleanup:PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalizer", type=Path)
    parser.add_argument("--action", choices=("prepare", "check", "cleanup"), help=argparse.SUPPRESS)
    parser.add_argument("--probe-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=CASES, default="fixed", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.action:
            actions: dict[str, Callable[[str, str], None]] = {
                "prepare": prepare,
                "check": check,
                "cleanup": lambda probe_id, case: cleanup(probe_id, case, allow_mountpoint=True),
            }
            actions[args.action](args.probe_id, args.case)
        elif args.normalizer is not None:
            run_probe(args.normalizer)
        else:
            parser.error("--normalizer /absolute/staged/path is required")
    except ProbeFailure as exc:
        # Every ProbeFailure message is a fixed source literal, never file,
        # credential, or subprocess output.
        print("systemd-runtime-ownership:FAIL: " + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        number = exc.errno if isinstance(exc.errno, int) and 0 <= exc.errno <= 999 else 0
        action = args.action or "controller"
        print(f"systemd-runtime-ownership:os-error:{action}:{number}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, subprocess.SubprocessError):
        print("systemd-runtime-ownership:FAIL", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
