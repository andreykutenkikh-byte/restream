from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.bootstrap_api import BootstrapRateLimiter, BootstrapRequest, SudoPasswordRequest
from app.core.security import digest_opaque_token, generate_master_key
from app.core.ssh_target import canonicalize_ssh_address
from app.db import Database
from app.services.bootstrap import (
    MIN_WORKER_TERMINAL_RESULT_TTL_SECONDS,
    SYNC_FAILURE_GRACE_SECONDS,
    WORKER_OVERALL_TIMEOUT_SECONDS,
    BootstrapClient,
    BootstrapCoordinator,
    BootstrapJobConflict,
    BootstrapJobNotFound,
    BootstrapSubmission,
    BootstrapUnavailable,
    BootstrapWorkerRestarted,
)
from app.services.nodes import EnrollmentTokenError, NodeAuthenticationError, NodeService
from app.services.relays import RelayAuthenticationError, RelayService
from bootstrap_worker.jobs import MIN_TERMINAL_RESULT_TTL_SECONDS


class FakeBootstrapClient:
    def __init__(self) -> None:
        self.instance_id = str(uuid4())
        self.submission: BootstrapSubmission | None = None
        self.enrollment_completed_calls = 0
        self.enrollment_token_calls = 0
        self.last_enrollment_token = ""
        self.host_key_persisted_calls = 0
        self.view: dict[str, Any] = {
            "state": "checking_system",
            "current_step": "checking_system",
            "progress_percent": 35,
            "steps": [{"name": "checking_system", "state": "running"}],
            "safe_error": None,
            "target": {"resolved_ip": "8.8.8.8"},
            "host_key": {
                "algorithm": "ssh-ed25519",
                "fingerprint": "SHA256:" + "A" * 43,
                "trust_mode": "tofu",
            },
            "system": {
                "hostname": "edge-01",
                "os_name": "Ubuntu",
                "os_version": "24.04",
                "architecture": "amd64",
                "cpu_count": 2,
                "memory_total_bytes": 2 * 1024**3,
                "memory_available_bytes": 1024**3,
                "disk_total_bytes": 32 * 1024**3,
                "disk_free_bytes": 24 * 1024**3,
            },
        }

    async def create_job(self, submission: BootstrapSubmission) -> dict[str, Any]:
        self.submission = submission
        return {
            "job_id": submission.job_id,
            "state": "queued",
            "worker_instance_id": self.instance_id,
        }

    async def discover_job(self, worker_job_id: str) -> dict[str, Any]:
        if self.submission is None or worker_job_id != self.submission.job_id:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        return {
            "job_id": worker_job_id,
            "state": self.view["state"],
            "worker_instance_id": self.instance_id,
        }

    async def get_job(self, _: str, __: str) -> dict[str, Any]:
        return self.view

    async def provide_sudo_password(self, _: str, __: str, password: SecretStr) -> dict[str, Any]:
        assert password.get_secret_value() == "sudo-only"
        return {**self.view, "state": "checking_system"}

    async def provide_enrollment_token(
        self,
        _: str,
        __: str,
        enrollment_token: SecretStr,
    ) -> dict[str, Any]:
        self.enrollment_token_calls += 1
        self.last_enrollment_token = enrollment_token.get_secret_value()
        self.view = {**self.view, "enrollment_token_received": True}
        return self.view

    async def host_key_persisted(self, _: str, __: str) -> dict[str, Any]:
        self.host_key_persisted_calls += 1
        return self.view

    async def cancel_job(self, _: str, __: str) -> dict[str, Any]:
        return {
            **self.view,
            "state": "cancelled",
            "current_step": "cancelled",
            "progress_percent": 100,
        }

    async def enrollment_completed(self, _: str, __: str) -> dict[str, Any]:
        self.enrollment_completed_calls += 1
        return {
            **self.view,
            "state": "completed",
            "current_step": "completed",
            "progress_percent": 100,
        }

    async def healthy(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class UncertainCreateClient(FakeBootstrapClient):
    def __init__(self, *, unavailable_discoveries: int = 1) -> None:
        super().__init__()
        self.unavailable_discoveries = unavailable_discoveries
        self.create_calls = 0
        self.discover_calls = 0
        self.cancel_calls = 0
        self.not_found_discoveries = 0

    async def create_job(self, submission: BootstrapSubmission) -> dict[str, Any]:
        self.create_calls += 1
        self.submission = submission
        raise BootstrapUnavailable("simulated lost create response")

    async def discover_job(self, worker_job_id: str) -> dict[str, Any]:
        self.discover_calls += 1
        if self.discover_calls <= self.unavailable_discoveries:
            raise BootstrapUnavailable("simulated temporary discovery outage")
        if self.not_found_discoveries > 0:
            self.not_found_discoveries -= 1
            raise BootstrapJobNotFound("simulated create still settling")
        assert self.submission is not None and worker_job_id == self.submission.job_id
        return {
            "job_id": worker_job_id,
            "state": self.view["state"],
            "worker_instance_id": self.instance_id,
        }

    async def cancel_job(self, worker_job_id: str, worker_instance_id: str) -> dict[str, Any]:
        self.cancel_calls += 1
        return await super().cancel_job(worker_job_id, worker_instance_id)


def test_worker_terminal_result_ttl_exceeds_backend_recovery_horizon() -> None:
    recovery_horizon = WORKER_OVERALL_TIMEOUT_SECONDS + SYNC_FAILURE_GRACE_SECONDS
    assert MIN_TERMINAL_RESULT_TTL_SECONDS == MIN_WORKER_TERMINAL_RESULT_TTL_SECONDS
    assert MIN_TERMINAL_RESULT_TTL_SECONDS >= 1200
    assert recovery_horizon < MIN_TERMINAL_RESULT_TTL_SECONDS


@pytest.fixture()
def bootstrap_components(
    tmp_path: Path,
) -> tuple[Database, FakeBootstrapClient, BootstrapCoordinator]:
    database = Database(tmp_path / "restream.db")
    database.migrate()
    client = FakeBootstrapClient()
    coordinator = BootstrapCoordinator(
        database,
        NodeService(database),
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    return database, client, coordinator


def test_secret_models_never_reveal_passwords_in_repr() -> None:
    marker = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"
    request = BootstrapRequest(
        address="example.test",
        username="root",
        password=marker,
    )
    sudo = SudoPasswordRequest(sudo_password=marker)
    submission = BootstrapSubmission(
        job_id=str(uuid4()),
        node_id=str(uuid4()),
        address="example.test",
        port=22,
        username="root",
        password=SecretStr(marker),
        expected_host_fingerprint=None,
        pinned_host_fingerprint=None,
        control_url="https://restream.example.test",
        node_agent_image="node:test",
        node_agent_environment="test",
    )

    assert marker not in repr(request)
    assert marker not in repr(sudo)
    assert marker not in repr(submission)
    assert submission.recover_failed_install is False
    worker_payload = submission.worker_payload()
    assert worker_payload["recover_failed_install"] is False
    assert "rotate_existing_credential" not in worker_payload


@pytest.mark.asyncio()
async def test_coordinator_create_is_tokenless_and_never_persists_password(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    marker = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"

    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr(marker),
        expected_host_fingerprint=None,
    )
    assert accepted["state"] == "queued"
    assert client.submission is not None
    assert client.submission.recover_failed_install is False
    assert client.submission.password.get_secret_value() == ""

    persisted = database.path.read_bytes()
    assert marker.encode() not in persisted
    with database.connect() as connection:
        enrollment = connection.execute(
            "SELECT token_digest FROM node_enrollment_tokens"
        ).fetchone()
        assert enrollment is None

    job = await coordinator.get_job(accepted["job_id"])
    assert job["state"] == "checking_system"
    assert job["steps"] == [{"name": "checking_system", "state": "running"}]
    assert marker not in json.dumps(job)


@pytest.mark.asyncio()
async def test_jit_enrollment_is_issued_after_slow_preflight_and_replaces_expired_token(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 8, 16, 0, 0, tzinfo=UTC)]
    database = Database(tmp_path / "jit-restream.db")
    database.migrate()
    nodes = NodeService(database, clock=lambda: current[0])
    client = FakeBootstrapClient()
    coordinator = BootstrapCoordinator(
        database,
        nodes,
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        node_id = str(
            connection.execute(
                "SELECT node_id FROM node_install_jobs WHERE id = ?",
                (accepted["job_id"],),
            ).fetchone()["node_id"]
        )
        assert connection.execute("SELECT 1 FROM node_enrollment_tokens").fetchone() is None

    prior = nodes.issue_enrollment(node_id)
    current[0] += timedelta(seconds=601)
    client.view = {
        **client.view,
        "state": "needs_enrollment_token",
        "current_step": "agent_install",
        "enrollment_token_received": False,
    }
    await asyncio.gather(
        coordinator.sync_active_jobs_once(),
        coordinator.sync_active_jobs_once(),
    )

    assert client.enrollment_token_calls == 1
    fresh = client.last_enrollment_token
    assert len(fresh) >= 32 and fresh != prior
    assert fresh.encode() not in database.path.read_bytes()
    with pytest.raises(EnrollmentTokenError):
        nodes.enroll(
            prior,
            public_ip="198.51.100.10",
            profile={
                "agent_version": "0.1.0",
                "protocol_version": 1,
                "hostname": "node-01",
                "os_name": "Ubuntu",
                "os_version": "24.04",
                "architecture": "amd64",
                "cpu_count": 2,
                "memory_total_bytes": 2_147_483_648,
                "memory_available_bytes": 1_073_741_824,
                "disk_total_bytes": 40_000_000_000,
                "disk_free_bytes": 30_000_000_000,
                "capabilities": ["ping", "self_test", "ffmpeg", "ffprobe"],
            },
        )
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT created_at, expires_at, used_at FROM node_enrollment_tokens
            WHERE node_id = ? ORDER BY created_at
            """,
            (node_id,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["used_at"] is not None
    assert datetime.fromisoformat(rows[1]["expires_at"]) - datetime.fromisoformat(
        rows[1]["created_at"]
    ) == timedelta(seconds=600)

    await coordinator.sync_active_jobs_once()
    assert client.enrollment_token_calls == 1


@pytest.mark.asyncio()
async def test_native_relay_bootstrap_issues_only_a_scoped_permanent_credential(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "relay-bootstrap.db")
    database.migrate()
    relay_service = RelayService(database, generate_master_key())
    nodes = NodeService(database, relay_payload_tombstone=relay_service.encrypted_empty_payload())
    client = FakeBootstrapClient()
    coordinator = BootstrapCoordinator(
        database,
        nodes,
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    accepted = await coordinator.create_job(
        address="relay.example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
        install_profile="moblin_relay",
    )
    with database.connect() as connection:
        reserved = connection.execute(
            """
            SELECT node.node_kind, job.install_profile, relay.node_id AS relay_node_id
            FROM node_install_jobs AS job
            JOIN restream_nodes AS node ON node.id = job.node_id
            JOIN relay_nodes AS relay ON relay.node_id = node.id
            WHERE job.id = ?
            """,
            (accepted["job_id"],),
        ).fetchone()
    assert reserved["node_kind"] == "moblin_relay"
    assert reserved["install_profile"] == "moblin_relay"
    assert reserved["relay_node_id"] is not None
    client.view = {
        **client.view,
        "state": "needs_enrollment_token",
        "current_step": "agent_install",
        "enrollment_token_received": False,
    }

    await coordinator.sync_active_jobs_once()

    raw_token = client.last_enrollment_token
    assert raw_token.startswith("node_")
    assert raw_token.encode() not in database.path.read_bytes()
    authenticated = relay_service.authenticate(raw_token)
    assert authenticated["node_kind"] == "moblin_relay"
    with pytest.raises(NodeAuthenticationError):
        nodes.authenticate(raw_token)
    with database.connect() as connection:
        job = connection.execute(
            "SELECT node_id, install_profile FROM node_install_jobs WHERE id = ?",
            (accepted["job_id"],),
        ).fetchone()
        node = connection.execute(
            "SELECT node_kind FROM restream_nodes WHERE id = ?",
            (job["node_id"],),
        ).fetchone()
        enrollment_count = connection.execute(
            "SELECT COUNT(*) FROM node_enrollment_tokens WHERE node_id = ?",
            (job["node_id"],),
        ).fetchone()[0]
        relay_count = connection.execute(
            "SELECT COUNT(*) FROM relay_nodes WHERE node_id = ?",
            (job["node_id"],),
        ).fetchone()[0]
    assert job["install_profile"] == "moblin_relay"
    assert node["node_kind"] == "moblin_relay"
    assert enrollment_count == 0
    assert relay_count == 1

    cancelled = await coordinator.cancel_job(accepted["job_id"])
    assert cancelled["state"] == "cancelled"
    with pytest.raises(RelayAuthenticationError):
        relay_service.authenticate(raw_token)


@pytest.mark.parametrize("action", ["get", "cancel"])
async def test_lost_create_response_is_discovered_before_immediate_get_or_cancel(
    tmp_path: Path,
    action: str,
) -> None:
    database = Database(tmp_path / f"lost-create-{action}.db")
    database.migrate()
    client = UncertainCreateClient(unavailable_discoveries=1)
    coordinator = BootstrapCoordinator(
        database,
        NodeService(database),
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        before = connection.execute(
            "SELECT state, worker_job_id FROM node_install_jobs WHERE id = ?",
            (accepted["job_id"],),
        ).fetchone()
    assert before["state"] == "queued"
    assert before["worker_job_id"] is None

    if action == "get":
        result = await coordinator.get_job(accepted["job_id"])
        assert result["state"] == "checking_system"
    else:
        result = await coordinator.cancel_job(accepted["job_id"])
        assert result["state"] == "cancelled"

    with database.connect() as connection:
        after = connection.execute(
            "SELECT state, worker_job_id FROM node_install_jobs WHERE id = ?",
            (accepted["job_id"],),
        ).fetchone()
        assert (
            connection.execute("SELECT COUNT(*) AS count FROM restream_nodes").fetchone()["count"]
            == 1
        )
    assert after["worker_job_id"] == f"{client.instance_id}/{accepted['job_id']}"
    assert after["state"] != "failed"
    assert client.create_calls == 1
    assert client.cancel_calls == (1 if action == "cancel" else 0)


async def test_restart_discovers_and_cancels_null_identity_before_failing_job(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "restart-null-identity.db")
    database.migrate()
    client = UncertainCreateClient(unavailable_discoveries=1)
    first = BootstrapCoordinator(
        database,
        NodeService(database),
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    accepted = await first.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    client.not_found_discoveries = 2
    restarted = BootstrapCoordinator(
        database,
        NodeService(database),
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )

    await restarted.recover_interrupted_jobs()

    saved = restarted._job(accepted["job_id"])
    assert saved is not None
    assert saved["state"] == "failed"
    assert saved["worker_job_id"] == f"{client.instance_id}/{accepted['job_id']}"
    assert client.cancel_calls == 1
    assert client.discover_calls >= 4


@pytest.mark.asyncio()
async def test_only_one_bootstrap_job_may_be_active(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    _, _, coordinator = bootstrap_components
    await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )

    with pytest.raises(BootstrapJobConflict):
        await coordinator.create_job(
            address="other.example.test",
            port=22,
            username="root",
            password=SecretStr("temporary"),
            expected_host_fingerprint=None,
        )


@pytest.mark.asyncio()
async def test_active_job_discovery_syncs_worker_and_clears_after_terminal_state(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    _, client, coordinator = bootstrap_components
    assert await coordinator.get_active_job() is None

    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    client.view = {
        **client.view,
        "state": "needs_sudo_password",
        "current_step": "checking_privileges",
        "progress_percent": 20,
    }

    active = await coordinator.get_active_job()
    assert active is not None
    assert active["job_id"] == accepted["job_id"]
    assert active["state"] == "needs_sudo_password"

    await coordinator.cancel_job(accepted["job_id"])
    assert await coordinator.get_active_job() is None


@pytest.mark.asyncio()
async def test_long_uds_outage_keeps_active_job_recoverable_and_cancelable(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    original_get_job = client.get_job

    async def unavailable(_: str, __: str) -> dict[str, Any]:
        raise BootstrapUnavailable("temporary UDS outage")

    client.get_job = unavailable  # type: ignore[method-assign]
    # This represents an outage already longer than the old 30-second grace.
    coordinator._sync_failures[accepted["job_id"]] = (0.0, 99)
    await coordinator.sync_active_jobs_once()

    with database.connect() as connection:
        persisted = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
    assert persisted["state"] == "queued"
    discovered = await coordinator.get_active_job()
    assert discovered is not None and discovered["job_id"] == accepted["job_id"]
    with pytest.raises(BootstrapJobConflict):
        await coordinator.create_job(
            address="other.example.test",
            port=22,
            username="root",
            password=SecretStr("other-temporary"),
            expected_host_fingerprint=None,
        )

    client.get_job = original_get_job  # type: ignore[method-assign]
    client.view = {
        **client.view,
        "state": "checking_docker",
        "current_step": "checking_docker",
        "progress_percent": 48,
    }
    await coordinator.sync_active_jobs_once()
    recovered = await coordinator.get_active_job()
    assert recovered is not None and recovered["state"] == "checking_docker"
    cancelled = await coordinator.cancel_job(accepted["job_id"])
    assert cancelled["state"] == "cancelled"


@pytest.mark.asyncio()
async def test_unreachable_worker_fails_only_after_overall_deadline_and_retries(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE node_install_jobs SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", accepted["job_id"]),
        )

    async def unavailable(_: str, __: str) -> dict[str, Any]:
        raise BootstrapUnavailable("temporary UDS outage")

    client.get_job = unavailable  # type: ignore[method-assign]
    await coordinator.sync_active_jobs_once()
    await coordinator.sync_active_jobs_once()
    assert (await coordinator.get_active_job()) is not None
    await coordinator.sync_active_jobs_once()

    failed = await coordinator.get_job(accepted["job_id"])
    assert failed["state"] == "failed"
    assert failed["safe_error"]["code"] == "bootstrap_unavailable"


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    "malformed",
    [None, [], {"state": "queued"}, {"job_id": "not-a-uuid", "state": "queued"}],
)
async def test_malformed_worker_acceptance_fails_job_and_does_not_wedge_coordinator(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
    malformed: object,
) -> None:
    database, client, coordinator = bootstrap_components
    valid_create = client.create_job
    attempts = 0

    async def create_job(submission: BootstrapSubmission) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return malformed
        return await valid_create(submission)

    client.create_job = create_job  # type: ignore[method-assign]
    with pytest.raises(BootstrapUnavailable):
        await coordinator.create_job(
            address="example.test",
            port=22,
            username="root",
            password=SecretStr("first-temporary"),
            expected_host_fingerprint=None,
        )

    with database.connect() as connection:
        first_job = connection.execute(
            "SELECT state, safe_error_code FROM node_install_jobs ORDER BY created_at LIMIT 1"
        ).fetchone()
        node = connection.execute("SELECT status FROM restream_nodes").fetchone()
    assert dict(first_job) == {
        "state": "failed",
        "safe_error_code": "bootstrap_unavailable",
    }
    assert node["status"] == "failed"

    retry = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=None,
    )
    assert retry["state"] == "queued"


@pytest.mark.asyncio()
async def test_coordinator_syncs_worker_and_completes_enrollment_without_browser_polling(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        node_id = str(
            connection.execute(
                "SELECT node_id FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
            ).fetchone()["node_id"]
        )
    client.view = {
        **client.view,
        "state": "waiting_for_enrollment",
        "current_step": "waiting_for_enrollment",
        "progress_percent": 85,
    }

    await coordinator.sync_active_jobs_once()
    with database.connect() as connection:
        persisted = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
        connection.execute(
            "UPDATE restream_nodes SET status = 'ready', last_seen_at = ? WHERE id = ?",
            ("2026-08-16T00:00:00+00:00", node_id),
        )
    assert persisted["state"] == "waiting_for_enrollment"

    await asyncio.gather(
        coordinator.notify_enrollment_completed(node_id),
        coordinator.notify_enrollment_completed(node_id),
    )
    with database.connect() as connection:
        completed = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
        node = connection.execute(
            "SELECT status FROM restream_nodes WHERE id = ?", (node_id,)
        ).fetchone()
    assert completed["state"] == "completed"
    assert client.enrollment_completed_calls == 1
    assert node["status"] == "ready"


@pytest.mark.asyncio()
async def test_running_final_check_does_not_repeat_enrollment_callback(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        node_id = str(
            connection.execute(
                "SELECT node_id FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
            ).fetchone()["node_id"]
        )
    client.view = {
        **client.view,
        "state": "running_self_test",
        "current_step": "running_self_test",
        "progress_percent": 95,
    }

    await coordinator.notify_enrollment_completed(node_id)

    assert client.enrollment_completed_calls == 0
    with database.connect() as connection:
        job = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
    assert job["state"] == "running_self_test"


@pytest.mark.asyncio()
async def test_verified_host_key_is_durable_before_worker_ack(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    client.view = {
        **client.view,
        "state": "authenticating",
        "current_step": "ssh_connect",
        "progress_percent": 10,
    }

    async def assert_persisted_before_ack(_: str, __: str) -> dict[str, Any]:
        with database.connect() as connection:
            node = connection.execute(
                """
                SELECT host_key_algorithm, host_key_fingerprint, host_key_trust_mode
                FROM restream_nodes
                """
            ).fetchone()
        assert dict(node) == {
            "host_key_algorithm": "ssh-ed25519",
            "host_key_fingerprint": "SHA256:" + "A" * 43,
            "host_key_trust_mode": "tofu",
        }
        client.host_key_persisted_calls += 1
        return client.view

    client.host_key_persisted = assert_persisted_before_ack  # type: ignore[method-assign]
    await coordinator.sync_active_jobs_once()

    assert client.host_key_persisted_calls == 1
    persisted = coordinator._job(accepted["job_id"])
    assert persisted is not None
    assert persisted["state"] == "authenticating"


@pytest.mark.asyncio()
async def test_cancelled_bootstrap_stays_failed_when_stale_progress_arrives(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        node_id = str(
            connection.execute(
                "SELECT node_id FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
            ).fetchone()["node_id"]
        )
    await coordinator.cancel_for_node(node_id)
    client.view = {
        **client.view,
        "state": "waiting_for_enrollment",
        "current_step": "waiting_for_enrollment",
    }

    await coordinator.sync_active_jobs_once()
    assert coordinator.nodes.get_node(node_id)["status"] == "failed"  # type: ignore[index]

    with database.connect() as connection:
        job = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
    assert job["state"] == "cancelled"
    assert coordinator.nodes.get_node(node_id)["status"] == "failed"  # type: ignore[index]


@pytest.mark.asyncio()
async def test_worker_instance_mismatch_fails_persisted_job(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )

    async def restarted(_: str, __: str) -> dict[str, Any]:
        raise BootstrapWorkerRestarted("worker restarted")

    client.get_job = restarted  # type: ignore[method-assign]
    job = await coordinator.get_job(accepted["job_id"])

    assert job["state"] == "failed"
    assert job["safe_error"]["code"] == "bootstrap_worker_restarted"
    with database.connect() as connection:
        node = connection.execute("SELECT status FROM restream_nodes").fetchone()
    assert node["status"] == "failed"


@pytest.mark.asyncio()
async def test_backend_restart_fails_active_job_and_allows_new_password_submission(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    first = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("first-temporary"),
        expected_host_fingerprint=None,
    )

    restarted = BootstrapCoordinator(
        database,
        NodeService(database),
        client,  # type: ignore[arg-type]
        control_url="https://restream.example.test",
        node_agent_image="ghcr.io/example/node@sha256:" + "1" * 64,
    )
    await restarted.recover_interrupted_jobs()

    persisted = restarted._job(first["job_id"])
    assert persisted is not None
    assert persisted["state"] == "failed"
    assert persisted["safe_error_code"] == "bootstrap_worker_restarted"
    second = await restarted.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=None,
    )
    assert second["state"] == "queued"
    assert client.submission is not None
    assert client.submission.pinned_host_fingerprint == "SHA256:" + "A" * 43
    assert client.submission.recover_failed_install is True
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) AS count FROM restream_nodes").fetchone()["count"]
            == 1
        )
        node = connection.execute(
            "SELECT hostname, resolved_ip, host_key_fingerprint FROM restream_nodes"
        ).fetchone()
    assert dict(node) == {
        "hostname": "edge-01",
        "resolved_ip": "8.8.8.8",
        "host_key_fingerprint": "SHA256:" + "A" * 43,
    }


@pytest.mark.asyncio()
async def test_backend_restart_recovers_installing_node_without_a_job(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    node = coordinator.nodes.create_pending_node(
        display_name="server-01",
        address="example.test",
        resolved_ip="",
        ssh_port=22,
        ssh_username="root",
        host_key_fingerprint=None,
        host_key_trust_mode=None,
        node_id=str(uuid4()),
    )
    coordinator.nodes.issue_enrollment(str(node["id"]))

    await coordinator.recover_interrupted_jobs()

    with database.connect() as connection:
        saved = connection.execute(
            "SELECT status FROM restream_nodes WHERE id = ?", (node["id"],)
        ).fetchone()
        token = connection.execute(
            "SELECT used_at FROM node_enrollment_tokens WHERE node_id = ?", (node["id"],)
        ).fetchone()
    assert saved["status"] == "failed"
    assert token["used_at"] is not None

    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("replacement-temporary"),
        expected_host_fingerprint=None,
    )
    assert accepted["state"] == "queued"
    assert client.submission is not None
    assert client.submission.recover_failed_install is True


@pytest.mark.asyncio()
async def test_atomic_bootstrap_reservation_failure_is_recovered_without_backend_restart(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, client, coordinator = bootstrap_components
    reserve = coordinator.nodes.create_pending_bootstrap_node

    def fail_insert(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr(coordinator.nodes, "create_pending_bootstrap_node", fail_insert)
    with pytest.raises(BootstrapUnavailable):
        await coordinator.create_job(
            address="example.test",
            port=22,
            username="root",
            password=SecretStr("first-temporary"),
            expected_host_fingerprint=None,
        )

    with database.connect() as connection:
        nodes = connection.execute("SELECT COUNT(*) AS count FROM restream_nodes").fetchone()
        relays = connection.execute("SELECT COUNT(*) AS count FROM relay_nodes").fetchone()
        jobs = connection.execute("SELECT COUNT(*) AS count FROM node_install_jobs").fetchone()
    assert nodes["count"] == 0
    assert relays["count"] == 0
    assert jobs["count"] == 0

    monkeypatch.setattr(coordinator.nodes, "create_pending_bootstrap_node", reserve)
    retry = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=None,
    )
    assert retry["state"] == "queued"
    assert client.submission is not None
    assert client.submission.recover_failed_install is False


@pytest.mark.asyncio()
async def test_unverified_expected_host_key_is_not_pinned_across_retry(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    wrong = "SHA256:" + "B" * 43
    corrected = "SHA256:" + "C" * 43
    first = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("first-temporary"),
        expected_host_fingerprint=wrong,
    )
    with database.connect() as connection:
        node = connection.execute(
            "SELECT host_key_fingerprint, host_key_trust_mode FROM restream_nodes"
        ).fetchone()
    assert dict(node) == {"host_key_fingerprint": None, "host_key_trust_mode": None}

    async def mismatch(_: str, __: str) -> dict[str, Any]:
        raise BootstrapWorkerRestarted("host key verification did not complete")

    client.get_job = mismatch  # type: ignore[method-assign]
    failed = await coordinator.get_job(first["job_id"])
    assert failed["state"] == "failed"

    second = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=corrected,
    )
    assert second["state"] == "queued"
    assert client.submission is not None
    assert client.submission.expected_host_fingerprint == corrected
    assert client.submission.pinned_host_fingerprint is None
    assert client.submission.recover_failed_install is True


@pytest.mark.asyncio()
async def test_failed_retry_reuses_node_and_pins_the_first_verified_host_key(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    first = await coordinator.create_job(
        address="Example.Test.",
        port=22,
        username="root",
        password=SecretStr("first-temporary"),
        expected_host_fingerprint=None,
    )
    await coordinator.get_job(first["job_id"])
    first_submission = client.submission
    assert first_submission is not None
    first_node_id = first_submission.node_id

    async def restarted(_: str, __: str) -> dict[str, Any]:
        raise BootstrapWorkerRestarted("worker restarted")

    client.get_job = restarted  # type: ignore[method-assign]
    await coordinator.get_job(first["job_id"])

    second = await coordinator.create_job(
        address="example.test",
        port=22,
        username="admin",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=None,
    )
    assert second["state"] == "queued"
    assert client.submission is not None
    assert client.submission.node_id == first_node_id
    assert client.submission.pinned_host_fingerprint == "SHA256:" + "A" * 43
    assert client.submission.recover_failed_install is True
    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM restream_nodes").fetchone()
    assert count["count"] == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example.COM.", "example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
        ("ci-ssh-target", "ci-ssh-target"),
    ],
)
def test_ssh_target_identity_is_canonical_before_persistence(value: str, expected: str) -> None:
    assert canonicalize_ssh_address(value) == expected


@pytest.mark.asyncio()
async def test_post_enrollment_failed_retry_enters_remote_state_recovery(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    first = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("first-temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        node_id = str(
            connection.execute(
                "SELECT node_id FROM node_install_jobs WHERE id = ?", (first["job_id"],)
            ).fetchone()["node_id"]
        )
        connection.execute(
            """
            INSERT INTO node_credentials(node_id, token_digest, issued_at)
            VALUES (?, ?, '2026-08-16T00:00:00+00:00')
            """,
            (node_id, digest_opaque_token("previous-node-token")),
        )

    async def restarted(_: str, __: str) -> dict[str, Any]:
        raise BootstrapWorkerRestarted("worker restarted")

    client.get_job = restarted  # type: ignore[method-assign]
    failed = await coordinator.get_job(first["job_id"])
    assert failed["state"] == "failed"

    second = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("second-temporary"),
        expected_host_fingerprint=None,
    )

    assert second["state"] == "queued"
    assert client.submission is not None
    assert client.submission.node_id == node_id
    assert client.submission.recover_failed_install is True


@pytest.mark.asyncio()
async def test_docker_install_side_effect_survives_worker_ttl_view_loss(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    _, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    client.view = {
        **client.view,
        "state": "failed",
        "current_step": "installing_docker",
        "progress_percent": 100,
        "docker_install_started": True,
        "safe_error": {
            "code": "docker_install_failed",
            "message": "Docker install failed",
        },
    }

    await coordinator.sync_active_jobs_once()
    persisted = await coordinator.get_job(accepted["job_id"])

    assert persisted["state"] == "failed"
    assert persisted["docker_install_started"] is True


@pytest.mark.asyncio()
async def test_terminal_worker_result_survives_long_sync_outage_until_backend_reconnects(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        job = connection.execute(
            "SELECT node_id FROM node_install_jobs WHERE id = ?",
            (accepted["job_id"],),
        ).fetchone()
        node_id = str(job["node_id"])
        connection.execute(
            """
            INSERT INTO node_credentials(node_id, token_digest, issued_at)
            VALUES (?, ?, '2026-08-16T00:00:00+00:00')
            """,
            (node_id, digest_opaque_token("terminal-result-token")),
        )
        six_hundred_seconds_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        connection.execute(
            "UPDATE node_install_jobs SET created_at = ?, updated_at = ? WHERE id = ?",
            (six_hundred_seconds_ago, six_hundred_seconds_ago, accepted["job_id"]),
        )

    original_get_job = client.get_job

    async def unavailable(_: str, __: str) -> dict[str, Any]:
        raise BootstrapUnavailable("temporary worker transport outage")

    client.get_job = unavailable  # type: ignore[method-assign]
    for _ in range(3):
        await coordinator.sync_active_jobs_once()

    with database.connect() as connection:
        during_outage = connection.execute(
            "SELECT state FROM node_install_jobs WHERE id = ?",
            (accepted["job_id"],),
        ).fetchone()
        credential = connection.execute(
            "SELECT revoked_at FROM node_credentials WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    assert during_outage["state"] == "queued"
    assert credential["revoked_at"] is None

    client.view = {
        **client.view,
        "state": "completed",
        "current_step": "completed",
        "progress_percent": 100,
    }
    client.get_job = original_get_job  # type: ignore[method-assign]
    await coordinator.sync_active_jobs_once()

    reconnected = await coordinator.get_job(accepted["job_id"])
    with database.connect() as connection:
        credential = connection.execute(
            "SELECT revoked_at FROM node_credentials WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    assert reconnected["state"] == "completed"
    assert credential["revoked_at"] is None


@pytest.mark.asyncio()
@pytest.mark.parametrize("terminal", ["worker_restart", "cancelled"])
async def test_unsuccessful_terminal_bootstrap_revokes_enrolled_node_atomically(
    bootstrap_components: tuple[Database, FakeBootstrapClient, BootstrapCoordinator],
    terminal: str,
) -> None:
    database, client, coordinator = bootstrap_components
    accepted = await coordinator.create_job(
        address="example.test",
        port=22,
        username="root",
        password=SecretStr("temporary"),
        expected_host_fingerprint=None,
    )
    with database.connect() as connection:
        job = connection.execute(
            "SELECT node_id FROM node_install_jobs WHERE id = ?", (accepted["job_id"],)
        ).fetchone()
        node_id = str(job["node_id"])
        connection.execute(
            """
            INSERT INTO node_credentials(node_id, token_digest, issued_at)
            VALUES (?, ?, '2026-08-16T00:00:00+00:00')
            """,
            (node_id, digest_opaque_token("node-token")),
        )
        connection.execute(
            "UPDATE restream_nodes SET status = 'ready', protocol_version = 1 WHERE id = ?",
            (node_id,),
        )
    command = NodeService(database).create_command(node_id, "PING")

    if terminal == "worker_restart":

        async def restarted(_: str, __: str) -> dict[str, Any]:
            raise BootstrapWorkerRestarted("worker restarted")

        client.get_job = restarted  # type: ignore[method-assign]
        result = await coordinator.get_job(accepted["job_id"])
    else:
        result = await coordinator.cancel_job(accepted["job_id"])

    assert result["state"] == ("failed" if terminal == "worker_restart" else "cancelled")
    with database.connect() as connection:
        node = connection.execute(
            "SELECT status FROM restream_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        credential = connection.execute(
            "SELECT revoked_at FROM node_credentials WHERE node_id = ?", (node_id,)
        ).fetchone()
        saved_command = connection.execute(
            "SELECT state FROM node_commands WHERE id = ?", (command["id"],)
        ).fetchone()
    assert node["status"] == "failed"
    assert credential["revoked_at"] is not None
    assert saved_command["state"] == "cancelled"


@pytest.mark.asyncio()
async def test_uds_client_pins_worker_instance_and_maps_restart() -> None:
    secret = "bootstrap-secret-that-is-long-enough"
    instance = str(uuid4())
    job_id = str(uuid4())
    seen_headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.url.path == "/health/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "worker_instance_id": instance,
                    "started_at": "now",
                    "terminal_ttl_seconds": 1200,
                },
            )
        if request.url.path.endswith("/host-key-persisted"):
            return httpx.Response(202, json={"state": "authenticating"})
        return httpx.Response(
            409,
            json={
                "safe_error": {
                    "code": "bootstrap_worker_restarted",
                    "message": "worker restarted",
                }
            },
        )

    client = BootstrapClient(
        Path("/run/adojapan-bootstrap/bootstrap.sock"),
        secret,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.healthy() is True
        acknowledged = await client.host_key_persisted(job_id, instance)
        assert acknowledged == {"state": "authenticating"}
        with pytest.raises(BootstrapWorkerRestarted):
            await client.get_job(job_id, instance)
    finally:
        await client.close()

    assert seen_headers[0]["x-bootstrap-secret"] == secret
    assert "x-bootstrap-worker-instance" not in seen_headers[0]
    for headers in seen_headers[1:]:
        assert headers["x-bootstrap-secret"] == secret
        assert headers["x-bootstrap-worker-instance"] == instance


def test_bootstrap_limiter_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter((0.0, 1.0, 2.0, 12.0))
    monkeypatch.setattr("app.bootstrap_api.monotonic", lambda: next(timestamps))
    limiter = BootstrapRateLimiter(attempts=2, window_seconds=10)

    assert limiter.allow("client") is True
    assert limiter.allow("client") is True
    assert limiter.allow("client") is False
    assert limiter.allow("client") is True
