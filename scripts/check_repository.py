"""Fail CI when repository-wide isolation and domain invariants are violated."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
from pathlib import Path

IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backups",
    "build",
    "data",
    "dist",
    "logs",
}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

FORBIDDEN = {
    "incorrect production domain": "rea" + "stream.adojapan.ru",
    "host networking": "network_mode:" + " host",
    "privileged containers": "privileged:" + " true",
    "Docker socket mount": "/var/run/" + "docker.sock",
    "global system prune": "docker system" + " prune",
    "global volume prune": "docker volume" + " prune",
    "global network prune": "docker network" + " prune",
}

RUNTIME_POLICY_DIRECTORIES = {
    ".github",
    "app",
    "bootstrap_worker",
    "ci",
    "deploy",
    "node_agent",
    "relay_agent",
    "scripts",
}
RUNTIME_POLICY_ROOT_FILES = {
    "Dockerfile",
    "Dockerfile.bootstrap",
    "Dockerfile.node",
    "compose.yml",
    "compose.production.yml",
    "compose.ci.yml",
}
DIRECT_FIREWALL_TOOL = re.compile(
    r"(?<![A-Za-z0-9_-])(ufw|iptables|ip6tables|nft|firewall-cmd)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
DIRECT_SELINUX_TOOL = re.compile(
    r"(?<![A-Za-z0-9_-])(setenforce|chcon|restorecon)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
DOCKER_DAEMON_FIREWALL_MARKERS = {
    "/etc/docker/" + "daemon.json",
    "/etc/default/" + "docker",
    "/etc/sysconfig/" + "docker",
    "/etc/systemd/system/" + "docker.service.d",
    "firewall-" + "backend",
    "--ip" + "tables",
    "--ip6" + "tables",
}
SELINUX_CONFIGURATION_MARKERS = {"/etc/selinux/" + "config"}
PRIVATE_RUNTIME_SUFFIXES = {".age", ".db", ".sqlite", ".sqlite3"}
PRIVATE_DATABASE_SIDECAR_PATTERN = re.compile(
    r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$",
    re.IGNORECASE,
)
PRIVATE_AGE_TEMP_PATTERN = re.compile(
    r"\.age[.-](?:tmp|temp|part|partial)(?:[.-].*)?$",
    re.IGNORECASE,
)
PEM_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN(?: [A-Z0-9_-]+)* PRIVATE KEY-----",
    re.IGNORECASE,
)
AGE_SECRET_IDENTITY_PATTERN = re.compile(
    rb"(?<![A-Z0-9-])AGE-SECRET-KEY-1[0-9A-Z]+",
    re.IGNORECASE,
)
GIT_PEM_PRIVATE_KEY_PATTERN = r"-----BEGIN( [A-Z0-9_-]+)* PRIVATE KEY-----"
GIT_AGE_IDENTITY_PATTERN = r"(^|[^A-Z0-9-])AGE-SECRET-KEY-1[0-9A-Z]+"
GIT_INSPECTION_TIMEOUT_SECONDS = 15
GIT_INSPECTION_ERROR = "Git index inspection failed; repository policy cannot continue safely"


class GitInspectionError(RuntimeError):
    """Raised when Git cannot prove the state of the repository index."""


def _run_git(
    root: Path,
    arguments: list[str],
    *,
    allowed_returncodes: frozenset[int],
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Git operation; root is a separate argv
            ["git", "-C", str(root), *arguments],  # noqa: S607
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_INSPECTION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitInspectionError from exc
    if result.returncode not in allowed_returncodes:
        raise GitInspectionError
    return result


def _tracked_files(root: Path) -> set[Path]:
    result = _run_git(
        root,
        ["ls-files", "--cached", "-z", "--"],
        allowed_returncodes=frozenset({0}),
    )
    return {
        root / Path(os.fsdecode(raw_path)) for raw_path in result.stdout.split(b"\0") if raw_path
    }


def _tracked_secret_files(root: Path, pattern: str) -> set[Path]:
    result = _run_git(
        root,
        ["grep", "--cached", "-a", "-i", "-l", "-z", "-E", "-e", pattern, "--"],
        allowed_returncodes=frozenset({0, 1}),
    )
    if result.returncode == 1:
        return set()
    return {
        root / Path(os.fsdecode(raw_path)) for raw_path in result.stdout.split(b"\0") if raw_path
    }


def _git_path_is_ignored(root: Path, relative: Path) -> bool:
    result = _run_git(
        root,
        ["check-ignore", "--quiet", "--no-index", "--", relative.as_posix()],
        allowed_returncodes=frozenset({0, 1}),
    )
    return result.returncode == 0


def _candidate_files(root: Path, tracked: set[Path]) -> list[Path]:
    discovered = {
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts)
    }
    return sorted(discovered | tracked, key=lambda path: path.as_posix())


def _is_runtime_environment(path: Path) -> bool:
    folded_name = path.name.casefold()
    return folded_name == ".env" or (
        folded_name.startswith(".env.") and path.name != ".env.example"
    )


def _is_private_runtime_artifact(path: Path) -> bool:
    return (
        path.suffix.casefold() in PRIVATE_RUNTIME_SUFFIXES
        or PRIVATE_DATABASE_SIDECAR_PATTERN.search(path.name) is not None
        or PRIVATE_AGE_TEMP_PATTERN.search(path.name) is not None
    )


def _is_runtime_policy_path(path: Path) -> bool:
    return path.name in RUNTIME_POLICY_ROOT_FILES or (
        bool(path.parts) and path.parts[0] in RUNTIME_POLICY_DIRECTORIES
    )


def _function_source(text: str, name: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            if node.end_lineno is None:
                return ""
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    return ""


def check(root: Path) -> list[str]:
    errors: list[str] = []
    git_repository = (root / ".git").exists()
    tracked: set[Path] = set()
    tracked_pem_private_keys: set[Path] = set()
    tracked_age_secret_identities: set[Path] = set()
    if git_repository:
        try:
            tracked = _tracked_files(root)
            tracked_pem_private_keys = _tracked_secret_files(root, GIT_PEM_PRIVATE_KEY_PATTERN)
            tracked_age_secret_identities = _tracked_secret_files(root, GIT_AGE_IDENTITY_PATTERN)
        except GitInspectionError:
            return [GIT_INSPECTION_ERROR]

    ci_environment = root / ".env.ci"
    allow_ci_environment = False
    if (
        git_repository
        and ci_environment.is_file()
        and not ci_environment.is_symlink()
        and ci_environment not in tracked
    ):
        try:
            allow_ci_environment = _git_path_is_ignored(root, Path(".env.ci"))
        except GitInspectionError:
            return [GIT_INSPECTION_ERROR]

    for path in _candidate_files(root, tracked):
        relative = path.relative_to(root)
        if _is_private_runtime_artifact(path) or _is_runtime_environment(path):
            if relative.as_posix() == ".env.ci" and allow_ci_environment:
                continue
            errors.append(f"{relative}: runtime data belongs outside the public source repo")
            continue
        payload: bytes | None = None
        if path.is_file() and not path.is_symlink():
            try:
                payload = path.read_bytes()
            except OSError:
                errors.append(f"{relative}: unable to inspect repository file safely")
                continue
        if path in tracked_pem_private_keys or (
            payload is not None and PEM_PRIVATE_KEY_PATTERN.search(payload)
        ):
            errors.append(f"{relative}: forbidden committed private key")
        if path in tracked_age_secret_identities or (
            payload is not None and AGE_SECRET_IDENTITY_PATTERN.search(payload)
        ):
            errors.append(f"{relative}: forbidden committed age secret identity")
        if relative.as_posix() == "scripts/check_repository.py":
            continue
        if payload is None or path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, forbidden in FORBIDDEN.items():
            if forbidden.lower() in text.lower():
                errors.append(f"{relative}: forbidden {label}")
        if _is_runtime_policy_path(relative):
            for line_number, line in enumerate(text.splitlines(), start=1):
                if DIRECT_FIREWALL_TOOL.search(line):
                    errors.append(f"{relative}:{line_number}: forbidden direct firewall management")
                if DIRECT_SELINUX_TOOL.search(line):
                    errors.append(f"{relative}:{line_number}: forbidden direct SELinux management")
                lowered = line.lower()
                if any(marker in lowered for marker in DOCKER_DAEMON_FIREWALL_MARKERS):
                    errors.append(
                        f"{relative}:{line_number}: forbidden Docker daemon/firewall configuration"
                    )
                if any(marker in lowered for marker in SELINUX_CONFIGURATION_MARKERS):
                    errors.append(f"{relative}:{line_number}: forbidden SELinux host configuration")
        if relative.as_posix() == "bootstrap_worker/installer.py":
            renderer = _function_source(text, "render_agent_compose")
            if re.search(r"(?m)^\s*ports\s*:", renderer):
                errors.append(f"{relative}: Node Agent must not publish host ports")
            if re.search(r"(?m)^\s*network_mode\s*:", renderer):
                errors.append(f"{relative}: Node Agent must not set network_mode")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "docker compose" in line and "docker compose -p adojapan-restream" not in line:
                errors.append(f"{relative}:{line_number}: Compose command lacks project name")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    errors = check(args.root.resolve())
    if errors:
        raise SystemExit("\n".join(errors))
    print("Repository policy checks passed")


if __name__ == "__main__":
    main()
