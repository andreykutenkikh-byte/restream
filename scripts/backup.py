"""Create a bounded, transactionally consistent backup of this project's SQLite DB."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote


def backup(database: Path, output: Path, retain: int) -> Path:
    database = database.resolve(strict=True)
    output = output.resolve(strict=True)
    if database.suffix != ".db":
        raise ValueError("Expected the AdoJapan Restream .db file")
    if retain < 1 or retain > 365:
        raise ValueError("retain must be between 1 and 365")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output / f"adojapan-restream-{timestamp}.db"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    encoded_database = quote(database.as_posix(), safe="/")
    try:
        with (
            sqlite3.connect(f"file:{encoded_database}?mode=ro", uri=True) as source,
            sqlite3.connect(target) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite backup did not pass quick_check")
        target.chmod(0o600)
        with target.open("rb+") as persisted:
            os.fsync(persisted.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise

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
