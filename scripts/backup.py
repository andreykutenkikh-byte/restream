"""Create a bounded, transactionally consistent backup of this project's SQLite DB."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup(database: Path, output: Path, retain: int) -> Path:
    database = database.resolve(strict=True)
    output = output.resolve(strict=True)
    if database.suffix != ".db":
        raise ValueError("Expected the AdoJapan Restream .db file")
    if retain < 1 or retain > 365:
        raise ValueError("retain must be between 1 and 365")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output / f"adojapan-restream-{timestamp}.db"
    with sqlite3.connect(database) as source, sqlite3.connect(target) as destination:
        source.backup(destination)

    backups = sorted(output.glob("adojapan-restream-*.db"), reverse=True)
    for expired in backups[retain:]:
        expired.unlink()
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("/srv/app/data/restream.db"))
    parser.add_argument("--output", type=Path, default=Path("/srv/app/backups"))
    parser.add_argument("--retain", type=int, default=14)
    args = parser.parse_args()
    print(backup(args.database, args.output, args.retain))


if __name__ == "__main__":
    main()
