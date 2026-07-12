from pathlib import Path

from app.db import Database


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    database.migrate()
    assert database.ready()


def test_destination_lifecycle(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    destination = database.create_destination(
        name="YouTube",
        server_url="rtmps://example.test/live2",
        encrypted_key="encrypted",
        enabled=False,
    )
    assert database.count_destinations() == 1
    assert destination["state"] == "stopped"

    updated = database.update_destination(destination["id"], enabled=True)
    assert updated is not None
    assert updated["enabled"] == 1

    database.set_destination_state(destination["id"], "waiting_for_input")
    assert database.get_destination(destination["id"])["state"] == "waiting_for_input"
    assert database.delete_destination(destination["id"])


def test_audit_log_is_bounded(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite", audit_limit=3)
    database.migrate()
    for index in range(5):
        database.add_audit_event("test", str(index))
    events = database.list_audit_events(limit=10)
    assert [event["detail"] for event in events] == ["4", "3", "2"]
