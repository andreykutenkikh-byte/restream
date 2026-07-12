"""Explicitly restore this project's SQLite DB from a selected project backup."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

CONFIRMATION = "RESTORE_ADOJAPAN_RESTREAM"


def restore(backup: Path, database: Path, confirmation: str) -> None:
    if confirmation != CONFIRMATION:
        raise ValueError(f"Pass --confirm {CONFIRMATION} to restore")
    backup = backup.resolve(strict=True)
    database = database.resolve()
    if not backup.name.startswith("adojapan-restream-") or backup.suffix != ".db":
        raise ValueError("Refusing to restore a backup not owned by this project")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup) as source, sqlite3.connect(database) as destination:
        source.backup(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", type=Path, default=Path("/srv/app/data/restream.db"))
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    restore(args.backup, args.database, args.confirm)


if __name__ == "__main__":
    main()
