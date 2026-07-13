"""Fail CI when repository-wide isolation and domain invariants are violated."""

from __future__ import annotations

import argparse
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
}


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
        for label, forbidden in FORBIDDEN.items():
            if forbidden.lower() in text.lower():
                errors.append(f"{path.relative_to(root)}: forbidden {label}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "docker compose" in line and "docker compose -p adojapan-restream" not in line:
                errors.append(
                    f"{path.relative_to(root)}:{line_number}: Compose command lacks project name"
                )
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
