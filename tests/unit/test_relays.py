from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.security import decrypt_destination_key, generate_master_key
from app.db import Database
from app.schemas import RelayConfigureYouTubeRequest
from app.services.nodes import NodeAuthenticationError, NodeService, NodeUnavailableError
from app.services.relays import (
    RELAY_COMMAND_LEASE_SECONDS,
    RELAY_COMMAND_MAX_ATTEMPTS,
    RELAY_COMMAND_TTL_SECONDS,
    RelayActiveError,
    RelayAuthenticationError,
    RelayBootstrapActiveError,
    RelayCommandPendingError,
    RelayCommandStateError,
    RelayIdempotencyConflictError,
    RelayNotConfiguredError,
    RelayProvisionConflictError,
    RelaySecretUnavailableError,
    RelayService,
    RelayUnavailableError,
    RelayUnsupportedProtocolError,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 31, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def safe_state(*, active: bool = False) -> dict[str, Any]:
    return {
        "service_state": "active" if active else "inactive",
        "enabled": False,
        "main_process": "running" if active else "stopped",
        "srt_listener": "listening" if active else "closed",
        "source": "SLATE" if active else "NONE",
        "youtube_forward": "active" if active else "inactive",
        "overall": "healthy",
        "youtube_url_configured": True,
        "youtube_key_configured": True,
        "healthy": True,
        "portrait_profile": True,
        "error_code": None,
    }


def heartbeat(state: dict[str, Any], *, agent_version: str = "1.0.0") -> dict[str, Any]:
    return {
        "agent_version": agent_version,
        "protocol_version": 1,
        "hostname": "hk-relay",
        "relay": state,
        "host": {
            "uptime_seconds": 100,
            "load_1m": 0.1,
            "cpu_percent": 2.5,
            "memory_total_bytes": 2_000_000_000,
            "memory_available_bytes": 1_000_000_000,
            "disk_total_bytes": 20_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
        "current_command_id": None,
    }


def database_dump(database: Database) -> str:
    with database.connect() as connection:
        return "\n".join(connection.iterdump())


def set_relay_audit_failure(database: Database, *, enabled: bool) -> None:
    with database.connect() as connection:
        connection.execute("DROP TRIGGER IF EXISTS fail_relay_audit")
        if enabled:
            connection.execute(
                """
                CREATE TRIGGER fail_relay_audit
                BEFORE INSERT ON audit_events
                WHEN NEW.event_type LIKE 'relay.%'
                BEGIN
                    SELECT RAISE(ABORT, 'injected relay audit failure');
                END
                """
            )


class _BeginSignalConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        attempted: Event,
        worker_name: str,
    ) -> None:
        self._connection = connection
        self._attempted = attempted
        self._worker_name = worker_name

    def execute(
        self,
        statement: str,
        parameters: Any = (),
    ) -> sqlite3.Cursor:
        if (
            current_thread().name == self._worker_name
            and statement.strip().upper() == "BEGIN IMMEDIATE"
        ):
            self._attempted.set()
        return self._connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def run_during_blocked_writer(
    database: Database,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[], Any],
    *,
    advance_seconds: float,
) -> dict[str, Any]:
    original_connect = database.connect
    attempted = Event()
    worker_name = "blocked-relay-operation"

    @contextmanager
    def signaling_connect() -> Iterator[_BeginSignalConnection]:
        with original_connect() as connection:
            yield _BeginSignalConnection(connection, attempted, worker_name)

    monkeypatch.setattr(database, "connect", signaling_connect)
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = operation()
        except Exception as exc:  # noqa: BLE001 - test captures the domain result
            outcome["error"] = exc

    with original_connect() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        worker = Thread(target=run, name=worker_name, daemon=True)
        worker.start()
        assert attempted.wait(timeout=2), "relay operation did not wait for the writer lock"
        clock.advance(seconds=advance_seconds)
        blocker.execute("COMMIT")

    worker.join(timeout=2)
    assert not worker.is_alive(), "relay operation remained blocked"
    return outcome


def provisioned(tmp_path: Path) -> tuple[Database, RelayService, str, str, MutableClock]:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)
    grant = service.provision_node(display_name="HK relay", address="198.51.100.20")
    service.record_heartbeat(grant.node_token, heartbeat(safe_state()))
    return database, service, grant.node_id, grant.node_token, clock


