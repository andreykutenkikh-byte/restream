from pathlib import Path

from app.db import Database
from app.session import SessionManager


def test_session_stores_digests_and_validates_csrf(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    manager = SessionManager(database, "session-secret", 3600)
    session = manager.create()

    with database.connect() as connection:
        row = connection.execute("SELECT * FROM sessions").fetchone()
    assert row is not None
    assert session.token not in dict(row).values()
    assert session.csrf_token not in dict(row).values()
    assert manager.get(session.token) is not None
    assert manager.validate_csrf(session.token, session.csrf_token)
    assert not manager.validate_csrf(session.token, "wrong")

    manager.delete(session.token)
    assert manager.get(session.token) is None
