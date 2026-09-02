from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from relay_agent.errors import RelayAgentError
from relay_agent.journal import CommandJournal
from relay_agent.models import RelayCommand, RelayCompletion, RelaySnapshot, YouTubeKeyConfiguration

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "deploy" / "hk-relay-agent" / "journal-rollback.py"
LEGACY_ACTIONS = {
    "STATUS",
    "START",
    "STOP",
    "CONFIGURE_YOUTUBE",
    "CLEAR_YOUTUBE",
    "REVEAL_MOBLIN_URL",
}
LEGACY_SNAPSHOT_FIELDS = {
    "service_state",
    "enabled",
    "main_process",
    "srt_listener",
    "source",
    "youtube_forward",
    "overall",
    "youtube_url_configured",
    "youtube_key_configured",
    "healthy",
    "portrait_profile",
    "error_code",
}


def _load_helper() -> ModuleType:
    specification = importlib.util.spec_from_file_location("relay_journal_rollback", HELPER_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _snapshot(*, bitrate: int | None = None) -> RelaySnapshot:
    return RelaySnapshot(
        service_state="active",
        enabled=True,
        main_process="running",
        srt_listener="listening",
        source="LIVE" if bitrate is not None else "SLATE",
        youtube_forward="active",
        overall="healthy",
        youtube_url_configured=True,
        youtube_key_configured=True,
        healthy=True,
        portrait_profile=True,
        error_code=None,
        input_bitrate_bps=bitrate,
    )


def _command(command_id: str, action: str) -> RelayCommand:
    return RelayCommand(
        command_id=command_id,
        action=action,  # type: ignore[arg-type]
        lease_seconds=30,
        attempt_count=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        youtube_key=(
            YouTubeKeyConfiguration("FIXTURE_KEY_NOT_A_SECRET")
            if action == "CONFIGURE_YOUTUBE_KEY"
            else None
        ),
    )


def _entry(command_id: str, action: str, snapshot: RelaySnapshot) -> dict[str, Any]:
    return {
        "id": command_id,
        "action": action,
        "status": "ok",
        "completed_at": "2026-09-02T00:00:00.000Z",
        "safe_result": snapshot.to_json(),
    }


def _assert_accepted_by_legacy_v1_contract(payload: bytes) -> dict[str, Any]:
    decoded = json.loads(payload.decode("ascii"))
    assert set(decoded) == {"version", "entries"}
    assert type(decoded["version"]) is int and decoded["version"] == 1
    assert isinstance(decoded["entries"], list) and len(decoded["entries"]) <= 64
    seen: set[str] = set()
    for entry in decoded["entries"]:
        assert set(entry) == {"id", "action", "status", "completed_at", "safe_result"}
        assert isinstance(entry["id"], str) and entry["id"] not in seen
        seen.add(entry["id"])
        assert entry["action"] in LEGACY_ACTIONS
        assert entry["status"] in {"ok", "failed", "conflict"}
        parsed_time = datetime.fromisoformat(entry["completed_at"].replace("Z", "+00:00"))
        assert parsed_time.tzinfo is not None
        assert set(entry["safe_result"]) == LEGACY_SNAPSHOT_FIELDS
        RelaySnapshot.parse(entry["safe_result"])
    return decoded


def test_legacy_v1_loads_strictly_and_next_record_upgrades_to_v2(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    journal_path = directory / "commands.json"
    first_id = "11111111-1111-4111-8111-111111111111"
    legacy = {"version": 1, "entries": [_entry(first_id, "STATUS", _snapshot())]}
    journal_path.write_text(json.dumps(legacy), encoding="ascii")
    journal_path.chmod(0o600)

    journal = CommandJournal(journal_path)
    assert journal.lookup(_command(first_id, "STATUS")) is not None

    second = _command("22222222-2222-4222-8222-222222222222", "START")
    journal.record(second, RelayCompletion("ok", "2026-09-02T00:00:01.000Z", _snapshot()))
    upgraded = json.loads(journal_path.read_bytes())
    assert upgraded["version"] == 2
    assert [entry["action"] for entry in upgraded["entries"]] == ["STATUS", "START"]


def test_key_only_and_bitrate_entry_is_written_only_as_journal_v2(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    journal_path = directory / "commands.json"
    command = _command("33333333-3333-4333-8333-333333333333", "CONFIGURE_YOUTUBE_KEY")

    CommandJournal(journal_path).record(
        command,
        RelayCompletion("ok", "2026-09-02T00:00:02.000Z", _snapshot(bitrate=3_750_000)),
    )

    decoded = json.loads(journal_path.read_bytes())
    assert decoded["version"] == 2
    assert decoded["entries"][0]["action"] == "CONFIGURE_YOUTUBE_KEY"
    assert decoded["entries"][0]["safe_result"]["input_bitrate_bps"] == 3_750_000


@pytest.mark.parametrize(
    "entry",
    [
        _entry(
            "66666666-6666-4666-8666-666666666666",
            "CONFIGURE_YOUTUBE_KEY",
            _snapshot(),
        ),
        _entry("77777777-7777-4777-8777-777777777777", "STATUS", _snapshot(bitrate=1)),
    ],
)
def test_legacy_v1_rejects_v2_only_action_and_bitrate(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    journal_path = directory / "commands.json"
    journal_path.write_text(json.dumps({"version": 1, "entries": [entry]}), encoding="ascii")
    journal_path.chmod(0o600)

    with pytest.raises(RelayAgentError, match="invalid_journal"):
        CommandJournal(journal_path)


def test_restore_uses_projected_backup_accepted_by_legacy_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    current = json.dumps(
        {
            "version": 2,
            "entries": [
                _entry(
                    "44444444-4444-4444-8444-444444444444", "STATUS", _snapshot(bitrate=4_000_000)
                ),
                _entry(
                    "55555555-5555-4555-8555-555555555555",
                    "CONFIGURE_YOUTUBE_KEY",
                    _snapshot(bitrate=4_000_000),
                ),
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")
    backup = helper.project_to_legacy_v1(current)
    restored: dict[str, bytes] = {}

    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(helper, "_account_ids", lambda: (1001, 1001))
    monkeypatch.setattr(helper, "_validate_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(helper, "_path_exists_nofollow", lambda path: path == helper.LIVE_JOURNAL)
    monkeypatch.setattr(
        helper,
        "_read_private_file",
        lambda path, **kwargs: backup if path == helper.ROLLBACK_JOURNAL else current,
    )
    monkeypatch.setattr(
        helper,
        "_atomic_replace",
        lambda path, payload, **kwargs: restored.setdefault(str(path), payload),
    )

    helper.restore_legacy_journal()

    restored_payload = restored[str(helper.LIVE_JOURNAL)]
    decoded = _assert_accepted_by_legacy_v1_contract(restored_payload)
    assert [entry["action"] for entry in decoded["entries"]] == ["STATUS"]
    assert "input_bitrate_bps" not in decoded["entries"][0]["safe_result"]


def test_prepare_never_replaces_an_existing_valid_v1_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    existing = helper.project_to_legacy_v1(None)

    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(helper, "_account_ids", lambda: (1001, 1001))
    monkeypatch.setattr(helper, "_validate_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        helper,
        "_path_exists_nofollow",
        lambda path: path == helper.ROLLBACK_JOURNAL,
    )
    monkeypatch.setattr(helper, "_read_private_file", lambda *args, **kwargs: existing)
    monkeypatch.setattr(
        helper,
        "_atomic_create",
        lambda *args, **kwargs: pytest.fail("valid rollback point was overwritten"),
    )

    helper.prepare_rollback_point()


def test_service_activity_check_refuses_an_active_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _load_helper()

    class Result:
        def __init__(self, output: bytes) -> None:
            self.returncode = 0
            self.stdout = output

    results = iter((Result(b"active\n"), Result(b"123\n")))
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(helper.JournalRollbackError):
        helper._require_quiescent_services()