def test_provision_is_one_identity_and_rotation_requires_explicit_stopped_flag(
    tmp_path: Path,
) -> None:
    database, service, node_id, token, _ = provisioned(tmp_path)
    with database.connect() as connection:
        node = connection.execute(
            "SELECT id, node_kind, capabilities_json FROM restream_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        relay = connection.execute(
            "SELECT node_id FROM relay_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        credential = connection.execute(
            "SELECT token_digest FROM node_credentials WHERE node_id = ?", (node_id,)
        ).fetchone()
    assert node is not None and "moblin_relay" in node["capabilities_json"]
    assert node["node_kind"] == "moblin_relay"
    assert relay is not None
    assert credential is not None
    assert token not in database_dump(database)
    with pytest.raises(NodeAuthenticationError):
        NodeService(database).authenticate(token)
    with pytest.raises(NodeUnavailableError):
        NodeService(database).create_command(node_id, "PING")

    with pytest.raises(RelayProvisionConflictError):
        service.provision_node(display_name="HK relay", address="198.51.100.20")
    rotated = service.provision_node(
        display_name="HK relay",
        address="198.51.100.20",
        rotate_existing=True,
    )
    assert rotated.node_id == node_id
    assert rotated.node_token != token
    rotated_status = service.get_status(node_id)
    assert rotated_status["available"] is False
    assert rotated_status["last_seen_at"] is None
    assert rotated_status["status"]["service"] == "unknown"
    with pytest.raises(RelayAuthenticationError):
        service.record_heartbeat(token, heartbeat(safe_state()))
    assert service.get_status(node_id)["available"] is False

    service.record_heartbeat(rotated.node_token, heartbeat(safe_state()))
    assert service.get_status(node_id)["available"] is True


def test_relay_mutations_and_one_time_secret_are_atomic_with_audit(tmp_path: Path) -> None:
    database = Database(tmp_path / "relay.sqlite", audit_limit=3)
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)

    set_relay_audit_failure(database, enabled=True)
    with pytest.raises(sqlite3.DatabaseError, match="injected relay audit failure"):
        service.provision_node(display_name="HK relay", address="198.51.100.20")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM restream_nodes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM node_credentials").fetchone()[0] == 0

    set_relay_audit_failure(database, enabled=False)
    grant = service.provision_node(display_name="HK relay", address="198.51.100.20")
    service.record_heartbeat(grant.node_token, heartbeat(safe_state()))

    set_relay_audit_failure(database, enabled=True)
    with pytest.raises(sqlite3.DatabaseError, match="injected relay audit failure"):
        service.create_command(grant.node_id, "STATUS")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM relay_commands").fetchone()[0] == 0

    set_relay_audit_failure(database, enabled=False)
    command = service.create_command(grant.node_id, "REVEAL_MOBLIN_URL")
    assert service.lease_next_command(grant.node_token) is not None
    service.acknowledge_command(grant.node_token, command["id"])
    secret = "srt://198.51.100.20:8890?streamid=publish:live:AUDIT_ATOMIC_SECRET_76"

    set_relay_audit_failure(database, enabled=True)
    with pytest.raises(sqlite3.DatabaseError, match="injected relay audit failure"):
        service.complete_command(
            grant.node_token,
            command["id"],
            status="ok",
            completed_at=clock().isoformat(),
            safe_result=safe_state(),
            secret_result=secret,
        )
    with database.connect() as connection:
        incomplete = connection.execute(
            """
            SELECT state, completed_at, completion_status, secret_result_encrypted
            FROM relay_commands WHERE id = ?
            """,
            (command["id"],),
        ).fetchone()
    assert incomplete is not None and incomplete["state"] == "acknowledged"
    assert incomplete["completed_at"] is None
    assert incomplete["completion_status"] is None
    assert incomplete["secret_result_encrypted"] is None

    set_relay_audit_failure(database, enabled=False)
    service.complete_command(
        grant.node_token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=secret,
    )
    set_relay_audit_failure(database, enabled=True)
    with pytest.raises(sqlite3.DatabaseError, match="injected relay audit failure"):
        service.consume_secret_result(grant.node_id, command["id"])
    with database.connect() as connection:
        preserved = connection.execute(
            """
            SELECT secret_result_encrypted, secret_consumed_at
            FROM relay_commands WHERE id = ?
            """,
            (command["id"],),
        ).fetchone()
    assert preserved is not None and preserved["secret_result_encrypted"] is not None
    assert preserved["secret_consumed_at"] is None

    set_relay_audit_failure(database, enabled=False)
    assert service.consume_secret_result(grant.node_id, command["id"]) == secret
    assert len(database.list_audit_events(limit=10)) <= database.audit_limit


def test_fresh_stopped_heartbeat_is_available_even_when_broker_reports_offline(
    tmp_path: Path,
) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=2)
    stopped = safe_state()
    stopped["overall"] = "offline"
    service.record_heartbeat(token, heartbeat(stopped))

    status = service.get_status(node_id)
    assert status["available"] is True
    assert status["status"]["overall"] == "ok"

    clock.advance(seconds=31)
    stale_status = service.get_status(node_id)
    assert stale_status["available"] is False
    assert stale_status["status"]["overall"] == "offline"


