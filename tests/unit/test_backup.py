import sqlite3
from pathlib import Path

import pytest

from scripts.backup import backup
from scripts.restore import CONFIRMATION, restore


def create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('preserved')")


def test_backup_is_consistent_and_retention_is_project_scoped(tmp_path: Path) -> None:
    database = tmp_path / "restream.db"
    output = tmp_path / "backups"
    output.mkdir()
    create_database(database)
    (output / "adojapan-restream-20200101T000000Z.db").touch()
    (output / "unrelated.db").touch()

    result = backup(database, output, retain=1)

    with sqlite3.connect(result) as connection:
        row = connection.execute("SELECT value FROM sample").fetchone()
    assert row == ("preserved",)
    assert not (output / "adojapan-restream-20200101T000000Z.db").exists()
    assert (output / "unrelated.db").exists()


def test_restore_requires_explicit_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "restream.db"
    output = tmp_path / "backups"
    output.mkdir()
    create_database(source)
    project_backup = backup(source, output, retain=1)
    restored = tmp_path / "restored.db"

    with pytest.raises(ValueError, match="confirm"):
        restore(project_backup, restored, "")

    restore(project_backup, restored, CONFIRMATION)
    with sqlite3.connect(restored) as connection:
        row = connection.execute("SELECT value FROM sample").fetchone()
    assert row == ("preserved",)
