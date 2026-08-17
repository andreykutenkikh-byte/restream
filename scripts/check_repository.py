"""Fail CI when repository-wide isolation and domain invariants are violated."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

IGNORED_PARTS = {".git", ".venv", "__pycache__", "data", "logs", "backups"}
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
    "committed private key": "BEGIN OPENSSH " + "PRIVATE KEY",
}

RUNTIME_POLICY_DIRECTORIES = {
    ".github",
    "app",
    "bootstrap_worker",
    "ci",
    "node_agent",
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
    this_file = Path(__file__).resolve()
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.resolve() == this_file or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root)
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