def test_live_input_bitrate_is_persisted_and_hidden_when_stale_or_non_live(
    tmp_path: Path,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=2)
    live = safe_state(active=True)
    live["source"] = "LIVE"
    live["input_bitrate_bps"] = 4_000_000
    service.record_heartbeat(token, heartbeat(live))

    status = service.get_status(node_id)
    assert status["status"]["source"] == "LIVE"
    assert status["status"]["input_bitrate_bps"] == 4_000_000
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT input_bitrate_bps FROM relay_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()[0]
    assert stored == 4_000_000

    clock.advance(seconds=16)
    stale = service.get_status(node_id)
    assert stale["available"] is True
    assert stale["status"]["input_bitrate_bps"] is None

    clock.advance(seconds=15)
    stale = service.get_status(node_id)
    assert stale["available"] is False
    assert stale["status"]["input_bitrate_bps"] is None

    clock.advance(seconds=1)
    service.record_heartbeat(token, heartbeat(safe_state()))
    assert service.get_status(node_id)["status"]["input_bitrate_bps"] is None
    with database.connect() as connection:
        cleared = connection.execute(
            "SELECT input_bitrate_bps FROM relay_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()[0]
    assert cleared is None


def test_old_agent_heartbeat_without_input_bitrate_remains_supported(tmp_path: Path) -> None:
    _, service, node_id, _, _ = provisioned(tmp_path)

    status = service.get_status(node_id)
    assert status["available"] is True
    assert status["status"]["input_bitrate_bps"] is None


def test_command_completion_updates_live_input_bitrate(tmp_path: Path) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    live = safe_state(active=True)
    live["source"] = "LIVE"
    live["input_bitrate_bps"] = 3_750_000

    assert (
        service.complete_command(
            token,
            command["id"],
            status="ok",
            completed_at=clock().isoformat(),
            safe_result=live,
            secret_result=None,
        )
        == "completed"
    )
    assert service.get_status(node_id)["status"]["input_bitrate_bps"] == 3_750_000


def test_legacy_completed_result_without_bitrate_remains_idempotent(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    result = safe_state()
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=result,
        secret_result=None,
    )
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT safe_result_json FROM relay_commands WHERE id = ?", (command["id"],)
        ).fetchone()[0]
        legacy = json.loads(stored)
        legacy.pop("input_bitrate_bps")
        connection.execute(
            "UPDATE relay_commands SET safe_result_json = ? WHERE id = ?",
            (json.dumps(legacy, separators=(",", ":"), sort_keys=True), command["id"]),
        )

    assert (
        service.complete_command(
            token,
            command["id"],
            status="ok",
            completed_at=clock().isoformat(),
            safe_result=result,
            secret_result=None,
        )
        == "completed"
    )


def test_fresh_inconsistent_stopped_state_is_not_normalized_to_ok(tmp_path: Path) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=2)
    inconsistent = safe_state()
    inconsistent["main_process"] = "running"
    inconsistent["overall"] = "degraded"
    service.record_heartbeat(token, heartbeat(inconsistent))

    status = service.get_status(node_id)
    assert status["available"] is True
    assert status["status"]["service"] == "inactive"
    assert status["status"]["main_process"] == "running"
    assert status["status"]["overall"] == "degraded"


@pytest.mark.parametrize("reported_overall", ["degraded", "failed"])
def test_fresh_stopped_relay_preserves_reported_attention_state(
    tmp_path: Path,
    reported_overall: str,
) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=2)
    stopped = safe_state()
    stopped["overall"] = reported_overall
    service.record_heartbeat(token, heartbeat(stopped))

    status = service.get_status(node_id)
    assert status["available"] is True
    assert status["status"]["service"] == "inactive"
    assert status["status"]["main_process"] == "stopped"
    assert status["status"]["overall"] == reported_overall


def test_fresh_residual_forward_is_not_normalized_to_ok(tmp_path: Path) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=2)
    residual = safe_state()
    residual["overall"] = "offline"
    residual["youtube_forward"] = "active"
    service.record_heartbeat(token, heartbeat(residual))

    status = service.get_status(node_id)
    assert status["available"] is True
    assert status["status"]["overall"] == "offline"
    assert status["status"]["youtube_forward"] == "active"


def test_provision_refuses_to_convert_an_existing_generic_node(tmp_path: Path) -> None:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    NodeService(database).create_pending_node(
        display_name="generic",
        address="198.51.100.30",
        resolved_ip="198.51.100.30",
        ssh_port=22,
        ssh_username="root",
    )
    service = RelayService(database, generate_master_key())
    with pytest.raises(RelayProvisionConflictError):
        service.provision_node(display_name="relay", address="198.51.100.30")


