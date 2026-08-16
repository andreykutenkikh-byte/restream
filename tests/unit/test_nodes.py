from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.security import (
    digest_opaque_token,
    generate_enrollment_token,
    generate_node_token,
    verify_opaque_token_digest,
)
from app.db import Database
from app.services.nodes import (
    COMMAND_LEASE_SECONDS,
    COMMAND_MAX_AGE_SECONDS,
    CommandStateError,
    EnrollmentTokenError,
    HeartbeatRateLimitError,
    NodeAuthenticationError,
    NodeService,
    NodeUnavailableError,
    UnsupportedProtocolError,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


@pytest.fixture()
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 8, 16, 0, 0, tzinfo=UTC))


@pytest.fixture()
def node_service(tmp_path: Path, clock: MutableClock) -> NodeService:
    database = Database(tmp_path / "nodes.sqlite")
    database.migrate()
    return NodeService(database, clock=clock)


def profile(*, protocol_version: int = 1) -> dict[str, object]:
    return {
        "agent_version": "0.1.0",
        "protocol_version": protocol_version,
        "hostname": "node-01",
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "architecture": "x86_64",
        "cpu_count": 2,
        "memory_total_bytes": 2_147_483_648,
        "memory_available_bytes": 1_073_741_824,
        "disk_total_bytes": 40_000_000_000,
        "disk_free_bytes": 30_000_000_000,
        "capabilities": ["ping", "self_test", "ffmpeg", "ffprobe"],
    }


def heartbeat() -> dict[str, object]:
    return {
        "agent_version": "0.1.0",
        "protocol_version": 1,
        "hostname": "node-01",
        "uptime_seconds": 100.0,
        "load_1m": 0.25,
        "cpu_percent": 12.5,
        "memory_total_bytes": 2_147_483_648,
        "memory_available_bytes": 1_000_000_000,
        "disk_total_bytes": 40_000_000_000,
        "disk_free_bytes": 29_000_000_000,
        "ffmpeg_version": "7.1",
        "ffprobe_version": "7.1",
        "capabilities": ["ping", "self_test"],
        "current_command_id": None,
        "control_latency_ms": 12.5,
    }


def create_node(service: NodeService, *, suffix: str = "01") -> str:
    node = service.create_pending_node(
        display_name=f"server-{suffix}",
        address=f"198.51.100.{int(suffix)}",
        resolved_ip=f"198.51.100.{int(suffix)}",
        ssh_port=22,
        ssh_username="root",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint="SHA256:test-fingerprint",
        host_key_trust_mode="tofu",
    )
    return str(node["id"])


def enroll_node(service: NodeService, node_id: str) -> str:
    enrollment = service.issue_enrollment(node_id)
    grant = service.enroll(enrollment, public_ip="198.51.100.10", profile=profile())
    assert grant.node_id == node_id
    return grant.node_token


def database_dump(database: Database) -> str:
    with database.connect() as connection:
        return "\n".join(connection.iterdump())


def test_node_schema_migration_is_idempotent_and_has_no_password_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "nodes.sqlite")
    database.migrate()
    database.migrate()
    with database.connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        node_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(restream_nodes)").fetchall()
        }
    assert {
        "restream_nodes",
        "node_install_jobs",
        "node_enrollment_tokens",
        "node_credentials",
        "node_commands",
        "node_events",
    } <= tables
    assert "password" not in " ".join(node_columns)
    assert "control_latency_ms" in node_columns
    assert database.ready()