def test_relay_commands_are_blocked_until_bootstrap_is_terminal(tmp_path: Path) -> None:
    database, service, node_id, _, _ = provisioned(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO node_install_jobs(
                id, node_id, install_profile, state, current_step,
                progress_percent, created_at, updated_at
            ) VALUES (
                'bootstrap-job', ?, 'moblin_relay', 'waiting_for_enrollment',
                'waiting_for_enrollment', 85, 'created', 'updated'
            )
            """,
            (node_id,),
        )

    with pytest.raises(RelayBootstrapActiveError):
        service.create_command(node_id, "STATUS")

    with database.connect() as connection:
        connection.execute(
            """
            UPDATE node_install_jobs
            SET state = 'completed', current_step = 'completed', progress_percent = 100
            WHERE id = 'bootstrap-job'
            """
        )
    assert service.create_command(node_id, "STATUS")["state"] == "queued"


def test_failed_bootstrap_revokes_credential_and_tombstones_relay_payload(
    tmp_path: Path,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    marker = "FAILED_BOOTSTRAP_STREAM_KEY_CANARY_91"
    command = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={
            "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
            "youtube_stream_key": marker,
        },
    )
    nodes = NodeService(
        database,
        clock=clock,
        relay_payload_tombstone=service.encrypted_empty_payload(),
    )
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        nodes.revoke_incomplete_bootstrap(connection, node_id, clock().isoformat())
        connection.execute("COMMIT")

    with database.connect() as connection:
        saved = connection.execute(
            "SELECT state, payload_encrypted FROM relay_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()
        credential = connection.execute(
            "SELECT revoked_at FROM node_credentials WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    assert saved["state"] == "cancelled"
    assert (
        decrypt_destination_key(saved["payload_encrypted"], service.master_encryption_key) == "{}"
    )
    assert credential["revoked_at"] is not None
    assert marker not in database_dump(database)
    with pytest.raises(RelayAuthenticationError):
        service.authenticate(token)


def test_connecting_relay_can_be_revoked_then_rotated_and_requires_new_heartbeat(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)
    original = service.provision_node(display_name="HK relay", address="198.51.100.20")

    revoked = NodeService(
        database,
        clock=clock,
        relay_payload_tombstone=service.encrypted_empty_payload(),
    ).revoke_node(original.node_id)
    assert revoked["status"] == "revoked"
    with pytest.raises(RelayAuthenticationError):
        service.record_heartbeat(original.node_token, heartbeat(safe_state()))

    rotated = service.provision_node(
        display_name="HK relay",
        address="198.51.100.20",
        rotate_existing=True,
    )
    assert rotated.node_id == original.node_id
    assert rotated.node_token != original.node_token
    assert service.get_status(original.node_id)["available"] is False
    with pytest.raises(RelayAuthenticationError):
        service.record_heartbeat(original.node_token, heartbeat(safe_state()))

    service.record_heartbeat(rotated.node_token, heartbeat(safe_state()))
    assert service.get_status(original.node_id)["available"] is True


@pytest.mark.parametrize(
    "url",
    [
        "rtmps://a.rtmp.youtube.com/live2",
        "rtmps://evil.example/live2",
        "rtmps://a.rtmps.youtube.com/live2?x=1",
        "rtmps://a.rtmps.youtube.com/live2#key",
        "rtmp://a.rtmps.youtube.com/live2",
    ],
)
def test_youtube_url_trust_boundary_rejects_non_official_rtmps(url: str) -> None:
    marker = "KEY_CANARY_90"
    with pytest.raises(ValidationError) as exc:
        RelayConfigureYouTubeRequest.model_validate({"url": url, "stream_key": marker})
    assert marker not in str(exc.value)


def test_youtube_url_accepts_exact_official_primary_and_backup() -> None:
    for host in ("a.rtmps.youtube.com", "b.rtmps.youtube.com"):
        request = RelayConfigureYouTubeRequest.model_validate(
            {
                "url": f"rtmps://{host}:443/live2",
                "stream_key": "valid_key-123",
            }
        )
        assert request.url.get_secret_value() == f"rtmps://{host}:443/live2"
        assert request.admin_password is None

    legacy_request = RelayConfigureYouTubeRequest.model_validate(
        {
            "url": "rtmps://a.rtmps.youtube.com/live2",
            "stream_key": "valid_key-123",
            "admin_password": "cached-frontend-password",
        }
    )
    assert legacy_request.admin_password is not None


def test_encrypted_command_payload_and_active_configuration_conflict(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    marker = "STREAMKEY_CANARY_91"
    endpoint = "rtmps://a.rtmps.youtube.com/live2"
    command = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": marker},
        idempotency_key="test:configure:001",
    )
    duplicate = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": marker},
        idempotency_key="test:configure:001",
    )
    assert duplicate["id"] == command["id"]
    dump = database_dump(database)
    assert marker not in dump
    assert endpoint not in dump
    assert marker not in repr(command)

    lease = service.lease_next_command(token)
    assert lease is not None
    assert lease["payload"] == {
        "youtube_rtmps_url": endpoint,
        "youtube_stream_key": marker,
    }
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=None,
    )
    assert marker not in database_dump(database)
    assert marker not in str(database.list_audit_events())
    with database.connect() as connection:
        erased = connection.execute(
            "SELECT payload_encrypted FROM relay_commands WHERE id = ?", (command["id"],)
        ).fetchone()["payload_encrypted"]
    assert decrypt_destination_key(erased, service.master_encryption_key) == "{}"

    clock.advance(seconds=2)
    service.record_heartbeat(token, heartbeat(safe_state(active=True)))
    with pytest.raises(RelayActiveError):
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": "replacement"},
        )


@pytest.mark.parametrize(
    ("service_state", "main_process"),
    [
        ("active", "running"),
        ("failed", "running"),
        ("unknown", "running"),
        ("inactive", "running"),
        ("unknown", "stopped"),
    ],
)
def test_youtube_mutations_require_confirmed_inactive_stopped_state(
    tmp_path: Path,
    service_state: str,
    main_process: str,
) -> None:
    database, service, node_id, _, _ = provisioned(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE relay_nodes SET service_state = ?, main_process = ? WHERE node_id = ?",
            (service_state, main_process, node_id),
        )

    with pytest.raises(RelayActiveError):
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": "replacement",
            },
        )
    with pytest.raises(RelayActiveError):
        service.create_command(node_id, "CLEAR_YOUTUBE")


@pytest.mark.parametrize(
    ("srt_listener", "source", "youtube_forward", "overall", "last_error_code"),
    [
        ("listening", "NONE", "inactive", "offline", None),
        ("closed", "LIVE", "inactive", "offline", None),
        ("closed", "NONE", "active", "offline", None),
        ("closed", "NONE", "inactive", "degraded", None),
        ("closed", "NONE", "inactive", "offline", "relayctl_failed"),
    ],
)
def test_mutations_reject_incoherent_stopped_snapshot(
    tmp_path: Path,
    srt_listener: str,
    source: str,
    youtube_forward: str,
    overall: str,
    last_error_code: str | None,
) -> None:
    database, service, node_id, _, _ = provisioned(tmp_path)
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE relay_nodes
            SET srt_listener = ?, source = ?, youtube_forward = ?, overall = ?,
                last_error_code = ?
            WHERE node_id = ?
            """,
            (srt_listener, source, youtube_forward, overall, last_error_code, node_id),
        )

    with pytest.raises(RelayActiveError):
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": "replacement",
            },
        )
    with pytest.raises(RelayActiveError):
        service.create_command(node_id, "CLEAR_YOUTUBE")
    with pytest.raises(RelayActiveError):
        service.create_command(node_id, "START")


def test_idempotency_binds_exact_payload_and_mutations_are_serialized(tmp_path: Path) -> None:
    database, service, node_id, _, _ = provisioned(tmp_path)
    endpoint = "rtmps://a.rtmps.youtube.com/live2"
    first_key = "IDEMPOTENCY_CANARY_FIRST_31"
    second_key = "IDEMPOTENCY_CANARY_SECOND_32"
    command = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": first_key},
        idempotency_key="test:configure:exact:001",
    )
    assert (
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": first_key},
            idempotency_key="test:configure:exact:001",
        )["id"]
        == command["id"]
    )

    with pytest.raises(RelayIdempotencyConflictError) as mismatch:
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={"youtube_rtmps_url": endpoint, "youtube_stream_key": second_key},
            idempotency_key="test:configure:exact:001",
        )
    assert second_key not in str(mismatch.value)
    assert second_key not in database_dump(database)

    with pytest.raises(RelayCommandPendingError):
        service.create_command(node_id, "CLEAR_YOUTUBE")


def test_key_only_command_requires_capable_agent_and_is_secret_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)
    grant = service.provision_node(display_name="HK relay", address="198.51.100.20")
    service.record_heartbeat(grant.node_token, heartbeat(safe_state()))
    marker = "KEY_ONLY_SECRET_FIRST_71"

    with pytest.raises(RelayUnsupportedProtocolError):
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_stream_key": marker},
        )

    clock.advance(seconds=2)
    service.record_heartbeat(
        grant.node_token,
        heartbeat(safe_state(), agent_version="1.2.0"),
    )
    command = service.create_command(
        grant.node_id,
        "CONFIGURE_YOUTUBE_KEY",
        payload={"youtube_stream_key": marker},
        idempotency_key="test:key-only:0001",
    )
    repeated = service.create_command(
        grant.node_id,
        "CONFIGURE_YOUTUBE_KEY",
        payload={"youtube_stream_key": marker},
        idempotency_key="test:key-only:0001",
    )
    assert repeated["id"] == command["id"]
    replacement = "KEY_ONLY_SECRET_SECOND_72"
    with pytest.raises(RelayIdempotencyConflictError) as mismatch:
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_stream_key": replacement},
            idempotency_key="test:key-only:0001",
        )
    assert marker not in database_dump(database)
    assert replacement not in database_dump(database)
    assert replacement not in str(mismatch.value)

    leased = service.lease_next_command(
        grant.node_token,
        polling_agent_version="1.2.0",
    )
    assert leased is not None
    assert leased["action"] == "CONFIGURE_YOUTUBE_KEY"
    assert leased["payload"] == {"youtube_stream_key": marker}