def test_schema_v1_upgrade_preserves_existing_restream_data(tmp_path: Path) -> None:
    path = tmp_path / "schema-v1.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE ingest_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            stream_key_encrypted TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id_hash TEXT PRIMARY KEY,
            csrf_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            server_url TEXT NOT NULL,
            stream_key_encrypted TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'stopped',
            last_error TEXT,
            started_at TEXT,
            worker_pid INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, '2026-07-14T00:00:00+00:00');
        INSERT INTO ingest_config VALUES (1, 'encrypted-ingest', '2026-07-14T00:00:00+00:00');
        INSERT INTO sessions VALUES (
            'session-digest', 'csrf-digest',
            '2026-07-14T00:00:00+00:00', '2099-01-01T00:00:00+00:00'
        );
        INSERT INTO destinations(
            name, server_url, stream_key_encrypted, enabled, state, created_at, updated_at
        ) VALUES (
            'Existing output', 'rtmp://example.test/live', 'encrypted-output', 1, 'stopped',
            '2026-07-14T00:00:00+00:00', '2026-07-14T00:00:00+00:00'
        );
        INSERT INTO audit_events(event_type, detail, created_at)
        VALUES ('existing.event', 'preserve-me', '2026-07-14T00:00:00+00:00');
        """
    )
    connection.close()

    database = Database(path)
    database.migrate()

    with database.connect() as upgraded:
        assert (
            upgraded.execute(
                "SELECT stream_key_encrypted FROM ingest_config WHERE id = 1"
            ).fetchone()["stream_key_encrypted"]
            == "encrypted-ingest"
        )
        assert upgraded.execute("SELECT id_hash FROM sessions").fetchone()["id_hash"] == (
            "session-digest"
        )
        assert upgraded.execute("SELECT name FROM destinations").fetchone()["name"] == (
            "Existing output"
        )
        assert upgraded.execute("SELECT detail FROM audit_events").fetchone()["detail"] == (
            "preserve-me"
        )
        assert (
            upgraded.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()[
                "version"
            ]
            == 2
        )
        node_tables = {
            str(row["name"])
            for row in upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'node_%'"
            )
        }
    assert {
        "node_install_jobs",
        "node_enrollment_tokens",
        "node_credentials",
        "node_commands",
        "node_events",
    } <= node_tables
    assert database.ready()


def test_high_entropy_tokens_are_digested_and_verified_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrollment = generate_enrollment_token()
    node_token = generate_node_token("11111111-1111-4111-8111-111111111111")
    assert len(enrollment) >= 43
    assert node_token.startswith("node_11111111-1111-4111-8111-111111111111_")
    digest = digest_opaque_token(enrollment)
    comparisons: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return left == right

    monkeypatch.setattr("app.core.security.hmac.compare_digest", compare)
    assert verify_opaque_token_digest(enrollment, digest)
    assert not verify_opaque_token_digest(f"{enrollment}x", digest)
    assert comparisons == [
        (digest, digest),
        (digest_opaque_token(f"{enrollment}x"), digest),
    ]


def test_enrollment_is_one_time_bounded_and_never_persists_raw_tokens(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    enrollment = node_service.issue_enrollment(node_id)
    grant = node_service.enroll(
        enrollment,
        public_ip="198.51.100.10",
        profile=profile(),
    )
    dump = database_dump(node_service.database)
    assert enrollment not in dump
    assert grant.node_token not in dump
    assert digest_opaque_token(enrollment) in dump
    assert digest_opaque_token(grant.node_token) in dump
    with pytest.raises(EnrollmentTokenError):
        node_service.enroll(enrollment, public_ip="198.51.100.10", profile=profile())
    assert node_service.authenticate(grant.node_token)["node_id"] == node_id


def test_enrollment_random_input_is_read_only_before_atomic_consume(
    node_service: NodeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = create_node(node_service)
    enrollment = node_service.issue_enrollment(node_id)
    statements: list[str] = []
    original_connect = node_service.database.connect

    @contextmanager
    def traced_connect() -> Iterator[sqlite3.Connection]:
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(node_service.database, "connect", traced_connect)
    with pytest.raises(EnrollmentTokenError):
        node_service.enroll(
            "invalid-enrollment-marker-" + "x" * 32,
            public_ip="198.51.100.10",
            profile=profile(),
        )
    normalized = [statement.strip().upper() for statement in statements]
    assert sum("FROM NODE_ENROLLMENT_TOKENS AS TOKEN" in item for item in normalized) == 1
    assert not any(item.startswith("BEGIN IMMEDIATE") for item in normalized)

    statements.clear()
    node_service.enroll(enrollment, public_ip="198.51.100.10", profile=profile())
    normalized = [statement.strip().upper() for statement in statements]
    lookups = [
        index
        for index, statement in enumerate(normalized)
        if "FROM NODE_ENROLLMENT_TOKENS AS TOKEN" in statement
    ]
    writer = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("BEGIN IMMEDIATE")
    )
    assert len(lookups) == 2
    assert lookups[0] < writer < lookups[1]


def test_expired_enrollment_and_unsupported_protocol_are_rejected_without_consuming(
    node_service: NodeService,
    clock: MutableClock,
) -> None:
    node_id = create_node(node_service)
    enrollment = node_service.issue_enrollment(node_id)
    with pytest.raises(UnsupportedProtocolError):
        node_service.enroll(
            enrollment,
            public_ip="198.51.100.10",
            profile=profile(protocol_version=2),
        )
    node_service.enroll(enrollment, public_ip="198.51.100.10", profile=profile())

    second = node_service.issue_enrollment(node_id)
    clock.advance(seconds=601)
    with pytest.raises(EnrollmentTokenError):
        node_service.enroll(second, public_ip="198.51.100.10", profile=profile())


def test_heartbeat_updates_metrics_rate_limits_and_derives_liveness(
    node_service: NodeService,
    clock: MutableClock,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    result = node_service.record_heartbeat(token, heartbeat())
    assert result["node_status"] == "ready"
    node = node_service.get_node(node_id)
    assert node is not None
    assert node["uptime_seconds"] == 100.0
    assert node["load_1m"] == 0.25
    assert node["cpu_percent"] == 12.5
    assert node["memory_available_bytes"] == 1_000_000_000
    assert node["ffmpeg_version"] == "7.1"
    assert node["ffprobe_version"] == "7.1"
    assert node["control_latency_ms"] == 12.5
    assert node["capabilities"] == ["ping", "self_test"]
    with pytest.raises(HeartbeatRateLimitError):
        node_service.record_heartbeat(token, heartbeat())

    clock.advance(seconds=16)
    assert node_service.get_node(node_id)["status"] == "degraded"  # type: ignore[index]
    clock.advance(seconds=15)
    assert node_service.get_node(node_id)["status"] == "offline"  # type: ignore[index]
    node_service.record_heartbeat(token, heartbeat())
    assert node_service.get_node(node_id)["status"] == "ready"  # type: ignore[index]


def test_agent_keeps_authoritative_bootstrap_host_facts(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    with node_service.database.connect() as connection:
        connection.execute(
            """
            UPDATE restream_nodes
            SET hostname = 'edge-host-01', os_name = 'ubuntu', os_version = '24.04',
                architecture = 'amd64', cpu_count = 8,
                memory_total_bytes = 8589934592, memory_available_bytes = 7000000000,
                disk_total_bytes = 100000000000, disk_free_bytes = 90000000000
            WHERE id = ?
            """,
            (node_id,),
        )
    enrollment = node_service.issue_enrollment(node_id)
    grant = node_service.enroll(
        enrollment,
        public_ip="198.51.100.10",
        profile={
            **profile(),
            "hostname": "container-id",
            "os_name": "container-os",
            "cpu_count": 1,
            "memory_total_bytes": 268435456,
        },
    )
    node = node_service.get_node(node_id)
    assert node is not None
    assert node["hostname"] == "edge-host-01"
    assert node["os_name"] == "ubuntu"
    assert node["architecture"] == "amd64"
    assert node["cpu_count"] == 8
    assert node["memory_total_bytes"] == 8_589_934_592

    node_service.record_heartbeat(
        grant.node_token,
        {
            **heartbeat(),
            "hostname": "another-container-id",
            "cpu_count": 1,
            "memory_total_bytes": 268435456,
        },
    )
    refreshed = node_service.get_node(node_id)
    assert refreshed is not None
    assert refreshed["hostname"] == "edge-host-01"
    assert refreshed["cpu_count"] == 8
    assert refreshed["memory_total_bytes"] == 8_589_934_592
    # Availability remains a dynamic heartbeat metric, while immutable host
    # identity and capacity facts continue to come from the SSH preflight.
    assert refreshed["memory_available_bytes"] == 1_000_000_000


def test_command_lease_retry_ack_complete_and_idempotency(
    node_service: NodeService,
    clock: MutableClock,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    command = node_service.create_command(node_id, "PING")

    first = node_service.lease_next_command(token)
    assert first is not None
    assert first["id"] == command["id"]
    assert first["attempt_count"] == 1
    assert node_service.lease_next_command(token) is None
    clock.advance(seconds=COMMAND_LEASE_SECONDS + 1)
    second = node_service.lease_next_command(token)
    assert second is not None
    assert second["id"] == command["id"]
    assert second["attempt_count"] == 2
    assert node_service.acknowledge_command(token, str(command["id"])) == "acknowledged"
    assert node_service.acknowledge_command(token, str(command["id"])) == "acknowledged"

    result = {
        "status": "ok",
        "received_at": "2026-08-16T00:00:00+00:00",
        "completed_at": "2026-08-16T00:00:01+00:00",
        "agent_version": "0.1.0",
        "checks": None,
    }
    assert node_service.complete_command(token, str(command["id"]), result) == "completed"
    assert node_service.complete_command(token, str(command["id"]), result) == "completed"
    saved = node_service.get_command(node_id, str(command["id"]))
    assert saved is not None
    assert saved["state"] == "completed"
    assert saved["attempt_count"] == 2


def test_command_delivery_fails_closed_after_bounded_retries(
    node_service: NodeService,
    clock: MutableClock,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    command = node_service.create_command(node_id, "SELF_TEST")
    for attempt in range(1, 4):
        delivery = node_service.lease_next_command(token)
        assert delivery is not None
        assert delivery["attempt_count"] == attempt
        clock.advance(seconds=COMMAND_LEASE_SECONDS + 1)
    node_service.reconcile_command_leases(node_id=node_id)
    saved = node_service.get_command(node_id, str(command["id"]))
    assert saved is not None
    assert saved["state"] == "failed"
    assert saved["safe_result"] == {
        "code": "delivery_attempts_exhausted",
        "status": "failed",
    }


def test_reconciliation_expires_abandoned_queued_command(
    node_service: NodeService,
    clock: MutableClock,
) -> None:
    node_id = create_node(node_service)
    enroll_node(node_service, node_id)
    command = node_service.create_command(node_id, "PING")

    clock.advance(seconds=COMMAND_MAX_AGE_SECONDS + 1)
    reconciled = node_service.reconcile_command_leases()
    saved = node_service.get_command(node_id, str(command["id"]))

    assert reconciled == {"failed": 1, "requeued": 0}
    assert saved is not None
    assert saved["state"] == "failed"
    assert saved["safe_result"] == {"code": "command_expired", "status": "failed"}


def test_self_test_result_status_must_match_all_checks(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    command = node_service.create_command(node_id, "SELF_TEST")
    assert node_service.lease_next_command(token) is not None
    checks = {
        "control_https": True,
        "dns": False,
        "ffmpeg": True,
        "ffprobe": True,
        "memory": True,
        "disk": True,
        "data_writable": True,
        "no_inbound_ports": True,
    }

    with pytest.raises(CommandStateError, match="does not match"):
        node_service.complete_command(
            token,
            str(command["id"]),
            {
                "status": "ok",
                "received_at": None,
                "completed_at": "2026-08-16T00:00:01+00:00",
                "agent_version": "0.1.0",
                "checks": checks,
            },
        )


def test_command_requires_live_credential_and_supported_protocol(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    with pytest.raises(NodeUnavailableError):
        node_service.create_command(node_id, "PING")

    token = enroll_node(node_service, node_id)
    with node_service.database.connect() as connection:
        connection.execute(
            "UPDATE restream_nodes SET protocol_version = 2 WHERE id = ?",
            (node_id,),
        )
    with pytest.raises(UnsupportedProtocolError):
        node_service.create_command(node_id, "PING")
    with pytest.raises(UnsupportedProtocolError):
        node_service.lease_next_command(token)


def test_revoke_is_idempotent_rejects_credentials_and_cancels_pending_commands(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    leased = node_service.create_command(node_id, "PING")
    queued = node_service.create_command(node_id, "SELF_TEST")
    delivery = node_service.lease_next_command(token)
    assert delivery is not None
    delivered_id = str(delivery["id"])
    node_service.acknowledge_command(token, delivered_id)
    node_service.record_heartbeat(token, heartbeat())

    assert node_service.revoke_node(node_id)["status"] == "revoked"
    assert node_service.revoke_node(node_id)["status"] == "revoked"
    with pytest.raises(NodeAuthenticationError):
        node_service.authenticate(token)
    for command in (leased, queued):
        assert node_service.get_command(node_id, str(command["id"]))["state"] == "cancelled"  # type: ignore[index]


@pytest.mark.parametrize("status", ["installing", "connecting", "failed"])
def test_revoke_cannot_replace_active_bootstrap_or_retry_with_remote_rollback(
    node_service: NodeService,
    status: str,
) -> None:
    node_id = create_node(node_service)
    if status == "connecting":
        enroll_node(node_service, node_id)
    elif status == "failed":
        with node_service.database.connect() as connection:
            connection.execute(
                "UPDATE restream_nodes SET status = 'failed' WHERE id = ?", (node_id,)
            )

    with pytest.raises(NodeUnavailableError):
        node_service.revoke_node(node_id)
    assert node_service.get_node(node_id)["status"] == status  # type: ignore[index]


def test_ready_node_cannot_be_revoked_until_bootstrap_job_is_terminal(
    node_service: NodeService,
) -> None:
    node_id = create_node(node_service)
    token = enroll_node(node_service, node_id)
    node_service.record_heartbeat(token, heartbeat())
    job_id = str(uuid4())
    with node_service.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO node_install_jobs(
                id, node_id, state, current_step, progress_percent, created_at, updated_at
            ) VALUES (?, ?, 'running_self_test', 'running_self_test', 95, ?, ?)
            """,
            (
                job_id,
                node_id,
                node_service.get_node(node_id)["created_at"],
                node_service.get_node(node_id)["updated_at"],
            ),
        )

    with pytest.raises(NodeUnavailableError):
        node_service.revoke_node(node_id)
    assert node_service.authenticate(token)["node_id"] == node_id

    with node_service.database.connect() as connection:
        connection.execute(
            "UPDATE node_install_jobs SET state = 'completed' WHERE id = ?", (job_id,)
        )
    assert node_service.revoke_node(node_id)["status"] == "revoked"


def test_event_and_terminal_record_retention_is_bounded(
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    database = Database(tmp_path / "nodes.sqlite")
    database.migrate()
    service = NodeService(database, clock=clock, event_limit=3)
    node_id = create_node(service)
    token = enroll_node(service, node_id)
    command = service.create_command(node_id, "PING")
    service.lease_next_command(token)
    service.acknowledge_command(token, str(command["id"]))
    service.complete_command(
        token,
        str(command["id"]),
        {
            "status": "ok",
            "received_at": "2026-08-16T00:00:00+00:00",
            "completed_at": "2026-08-16T00:00:01+00:00",
            "agent_version": "0.1.0",
            "checks": None,
        },
    )
    service.issue_enrollment(node_id)
    assert len(service.list_events(node_id, limit=100)) == 3

    clock.advance(days=31)
    pruned = service.prune_retention()
    assert pruned == {"enrollment_tokens": 2, "commands": 1}
    assert service.get_command(node_id, str(command["id"])) is None