def test_key_only_command_refuses_active_missing_url_and_malformed_payload(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)
    grant = service.provision_node(display_name="HK relay", address="198.51.100.20")
    active = safe_state(active=True)
    service.record_heartbeat(
        grant.node_token,
        heartbeat(active, agent_version="1.2.0"),
    )
    with pytest.raises(RelayActiveError):
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_stream_key": "replacement"},
        )

    clock.advance(seconds=2)
    missing_url = safe_state()
    missing_url["youtube_url_configured"] = False
    missing_url["youtube_key_configured"] = False
    missing_url["error_code"] = "youtube_not_configured"
    service.record_heartbeat(
        grant.node_token,
        heartbeat(missing_url, agent_version="1.2.0"),
    )
    with pytest.raises(RelayNotConfiguredError):
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_stream_key": "replacement"},
        )
    with pytest.raises(ValueError, match="configure key payload"):
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2"},
        )
    with pytest.raises(ValueError, match="configure key payload"):
        service.create_command(
            grant.node_id,
            "CONFIGURE_YOUTUBE_KEY",
            payload={"youtube_stream_key": "invalid key!"},
        )


def test_old_poll_terminalizes_key_only_command_before_downgrade_heartbeat(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "relay.sqlite")
    database.migrate()
    clock = MutableClock()
    service = RelayService(database, generate_master_key(), clock=clock)
    grant = service.provision_node(display_name="HK relay", address="198.51.100.20")
    service.record_heartbeat(
        grant.node_token,
        heartbeat(safe_state(), agent_version="1.2.0"),
    )
    marker = "KEY_ONLY_DOWNGRADE_RACE_SECRET_73"
    command = service.create_command(
        grant.node_id,
        "CONFIGURE_YOUTUBE_KEY",
        payload={"youtube_stream_key": marker},
        idempotency_key="test:key-only:downgrade-race:0001",
    )

    # A rolled-back agent polls before it has sent the heartbeat that would
    # downgrade the stored 1.2 capability.  Old clients omit the assertion.
    assert service.lease_next_command(grant.node_token) is None
    terminal = service.get_command(grant.node_id, command["id"])
    assert terminal is not None
    assert terminal["state"] == "failed"
    assert terminal["completion_status"] == "failed"
    assert terminal["safe_result"] == {"error_code": "unsupported_command"}
    assert terminal["secret_available"] is False

    with database.connect() as connection:
        row = connection.execute(
            "SELECT payload_encrypted FROM relay_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()
    assert row is not None
    assert decrypt_destination_key(row["payload_encrypted"], service.master_encryption_key) == "{}"
    assert marker not in database_dump(database)

    # The safe terminal row no longer blocks a subsequent mutation.
    replacement = service.create_command(
        grant.node_id,
        "CLEAR_YOUTUBE",
        idempotency_key="test:key-only:downgrade-race:0002",
    )
    assert replacement["state"] == "queued"
    legacy_lease = service.lease_next_command(grant.node_token)
    assert legacy_lease is not None
    assert legacy_lease["action"] == "CLEAR_YOUTUBE"


def test_rotation_requires_fresh_stopped_state_and_no_command(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    clock.advance(seconds=31)
    with pytest.raises(RelayProvisionConflictError, match="fresh stopped"):
        service.provision_node(
            display_name="HK relay",
            address="198.51.100.20",
            rotate_existing=True,
        )

    service.record_heartbeat(token, heartbeat(safe_state()))
    command = service.create_command(node_id, "START")
    with pytest.raises(RelayProvisionConflictError, match="pending"):
        service.provision_node(
            display_name="HK relay",
            address="198.51.100.20",
            rotate_existing=True,
        )
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=None,
    )

    current = "11111111-1111-4111-8111-111111111111"
    with database.connect() as connection:
        connection.execute(
            "UPDATE restream_nodes SET current_command_id = ? WHERE id = ?",
            (current, node_id),
        )
        connection.execute(
            "UPDATE relay_nodes SET current_command_id = ? WHERE node_id = ?",
            (current, node_id),
        )
    with pytest.raises(RelayProvisionConflictError, match="active"):
        service.provision_node(
            display_name="HK relay",
            address="198.51.100.20",
            rotate_existing=True,
        )


def test_revoke_atomically_cancels_and_erases_pending_relay_payload(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    reveal = service.create_command(
        node_id,
        "REVEAL_MOBLIN_URL",
        idempotency_key="test:revoke:reveal:001",
    )
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, reveal["id"])
    service.complete_command(
        token,
        reveal["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result="srt://198.51.100.20:8890?streamid=publish:live:REVOKE_SRT_SECRET_62",
    )
    marker = "REVOKE_STREAM_KEY_CANARY_63"
    command = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={
            "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
            "youtube_stream_key": marker,
        },
        idempotency_key="test:revoke:configure:001",
    )
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    clock.advance(seconds=2)
    current_heartbeat = heartbeat(safe_state())
    current_heartbeat["current_command_id"] = command["id"]
    service.record_heartbeat(token, current_heartbeat)

    NodeService(
        database,
        clock=clock,
        relay_payload_tombstone=service.encrypted_empty_payload(),
    ).revoke_node(node_id)

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT state, completion_status, payload_encrypted, secret_result_encrypted
            FROM relay_commands WHERE id = ?
            """,
            (command["id"],),
        ).fetchone()
        reveal_secret = connection.execute(
            "SELECT secret_result_encrypted FROM relay_commands WHERE id = ?",
            (reveal["id"],),
        ).fetchone()["secret_result_encrypted"]
        current = connection.execute(
            """
            SELECT node.current_command_id AS node_current,
                   relay.current_command_id AS relay_current
            FROM restream_nodes AS node
            JOIN relay_nodes AS relay ON relay.node_id = node.id
            WHERE node.id = ?
            """,
            (node_id,),
        ).fetchone()
    assert row is not None and row["state"] == "cancelled"
    assert row["completion_status"] == "failed"
    assert row["secret_result_encrypted"] is None
    assert reveal_secret is None
    assert decrypt_destination_key(row["payload_encrypted"], service.master_encryption_key) == "{}"
    assert current is not None and current["node_current"] is None
    assert current["relay_current"] is None
    assert marker not in database_dump(database)
    with pytest.raises(RelayAuthenticationError):
        service.record_heartbeat(token, heartbeat(safe_state()))
    with pytest.raises(RelayAuthenticationError):
        service.create_command(
            node_id,
            "CONFIGURE_YOUTUBE",
            payload={
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": marker,
            },
            idempotency_key="test:revoke:configure:001",
        )
    with pytest.raises(RelaySecretUnavailableError):
        service.consume_secret_result(node_id, reveal["id"])
    assert service.get_status(node_id)["available"] is False


def test_reveal_secret_is_encrypted_safe_and_consumed_once(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    srt_marker = (
        "Public: srt://198.51.100.20:8890?streamid=publish:live:SRT_CANARY_72"
        "&passphrase=SRT_SECRET_CANARY_73\n"
        "VPN: srt://172.29.0.1:8890?streamid=publish:live:vpn"
    )
    command = service.create_command(node_id, "REVEAL_MOBLIN_URL")
    lease = service.lease_next_command(token)
    assert lease is not None and lease["action"] == "REVEAL_MOBLIN_URL"
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=srt_marker,
    )
    command_view = service.get_command(node_id, command["id"])
    assert command_view is not None and command_view["secret_available"] is True
    assert "SRT_SECRET_CANARY_73" not in repr(command_view)
    assert "SRT_SECRET_CANARY_73" not in database_dump(database)
    assert "SRT_SECRET_CANARY_73" not in str(database.list_audit_events())

    assert service.consume_secret_result(node_id, command["id"]) == srt_marker
    with pytest.raises(RelaySecretUnavailableError):
        service.consume_secret_result(node_id, command["id"])
    with database.connect() as connection:
        row = connection.execute(
            "SELECT secret_result_encrypted, secret_consumed_at FROM relay_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()
    assert row is not None and row["secret_result_encrypted"] is None
    assert row["secret_consumed_at"] is not None


def test_exact_idempotent_reveal_can_be_recovered_offline(tmp_path: Path) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    key = "test:offline:reveal:001"
    secret = "srt://198.51.100.20:8890?streamid=publish:live:OFFLINE_SRT_SECRET_74"
    command = service.create_command(node_id, "REVEAL_MOBLIN_URL", idempotency_key=key)
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=secret,
    )
    clock.advance(seconds=31)

    replay = service.create_command(node_id, "REVEAL_MOBLIN_URL", idempotency_key=key)
    assert replay["id"] == command["id"]
    assert service.consume_secret_result(node_id, command["id"]) == secret
    with pytest.raises(RelayUnavailableError):
        service.create_command(node_id, "STATUS", idempotency_key="test:offline:status:001")


def test_rotation_clears_unconsumed_relay_secret(tmp_path: Path) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "REVEAL_MOBLIN_URL")
    assert service.lease_next_command(token) is not None
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result="srt://198.51.100.20:8890?streamid=publish:live:ROTATE_SECRET_75",
    )

    service.provision_node(
        display_name="HK relay",
        address="198.51.100.20",
        rotate_existing=True,
    )
    with database.connect() as connection:
        persisted = connection.execute(
            "SELECT secret_result_encrypted FROM relay_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()
    assert persisted is not None and persisted["secret_result_encrypted"] is None
    with pytest.raises(RelaySecretUnavailableError):
        service.consume_secret_result(node_id, command["id"])


def test_retries_expiry_and_completion_secret_shape_are_enforced(tmp_path: Path) -> None:
    _, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    assert service.lease_next_command(token) is not None
    reconciled = service.reconcile_command_leases(node_id=node_id)
    assert reconciled["failed"] == 0
    in_flight = service.get_command(node_id, command["id"])
    assert in_flight is not None and in_flight["state"] == "leased"
    service.acknowledge_command(token, command["id"])
    service.complete_command(
        token,
        command["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=None,
    )
    completed = service.get_command(node_id, command["id"])
    assert completed is not None and completed["state"] == "completed"

    expiring = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    reconciled = service.reconcile_command_leases(node_id=node_id)
    assert reconciled["failed"] == 1
    view = service.get_command(node_id, expiring["id"])
    assert view is not None and view["state"] == "failed"

    service.record_heartbeat(token, heartbeat(safe_state()))
    reveal = service.create_command(node_id, "REVEAL_MOBLIN_URL")
    assert service.lease_next_command(token) is not None
    with pytest.raises(RelayCommandStateError):
        service.complete_command(
            token,
            reveal["id"],
            status="ok",
            completed_at=clock().isoformat(),
            safe_result=safe_state(),
            secret_result=None,
        )


def test_valid_third_lease_is_not_failed_before_its_lease_ends(tmp_path: Path) -> None:
    assert RELAY_COMMAND_TTL_SECONDS > (RELAY_COMMAND_LEASE_SECONDS * RELAY_COMMAND_MAX_ATTEMPTS)
    _, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None

    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    assert service.lease_next_command(token) is not None
    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS + 1)
    third = service.lease_next_command(token)
    assert third is not None and third["attempt_count"] == RELAY_COMMAND_MAX_ATTEMPTS

    clock.advance(seconds=RELAY_COMMAND_LEASE_SECONDS - 1)
    assert service.reconcile_command_leases(node_id=node_id)["failed"] == 0
    in_flight = service.get_command(node_id, command["id"])
    assert in_flight is not None and in_flight["state"] == "leased"

    clock.advance(seconds=2)
    assert service.reconcile_command_leases(node_id=node_id)["failed"] == 1
    failed = service.get_command(node_id, command["id"])
    assert failed is not None and failed["state"] == "failed"


def test_lease_requires_full_budget_before_expiry_and_erases_short_command(
    tmp_path: Path,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    exact = service.create_command(node_id, "STATUS")
    clock.advance(seconds=RELAY_COMMAND_TTL_SECONDS - RELAY_COMMAND_LEASE_SECONDS)
    lease = service.lease_next_command(token)
    assert lease is not None and lease["id"] == exact["id"]
    service.acknowledge_command(token, exact["id"])
    service.complete_command(
        token,
        exact["id"],
        status="ok",
        completed_at=clock().isoformat(),
        safe_result=safe_state(),
        secret_result=None,
    )

    marker = "LEASE_BUDGET_CANARY_47"
    short = service.create_command(
        node_id,
        "CONFIGURE_YOUTUBE",
        payload={
            "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
            "youtube_stream_key": marker,
        },
    )
    short_expiry = (clock() + timedelta(seconds=RELAY_COMMAND_LEASE_SECONDS - 1)).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE relay_commands SET expires_at = ? WHERE id = ?",
            (short_expiry, short["id"]),
        )

    assert service.lease_next_command(token) is None
    failed_short = service.get_command(node_id, short["id"])
    assert failed_short is not None and failed_short["state"] == "failed"
    with database.connect() as connection:
        ciphertext = connection.execute(
            "SELECT payload_encrypted FROM relay_commands WHERE id = ?", (short["id"],)
        ).fetchone()["payload_encrypted"]
    assert decrypt_destination_key(ciphertext, service.master_encryption_key) == "{}"
    assert marker not in database_dump(database)


def test_lease_samples_time_after_waiting_for_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    monkeypatch.setattr(
        service,
        "reconcile_command_leases",
        lambda *, node_id=None: {"failed": 0, "requeued": 0},
    )

    outcome = run_during_blocked_writer(
        database,
        clock,
        monkeypatch,
        lambda: service.lease_next_command(token),
        advance_seconds=45,
    )

    assert "error" not in outcome
    lease = outcome["value"]
    assert lease is not None and lease["id"] == command["id"]
    with database.connect() as connection:
        lease_until = connection.execute(
            "SELECT lease_until FROM relay_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()["lease_until"]
    assert datetime.fromisoformat(str(lease_until)) == clock() + timedelta(
        seconds=RELAY_COMMAND_LEASE_SECONDS
    )


def test_ack_rejects_lease_that_expires_while_waiting_for_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None

    outcome = run_during_blocked_writer(
        database,
        clock,
        monkeypatch,
        lambda: service.acknowledge_command(token, command["id"]),
        advance_seconds=RELAY_COMMAND_LEASE_SECONDS + 1,
    )

    assert isinstance(outcome.get("error"), RelayCommandStateError)
    saved = service.get_command(node_id, command["id"])
    assert saved is not None and saved["state"] == "leased"


def test_completion_rejects_lease_that_expires_while_waiting_for_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, node_id, token, clock = provisioned(tmp_path)
    command = service.create_command(node_id, "STATUS")
    assert service.lease_next_command(token) is not None

    outcome = run_during_blocked_writer(
        database,
        clock,
        monkeypatch,
        lambda: service.complete_command(
            token,
            command["id"],
            status="ok",
            completed_at=clock().isoformat(),
            safe_result=safe_state(),
            secret_result=None,
        ),
        advance_seconds=RELAY_COMMAND_LEASE_SECONDS + 1,
    )

    assert isinstance(outcome.get("error"), RelayCommandStateError)
    saved = service.get_command(node_id, command["id"])
    assert saved is not None and saved["state"] == "leased"
