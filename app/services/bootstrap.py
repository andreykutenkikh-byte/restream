"""Secret-safe client and persisted coordination for the SSH bootstrap worker."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Final, cast
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr

from app.core.redaction import redact_text
from app.core.ssh_target import canonicalize_ssh_address
from app.db import Database
from app.services.nodes import NodeService

LOGGER = logging.getLogger(__name__)

ACTIVE_JOB_STATES: Final = frozenset(
    {
        "queued",
        "resolving",
        "connecting",
        "verifying_host_key",
        "authenticating",
        "checking_privileges",
        "needs_sudo_password",
        "checking_system",
        "checking_resources",
        "checking_docker",
        "installing_docker",
        "needs_enrollment_token",
        "preparing_agent",
        "installing_agent",
        "waiting_for_enrollment",
        "running_self_test",
        "cancelling",
    }
)
TERMINAL_JOB_STATES: Final = frozenset({"completed", "cancelled", "failed"})
ALL_JOB_STATES: Final = ACTIVE_JOB_STATES | TERMINAL_JOB_STATES
_SAFE_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
SYNC_FAILURE_GRACE_SECONDS: Final = 30.0
SYNC_FAILURE_MIN_ATTEMPTS: Final = 3
WORKER_OVERALL_TIMEOUT_SECONDS: Final = 900.0
MIN_WORKER_TERMINAL_RESULT_TTL_SECONDS: Final = 1200.0
CREATE_SETTLE_GRACE_SECONDS: Final = 2.0
CREATE_SETTLE_POLL_SECONDS: Final = 0.1


def _now() -> str:
    return datetime.now(UTC).isoformat()


class BootstrapError(RuntimeError):
    """Base error whose message is safe to expose."""

    code = "bootstrap_unavailable"


class BootstrapUnavailable(BootstrapError):
    code = "bootstrap_unavailable"


class BootstrapWorkerProtocolError(BootstrapUnavailable):
    """The authenticated worker answered, but its response violated the contract."""


class BootstrapWorkerRestarted(BootstrapError):
    code = "bootstrap_worker_restarted"


class BootstrapRejected(BootstrapError):
    code = "bootstrap_rejected"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(_safe_message(message))
        self.code = code if _SAFE_ERROR_CODE.fullmatch(code) else "bootstrap_rejected"


class BootstrapJobConflict(BootstrapError):
    code = "bootstrap_job_conflict"


class BootstrapJobNotFound(BootstrapError):
    code = "bootstrap_job_not_found"


@dataclass(slots=True)
class BootstrapSubmission:
    job_id: str
    node_id: str
    address: str
    port: int
    username: str
    password: SecretStr
    expected_host_fingerprint: str | None
    pinned_host_fingerprint: str | None
    control_url: str
    node_agent_image: str
    node_agent_environment: str = "production"
    install_profile: str = "generic_node"
    recover_failed_install: bool = False
    adopt_empty_managed_root_for_test: bool = False

    def worker_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "node_id": self.node_id,
            "address": self.address,
            "port": self.port,
            "username": self.username,
            "password": self.password.get_secret_value(),
            "expected_host_fingerprint": self.expected_host_fingerprint,
            "pinned_host_fingerprint": self.pinned_host_fingerprint,
            "control_url": self.control_url,
            "node_agent_image": self.node_agent_image,
            "node_agent_environment": self.node_agent_environment,
            "install_profile": self.install_profile,
            "recover_failed_install": self.recover_failed_install,
            "adopt_empty_managed_root_for_test": self.adopt_empty_managed_root_for_test,
        }


def _safe_message(value: Any) -> str:
    text = redact_text(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 240:
        return "Не удалось выполнить безопасную установку сервера."
    return text or "Не удалось выполнить безопасную установку сервера."


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapWorkerProtocolError("Bootstrap Worker returned an invalid response")
    return cast(dict[str, Any], value)


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, Any], value)


class BootstrapClient:
    """HTTP-over-UDS client which never logs request bodies or response payloads."""

    def __init__(
        self,
        socket_path: Path,
        secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret = secret
        uds_transport = transport or httpx.AsyncHTTPTransport(uds=str(socket_path))
        self._client = httpx.AsyncClient(
            base_url="http://bootstrap-worker",
            transport=uds_transport,
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            response = await self._client.get(
                "/health/ready",
                timeout=1.0,
                headers=self._headers(),
            )
            if response.status_code != 200:
                return False
            payload = _mapping(response.json())
            ttl = payload.get("terminal_ttl_seconds")
            return (
                payload.get("status") == "ok"
                and bool(payload.get("worker_instance_id"))
                and isinstance(ttl, (int, float))
                and not isinstance(ttl, bool)
                and float(ttl) >= MIN_WORKER_TERMINAL_RESULT_TTL_SECONDS
            )
        except (httpx.HTTPError, ValueError, BootstrapError):
            return False

    def _headers(self, worker_instance_id: str | None = None) -> dict[str, str]:
        headers = {"X-Bootstrap-Secret": self._secret}
        if worker_instance_id:
            headers["X-Bootstrap-Worker-Instance"] = worker_instance_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        worker_instance_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                path,
                headers=self._headers(worker_instance_id),
                json=dict(payload) if payload is not None else None,
            )
        except httpx.HTTPError as exc:
            raise BootstrapUnavailable("Bootstrap Worker is unavailable") from exc
        try:
            body = _mapping(response.json())
        except (ValueError, BootstrapError) as exc:
            raise BootstrapWorkerProtocolError(
                "Bootstrap Worker returned an invalid response"
            ) from exc
        if response.is_success:
            return body
        error = (
            _mapping_or_empty(body.get("safe_error"))
            or _mapping_or_empty(body.get("error"))
            or body
        )
        code = str(error.get("code", "bootstrap_rejected"))
        message = str(error.get("message", "Bootstrap Worker rejected the request"))
        if response.status_code == 409 and code == "bootstrap_worker_restarted":
            raise BootstrapWorkerRestarted("Bootstrap Worker restarted")
        if response.status_code == 404:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        raise BootstrapRejected(code, message)

    async def create_job(self, submission: BootstrapSubmission) -> dict[str, Any]:
        return await self._request("POST", "/v1/jobs", payload=submission.worker_payload())

    async def discover_job(self, worker_job_id: str) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request("GET", f"/v1/jobs/{worker_job_id}/accepted")

    async def get_job(self, worker_job_id: str, worker_instance_id: str) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "GET",
            f"/v1/jobs/{worker_job_id}",
            worker_instance_id=worker_instance_id,
        )

    async def provide_sudo_password(
        self,
        worker_job_id: str,
        worker_instance_id: str,
        password: SecretStr,
    ) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "POST",
            f"/v1/jobs/{worker_job_id}/sudo-password",
            worker_instance_id=worker_instance_id,
            payload={"sudo_password": password.get_secret_value()},
        )

    async def provide_enrollment_token(
        self,
        worker_job_id: str,
        worker_instance_id: str,
        enrollment_token: SecretStr,
    ) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "POST",
            f"/v1/jobs/{worker_job_id}/enrollment-token",
            worker_instance_id=worker_instance_id,
            payload={"enrollment_token": enrollment_token.get_secret_value()},
        )

    async def host_key_persisted(
        self,
        worker_job_id: str,
        worker_instance_id: str,
    ) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "POST",
            f"/v1/jobs/{worker_job_id}/host-key-persisted",
            worker_instance_id=worker_instance_id,
        )

    async def cancel_job(self, worker_job_id: str, worker_instance_id: str) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "POST",
            f"/v1/jobs/{worker_job_id}/cancel",
            worker_instance_id=worker_instance_id,
        )

    async def enrollment_completed(
        self, worker_job_id: str, worker_instance_id: str
    ) -> dict[str, Any]:
        UUID(worker_job_id)
        return await self._request(
            "POST",
            f"/v1/jobs/{worker_job_id}/enrollment-completed",
            worker_instance_id=worker_instance_id,
        )


class BootstrapCoordinator:
    """Persists safe job state while ephemeral credentials stay in the worker."""

    def __init__(
        self,
        database: Database,
        nodes: NodeService,
        client: BootstrapClient,
        *,
        control_url: str,
        node_agent_image: str,
        node_agent_environment: str = "production",
    ) -> None:
        self.database = database
        self.nodes = nodes
        self.client = client
        self.control_url = control_url
        self.node_agent_image = node_agent_image
        self.node_agent_environment = node_agent_environment
        self._create_lock = asyncio.Lock()
        self._enrollment_lock = asyncio.Lock()
        self._worker_coordination_lock = asyncio.Lock()
        self._sync_failures: dict[str, tuple[float, int]] = {}
        self._uncertain_creates: dict[str, float] = {}

    async def close(self) -> None:
        await self.client.close()

    async def healthy(self) -> bool:
        return await self.client.healthy()

    async def recover_interrupted_jobs(self) -> None:
        """Fail persisted active jobs and best-effort cancel a surviving worker task."""

        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM node_install_jobs
                WHERE state IN ({placeholders}) ORDER BY created_at ASC
                """,  # noqa: S608 - placeholders are generated only from a fixed state set
                tuple(sorted(ACTIVE_JOB_STATES)),
            ).fetchall()
        for row in rows:
            job = dict(row)
            reference = str(row["worker_job_id"] or "")
            instance, separator, worker_job = reference.partition("/")
            if separator != "/":
                try:
                    accepted = await self._discover_for_startup_recovery(str(row["id"]))
                    worker_job, instance = self._attach_worker_identity(
                        str(row["id"]),
                        accepted,
                    )
                    separator = "/"
                except (ValueError, TypeError, BootstrapError):
                    pass
            if separator == "/":
                try:
                    UUID(instance)
                    UUID(worker_job)
                    view = await self.client.get_job(worker_job, instance)
                    self._persist_worker_facts(job, view)
                except (ValueError, TypeError, BootstrapError):
                    pass
                try:
                    cancelled = await self.client.cancel_job(worker_job, instance)
                    self._persist_worker_facts(job, cancelled)
                except (ValueError, TypeError, BootstrapError):
                    pass
            self._fail_job(
                str(row["id"]),
                "bootstrap_worker_restarted",
                "Bootstrap coordinator restarted",
            )
        self._fail_orphaned_installing_nodes()

    def _fail_orphaned_installing_nodes(self) -> None:
        """Recover a node left between creation and durable job registration."""

        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        now = _now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT node.id FROM restream_nodes AS node
                WHERE node.status = 'installing'
                  AND NOT EXISTS (
                    SELECT 1 FROM node_install_jobs AS job
                    WHERE job.node_id = node.id AND job.state IN ({placeholders})
                  )
                """,  # noqa: S608 - placeholders come only from fixed states
                tuple(sorted(ACTIVE_JOB_STATES)),
            ).fetchall()
            for row in rows:
                self._revoke_incomplete_install(connection, str(row["id"]), now)
            connection.execute("COMMIT")

    def _active_job_exists(self) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM node_install_jobs WHERE state IN ({placeholders}) LIMIT 1",  # noqa: S608
                tuple(sorted(ACTIVE_JOB_STATES)),
            ).fetchone()
        return row is not None

    def _active_jobs(self, *, node_id: str | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        parameters: list[Any] = list(sorted(ACTIVE_JOB_STATES))
        node_clause = ""
        if node_id is not None:
            node_clause = " AND node_id = ?"
            parameters.append(node_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM node_install_jobs
                WHERE state IN ({placeholders}){node_clause}
                ORDER BY created_at ASC
                """,  # noqa: S608 - SQL fragments come only from fixed constants above
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def _next_display_name(self) -> str:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM restream_nodes").fetchone()
        return f"server-{int(row['count']) + 1:02d}"

    def _retryable_node(
        self,
        *,
        address: str,
        port: int,
        username: str,
        install_profile: str,
    ) -> dict[str, Any] | None:
        """Reuse only a failed record so host-key pinning survives a retry."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM restream_nodes
                WHERE lower(address) = lower(?) AND ssh_port = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (address, port),
            ).fetchone()
            if row is None:
                return None
            node = dict(row)
            if node["status"] != "failed":
                raise BootstrapJobConflict("This server is already registered")
            if node["node_kind"] != install_profile:
                raise BootstrapJobConflict(
                    "This server was registered with a different install profile"
                )
            now = _now()
            connection.execute(
                """
                UPDATE restream_nodes
                SET status = 'installing', ssh_username = ?, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (username, now, node["id"]),
            )
        node["status"] = "installing"
        node["ssh_username"] = username
        return node

    def _insert_job(self, job_id: str, node_id: str, install_profile: str) -> None:
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO node_install_jobs(
                    id, node_id, install_profile, state, current_step, progress_percent,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 'queued', 0, ?, ?)
                """,
                (job_id, node_id, install_profile, now, now),
            )

    def _job(self, job_id: str) -> dict[str, Any] | None:
        try:
            UUID(job_id)
        except ValueError:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_install_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def _worker_identity(self, job: Mapping[str, Any]) -> tuple[str, str]:
        reference = str(job.get("worker_job_id") or "")
        instance, separator, worker_job = reference.partition("/")
        if separator != "/":
            raise BootstrapWorkerRestarted("Bootstrap Worker job identity was lost")
        UUID(instance)
        UUID(worker_job)
        return instance, worker_job

    async def _ensure_worker_identity(self, job: Mapping[str, Any]) -> tuple[str, str]:
        """Attach a worker UUID after a lost create response, then return it."""

        if job.get("worker_job_id"):
            return self._worker_identity(job)
        job_id = str(job["id"])
        try:
            accepted = await self.client.discover_job(job_id)
        except BootstrapJobNotFound as exc:
            deadline = self._uncertain_creates.get(job_id)
            if deadline is not None and monotonic() < deadline:
                raise BootstrapUnavailable("Bootstrap create outcome is still settling") from exc
            raise
        worker_job, instance = self._attach_worker_identity(job_id, accepted)
        return instance, worker_job

    async def _settle_uncertain_create(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Bound authenticated 404s while an accepted create may still finish."""

        job_id = str(job["id"])
        while True:
            try:
                return await self.client.discover_job(job_id)
            except BootstrapJobNotFound:
                deadline = self._uncertain_creates.get(job_id, 0.0)
                if monotonic() >= deadline:
                    raise
                await asyncio.sleep(CREATE_SETTLE_POLL_SECONDS)

    async def _discover_for_startup_recovery(self, job_id: str) -> dict[str, Any]:
        """Give an in-flight timed-out create a bounded chance to become visible."""

        deadline = monotonic() + CREATE_SETTLE_GRACE_SECONDS
        while True:
            try:
                return await self.client.discover_job(job_id)
            except (BootstrapJobNotFound, BootstrapUnavailable):
                if monotonic() >= deadline:
                    raise
                await asyncio.sleep(CREATE_SETTLE_POLL_SECONDS)

    @staticmethod
    def _accepted_worker_identity(
        accepted: object,
        *,
        expected_job_id: str,
    ) -> tuple[str, str]:
        """Validate the authenticated worker's 202 without leaking malformed state."""

        if not isinstance(accepted, Mapping):
            raise BootstrapWorkerProtocolError("Bootstrap Worker returned an invalid job identity")
        worker_job_id = str(accepted.get("job_id", ""))
        worker_instance_id = str(accepted.get("worker_instance_id", ""))
        try:
            UUID(worker_job_id)
            UUID(worker_instance_id)
        except (TypeError, ValueError) as exc:
            raise BootstrapWorkerProtocolError(
                "Bootstrap Worker returned an invalid job identity"
            ) from exc
        if worker_job_id != expected_job_id or str(accepted.get("state", "")) not in ALL_JOB_STATES:
            raise BootstrapWorkerProtocolError("Bootstrap Worker returned an invalid job identity")
        return worker_job_id, worker_instance_id

    def _attach_worker_identity(
        self,
        job_id: str,
        accepted: object,
    ) -> tuple[str, str]:
        worker_job_id, worker_instance_id = self._accepted_worker_identity(
            accepted,
            expected_job_id=job_id,
        )
        now = _now()
        reference = f"{worker_instance_id}/{worker_job_id}"
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, worker_job_id FROM node_install_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise BootstrapJobNotFound("Bootstrap job was not found")
            if str(row["state"]) not in ACTIVE_JOB_STATES:
                connection.execute("ROLLBACK")
                raise BootstrapRejected("invalid_job_state", "Bootstrap job is already complete")
            current_reference = str(row["worker_job_id"] or "")
            if current_reference and current_reference != reference:
                connection.execute("ROLLBACK")
                raise BootstrapWorkerProtocolError("Bootstrap Worker job identity changed")
            updated = connection.execute(
                f"""
                UPDATE node_install_jobs SET worker_job_id = ?, updated_at = ?
                WHERE id = ? AND state IN ({placeholders})
                  AND (worker_job_id IS NULL OR worker_job_id = ?)
                """,  # noqa: S608 - placeholders come only from fixed states
                (reference, now, job_id, *sorted(ACTIVE_JOB_STATES), reference),
            )
            if updated.rowcount != 1:
                connection.execute("ROLLBACK")
                raise BootstrapRejected(
                    "bootstrap_job_cancelled",
                    "Bootstrap job was cancelled before worker acceptance",
                )
            connection.execute("COMMIT")
        self._uncertain_creates.pop(job_id, None)
        return worker_job_id, worker_instance_id

    def _revoke_incomplete_install(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        now: str,
    ) -> None:
        """Make a rolled-back enrollment unusable in the same transaction."""
        self.nodes.revoke_incomplete_bootstrap(connection, node_id, now)

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
        now = _now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT node_id, state FROM node_install_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return
            persisted_state = str(row["state"])
            if persisted_state in TERMINAL_JOB_STATES:
                if persisted_state in {"cancelled", "failed"}:
                    self._revoke_incomplete_install(connection, str(row["node_id"]), now)
                connection.execute("COMMIT")
                return
            connection.execute(
                """
                UPDATE node_install_jobs
                SET state = 'failed', current_step = 'failed', safe_error_code = ?,
                    safe_error_message = ?, progress_percent = 100,
                    updated_at = ?, finished_at = ? WHERE id = ?
                """,
                (code, _safe_message(message), now, now, job_id),
            )
            self._revoke_incomplete_install(connection, str(row["node_id"]), now)
            connection.execute("COMMIT")

    def _mark_cancel_requested(self, job_id: str) -> dict[str, Any]:
        """Persist cancellation before an uncertain worker identity is attached."""

        now = _now()
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            connection.execute(
                f"""
                UPDATE node_install_jobs
                SET state = 'cancelling', current_step = 'cancelling', updated_at = ?
                WHERE id = ? AND state IN ({placeholders})
                """,  # noqa: S608 - placeholders come only from fixed states
                (now, job_id, *sorted(ACTIVE_JOB_STATES)),
            )
        job = self._job(job_id)
        if job is None:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        return job

    def _safe_job_view(
        self, job: Mapping[str, Any], worker_view: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        state = str((worker_view or {}).get("state") or job["state"])
        if state not in ALL_JOB_STATES:
            state = "failed"
        safe_error = None
        code = (worker_view or {}).get("safe_error_code") or job.get("safe_error_code")
        message = (worker_view or {}).get("safe_error_message") or job.get("safe_error_message")
        nested_error = (worker_view or {}).get("safe_error")
        if isinstance(nested_error, Mapping):
            code = nested_error.get("code", code)
            message = nested_error.get("message", message)
        if code or message:
            safe_error = {
                "code": str(code) if _SAFE_ERROR_CODE.fullmatch(str(code)) else "bootstrap_failed",
                "message": _safe_message(message),
            }
        raw_steps = (worker_view or {}).get("steps", [])
        steps: list[dict[str, str]] = []
        if isinstance(raw_steps, list):
            for step in raw_steps[:32]:
                if not isinstance(step, Mapping):
                    continue
                name = str(step.get("name", ""))
                step_state = str(step.get("state", "pending"))
                if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) and step_state in {
                    "pending",
                    "running",
                    "completed",
                    "failed",
                    "skipped",
                }:
                    steps.append({"name": name, "state": step_state})
        return {
            "job_id": str(job["id"]),
            "node_id": str(job["node_id"]),
            "install_profile": str(job.get("install_profile", "generic_node")),
            "state": state,
            "current_step": str((worker_view or {}).get("current_step") or job["current_step"]),
            "progress_percent": int(
                (worker_view or {}).get("progress_percent") or job["progress_percent"]
            ),
            "steps": steps,
            "safe_error": safe_error,
            "docker_install_started": bool(
                (worker_view or {}).get("docker_install_started")
                or job.get("docker_install_started")
            ),
            "created_at": str(job["created_at"]),
            "updated_at": str(job["updated_at"]),
            "finished_at": job.get("finished_at"),
        }

    async def create_job(
        self,
        *,
        address: str,
        port: int,
        username: str,
        password: SecretStr,
        expected_host_fingerprint: str | None,
        install_profile: str = "generic_node",
    ) -> dict[str, Any]:
        async with self._create_lock:
            if install_profile not in {"generic_node", "moblin_relay"}:
                raise BootstrapRejected("invalid_install_profile", "Install profile is invalid")
            try:
                address = canonicalize_ssh_address(address)
            except ValueError as exc:
                raise BootstrapRejected("invalid_target", "SSH target is invalid") from exc
            # A prior in-process persistence failure must not wedge the canonical
            # target until the next backend restart.
            self._fail_orphaned_installing_nodes()
            if self._active_job_exists():
                raise BootstrapJobConflict("Another bootstrap job is already active")
            job_id = str(uuid4())
            try:
                node = self._retryable_node(
                    address=address,
                    port=port,
                    username=username,
                    install_profile=install_profile,
                )
                retrying_failed_node = node is not None
                if node is None:
                    node = self.nodes.create_pending_bootstrap_node(
                        job_id=job_id,
                        install_profile=cast(Any, install_profile),
                        display_name=self._next_display_name(),
                        address=address,
                        resolved_ip="",
                        ssh_port=port,
                        ssh_username=username,
                        node_id=str(uuid4()),
                    )
                node_id = str(node["id"])
                if retrying_failed_node:
                    self._insert_job(job_id, node_id, install_profile)
            except (BootstrapError, sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
                persisted = self._job(job_id)
                if persisted is not None:
                    self._fail_job(
                        job_id,
                        "bootstrap_unavailable",
                        "Bootstrap job persistence failed",
                    )
                else:
                    self._fail_orphaned_installing_nodes()
                if isinstance(exc, BootstrapError):
                    raise
                raise BootstrapUnavailable("Bootstrap job could not be persisted") from exc
            submission = BootstrapSubmission(
                job_id=job_id,
                node_id=node_id,
                address=address,
                port=port,
                username=username,
                password=password,
                expected_host_fingerprint=expected_host_fingerprint,
                pinned_host_fingerprint=(
                    str(node["host_key_fingerprint"]) if node.get("host_key_fingerprint") else None
                ),
                control_url=self.control_url,
                node_agent_image=self.node_agent_image,
                node_agent_environment=self.node_agent_environment,
                install_profile=install_profile,
                recover_failed_install=retrying_failed_node,
                adopt_empty_managed_root_for_test=self.node_agent_environment == "test",
            )
            try:
                try:
                    accepted = await self.client.create_job(submission)
                except BootstrapUnavailable:
                    # The 202 may have been lost after the worker accepted the
                    # caller UUID. Discover first; retrying create is safe and
                    # idempotent only after the worker proves that UUID absent.
                    self._uncertain_creates[job_id] = monotonic() + CREATE_SETTLE_GRACE_SECONDS
                    try:
                        accepted = await self._settle_uncertain_create(self._job(job_id) or {})
                    except BootstrapJobNotFound:
                        try:
                            accepted = await self.client.create_job(submission)
                        except BootstrapUnavailable:
                            return {
                                "job_id": job_id,
                                "state": "queued",
                                "install_profile": install_profile,
                            }
                    except BootstrapUnavailable:
                        return {
                            "job_id": job_id,
                            "state": "queued",
                            "install_profile": install_profile,
                        }
                worker_job_id, worker_instance_id = self._attach_worker_identity(job_id, accepted)
            except BootstrapError as exc:
                self._fail_job(job_id, exc.code, str(exc))
                raise
            finally:
                submission.password = SecretStr("")
            return {
                "job_id": job_id,
                "state": "queued",
                "install_profile": install_profile,
            }

    @staticmethod
    def _update_node_from_worker(
        connection: sqlite3.Connection,
        *,
        node_id: str,
        view: Mapping[str, Any],
        now: str,
        state: str = "",
    ) -> None:
        """Persist only verified, secret-free facts reported by the worker."""

        target = _mapping_or_empty(view.get("target"))
        host_key = _mapping_or_empty(view.get("host_key"))
        system = _mapping_or_empty(view.get("system"))
        connection.execute(
            """
            UPDATE restream_nodes SET
                resolved_ip = COALESCE(NULLIF(?, ''), resolved_ip),
                host_key_algorithm = COALESCE(NULLIF(?, ''), host_key_algorithm),
                host_key_fingerprint = COALESCE(NULLIF(?, ''), host_key_fingerprint),
                host_key_trust_mode = COALESCE(NULLIF(?, ''), host_key_trust_mode),
                hostname = COALESCE(NULLIF(?, ''), hostname),
                os_name = COALESCE(NULLIF(?, ''), os_name),
                os_version = COALESCE(NULLIF(?, ''), os_version),
                architecture = COALESCE(NULLIF(?, ''), architecture),
                cpu_count = COALESCE(?, cpu_count),
                memory_total_bytes = COALESCE(?, memory_total_bytes),
                memory_available_bytes = COALESCE(?, memory_available_bytes),
                disk_total_bytes = COALESCE(?, disk_total_bytes),
                disk_free_bytes = COALESCE(?, disk_free_bytes),
                status = CASE
                    WHEN status = 'revoked' THEN status
                    WHEN ? = 'failed' THEN 'failed'
                    WHEN status = 'ready' THEN status
                    WHEN ? IN ('waiting_for_enrollment', 'running_self_test') THEN 'connecting'
                    ELSE status END,
                updated_at = ? WHERE id = ?
            """,
            (
                str(target.get("resolved_ip", "")),
                str(host_key.get("algorithm", "")),
                str(host_key.get("fingerprint", "")),
                str(host_key.get("trust_mode", "")),
                str(system.get("hostname", "")),
                str(system.get("os_name", "")),
                str(system.get("os_version", "")),
                str(system.get("architecture", "")),
                system.get("cpu_count"),
                system.get("memory_total_bytes"),
                system.get("memory_available_bytes"),
                system.get("disk_total_bytes"),
                system.get("disk_free_bytes"),
                state,
                state,
                now,
                node_id,
            ),
        )

    def _persist_worker_facts(self, job: Mapping[str, Any], view: Mapping[str, Any]) -> None:
        """Checkpoint verified host facts without adopting the worker job state."""

        now = _now()
        with self.database.connect() as connection:
            self._update_node_from_worker(
                connection,
                node_id=str(job["node_id"]),
                view=view,
                now=now,
            )

    def _persist_worker_view(self, job: Mapping[str, Any], view: Mapping[str, Any]) -> None:
        state = str(view.get("state", ""))
        if state not in ALL_JOB_STATES:
            raise BootstrapWorkerProtocolError("Bootstrap Worker returned an invalid job state")
        progress = max(0, min(100, int(view.get("progress_percent", 0))))
        current_step = str(view.get("current_step", state))
        safe_error = _mapping_or_empty(view.get("safe_error"))
        code = str(safe_error.get("code", "")) or None
        message = _safe_message(safe_error.get("message")) if safe_error else None
        now = _now()
        finished_at = now if state in TERMINAL_JOB_STATES else None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM node_install_jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            if current is None:
                connection.execute("ROLLBACK")
                raise BootstrapJobNotFound("Bootstrap job was not found")
            if str(current["state"]) == "cancelling" and state not in TERMINAL_JOB_STATES:
                connection.execute("ROLLBACK")
                return
            if str(current["state"]) in TERMINAL_JOB_STATES and state != current["state"]:
                connection.execute("ROLLBACK")
                raise BootstrapRejected("invalid_job_state", "Bootstrap job is already complete")
            connection.execute(
                """
                UPDATE node_install_jobs
                SET state = ?, current_step = ?, progress_percent = ?,
                    safe_error_code = ?, safe_error_message = ?, updated_at = ?,
                    finished_at = COALESCE(finished_at, ?),
                    docker_install_started = CASE
                        WHEN ? THEN 1 ELSE docker_install_started END
                WHERE id = ?
                """,
                (
                    state,
                    current_step,
                    progress,
                    code,
                    message,
                    now,
                    finished_at,
                    bool(view.get("docker_install_started", False)),
                    job["id"],
                ),
            )
            self._update_node_from_worker(
                connection,
                node_id=str(job["node_id"]),
                view=view,
                now=now,
                state=state,
            )
            if state in {"cancelled", "failed"}:
                self._revoke_incomplete_install(connection, str(job["node_id"]), now)
            connection.execute("COMMIT")

    async def _persist_and_ack_worker_view(
        self,
        job: Mapping[str, Any],
        view: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Durably pin the verified host key before allowing any remote command."""

        async with self._worker_coordination_lock:
            self._persist_worker_view(job, view)
            instance, worker_job = await self._ensure_worker_identity(job)
            state = str(view.get("state", ""))
            if state == "authenticating":
                host_key = _mapping_or_empty(view.get("host_key"))
                if not host_key.get("fingerprint"):
                    return view
                view = await self.client.host_key_persisted(worker_job, instance)
                self._persist_worker_view(job, view)
                state = str(view.get("state", ""))

            if state != "needs_enrollment_token" or bool(
                view.get("enrollment_token_received", False)
            ):
                return view

            # Re-read under the coordination lock so browser polling and the
            # background monitor cannot issue competing one-time credentials.
            latest = await self.client.get_job(worker_job, instance)
            self._persist_worker_view(job, latest)
            if str(latest.get("state", "")) != "needs_enrollment_token" or bool(
                latest.get("enrollment_token_received", False)
            ):
                return latest

            if str(job.get("install_profile", "generic_node")) == "moblin_relay":
                raw_token = self.nodes.issue_relay_bootstrap_credential(
                    str(job["node_id"]),
                    str(job["id"]),
                )
            else:
                raw_token = self.nodes.issue_enrollment(str(job["node_id"]))
            enrollment_token = SecretStr(raw_token)
            raw_token = ""
            try:
                acknowledged = await self.client.provide_enrollment_token(
                    worker_job,
                    instance,
                    enrollment_token,
                )
            finally:
                enrollment_token = SecretStr("")
            self._persist_worker_view(job, acknowledged)
            return acknowledged

    async def sync_active_jobs_once(self) -> None:
        """Persist worker progress without relying on an open browser poller."""

        for job in self._active_jobs():
            job_id = str(job["id"])
            if not job.get("worker_job_id"):
                # Ordinary NULL identities are still inside create_job. Only an
                # explicitly uncertain response-loss handshake is discoverable
                # by the monitor; this avoids racing the live create request.
                if job_id not in self._uncertain_creates:
                    continue
                try:
                    await self._ensure_worker_identity(job)
                    job = self._job(job_id) or job
                except BootstrapJobNotFound as exc:
                    self._uncertain_creates.pop(job_id, None)
                    self._fail_job(job_id, exc.code, str(exc))
                    continue
                except BootstrapUnavailable:
                    continue
            try:
                instance, worker_job = await self._ensure_worker_identity(job)
                if str(job.get("state", "")) == "cancelling":
                    view = await self.client.cancel_job(worker_job, instance)
                    self._persist_worker_view(job, view)
                    self._sync_failures.pop(job_id, None)
                    continue
                view = await self.client.get_job(worker_job, instance)
                await self._persist_and_ack_worker_view(job, view)
                self._sync_failures.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except BootstrapWorkerProtocolError as exc:
                self._sync_failures.pop(job_id, None)
                self._fail_job(job_id, exc.code, str(exc))
            except BootstrapUnavailable as exc:
                now = monotonic()
                first_failure, attempts = self._sync_failures.get(job_id, (now, 0))
                attempts += 1
                self._sync_failures[job_id] = (first_failure, attempts)
                if attempts >= SYNC_FAILURE_MIN_ATTEMPTS and self._worker_deadline_expired(job):
                    self._sync_failures.pop(job_id, None)
                    self._fail_job(job_id, exc.code, str(exc))
            except (BootstrapError, ValueError, TypeError) as exc:
                self._sync_failures.pop(job_id, None)
                self._fail_job(job_id, getattr(exc, "code", "bootstrap_worker_invalid"), str(exc))

    async def monitor_active_jobs(self, *, poll_interval_seconds: float = 0.5) -> None:
        """Continuously synchronize the single active worker job into SQLite."""

        while True:
            try:
                await self.sync_active_jobs_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive service-loop isolation
                LOGGER.warning("Bootstrap job synchronization failed")
            await asyncio.sleep(poll_interval_seconds)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if job is None:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        if str(job["state"]) in TERMINAL_JOB_STATES:
            return self._safe_job_view(job)
        if str(job["state"]) == "cancelling":
            try:
                return await self.cancel_job(job_id)
            except BootstrapUnavailable:
                return self._safe_job_view(self._job(job_id) or job)
        try:
            worker_instance, worker_job = await self._ensure_worker_identity(job)
            worker_view = await self.client.get_job(worker_job, worker_instance)
            safe_worker_view = await self._persist_and_ack_worker_view(job, worker_view)
            persisted = self._job(job_id) or job
            return self._safe_job_view(persisted, safe_worker_view)
        except (BootstrapWorkerProtocolError, ValueError, TypeError) as exc:
            self._fail_job(job_id, getattr(exc, "code", "bootstrap_worker_invalid"), str(exc))
            return self._safe_job_view(self._job(job_id) or job)
        except BootstrapUnavailable:
            return self._safe_job_view(self._job(job_id) or job)
        except (BootstrapWorkerRestarted, BootstrapJobNotFound) as exc:
            self._fail_job(job_id, "bootstrap_worker_restarted", str(exc))
            return self._safe_job_view(self._job(job_id) or job)

    async def get_active_job(self) -> dict[str, Any] | None:
        """Discover and synchronize the singleton job after a browser reload."""

        jobs = self._active_jobs()
        if not jobs:
            return None
        if len(jobs) != 1:
            raise BootstrapUnavailable("Multiple active bootstrap jobs were found")
        try:
            return await self.get_job(str(jobs[0]["id"]))
        except BootstrapUnavailable:
            return self._safe_job_view(jobs[0])

    @staticmethod
    def _worker_deadline_expired(job: Mapping[str, Any]) -> bool:
        try:
            created_at = datetime.fromisoformat(str(job["created_at"]))
        except (KeyError, TypeError, ValueError):
            return True
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds()
        return age >= WORKER_OVERALL_TIMEOUT_SECONDS + SYNC_FAILURE_GRACE_SECONDS

    async def provide_sudo_password(self, job_id: str, password: SecretStr) -> dict[str, Any]:
        job = self._job(job_id)
        if job is None:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        if job["state"] != "needs_sudo_password":
            raise BootstrapRejected("invalid_job_state", "Sudo password is not expected")
        instance, worker_job = await self._ensure_worker_identity(job)
        view = await self.client.provide_sudo_password(worker_job, instance, password)
        self._persist_worker_view(job, view)
        return self._safe_job_view(self._job(job_id) or job, view)

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if job is None:
            raise BootstrapJobNotFound("Bootstrap job was not found")
        if str(job["state"]) in TERMINAL_JOB_STATES:
            return self._safe_job_view(job)
        job = self._mark_cancel_requested(job_id)
        try:
            instance, worker_job = await self._ensure_worker_identity(job)
        except BootstrapUnavailable:
            return self._safe_job_view(job)
        except BootstrapJobNotFound as exc:
            self._fail_job(job_id, exc.code, str(exc))
            return self._safe_job_view(self._job(job_id) or job)
        view = await self.client.cancel_job(worker_job, instance)
        self._persist_worker_view(job, view)
        return self._safe_job_view(self._job(job_id) or job, view)

    async def cancel_for_node(self, node_id: str) -> None:
        """Cancel any in-flight bootstrap after the node credential is revoked."""

        for job in self._active_jobs(node_id=node_id):
            try:
                await self.cancel_job(str(job["id"]))
            except asyncio.CancelledError:
                raise
            except BootstrapUnavailable:
                continue
            except (BootstrapError, ValueError, TypeError) as exc:
                self._fail_job(
                    str(job["id"]),
                    getattr(exc, "code", "bootstrap_cancel_failed"),
                    str(exc),
                )

    async def notify_enrollment_completed(self, node_id: str) -> None:
        async with self._enrollment_lock:
            await self._notify_enrollment_completed_locked(node_id)

    async def _notify_enrollment_completed_locked(self, node_id: str) -> None:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT jobs.*, nodes.status AS node_status
                FROM node_install_jobs AS jobs
                JOIN restream_nodes AS nodes ON nodes.id = jobs.node_id
                WHERE jobs.node_id = ? AND jobs.state IN ({placeholders})
                ORDER BY jobs.created_at DESC LIMIT 1
                """,  # noqa: S608 - placeholders are generated only from a fixed state set
                (node_id, *sorted(ACTIVE_JOB_STATES)),
            ).fetchone()
        if row is None or str(row["node_status"]) == "revoked":
            return
        job = dict(row)
        try:
            instance, worker_job = await self._ensure_worker_identity(job)
            current = await self.client.get_job(worker_job, instance)
            self._persist_worker_view(job, current)
            if str(current.get("state", "")) != "waiting_for_enrollment":
                return
            view = await self.client.enrollment_completed(worker_job, instance)
            self._persist_worker_view(job, view)
        except BootstrapUnavailable:
            LOGGER.warning("Bootstrap enrollment synchronization is temporarily unavailable")
        except BootstrapError as exc:
            self._fail_job(str(job["id"]), exc.code, str(exc))


class UnavailableBootstrapCoordinator:
    """Fail-closed bootstrap facade for unit tests and explicitly disabled setups."""

    async def close(self) -> None:
        return None

    async def healthy(self) -> bool:
        return False

    async def recover_interrupted_jobs(self) -> None:
        return None

    async def create_job(self, **_: Any) -> dict[str, Any]:
        raise BootstrapUnavailable("Bootstrap Worker is unavailable")

    async def get_job(self, _: str) -> dict[str, Any]:
        raise BootstrapUnavailable("Bootstrap Worker is unavailable")

    async def get_active_job(self) -> dict[str, Any] | None:
        raise BootstrapUnavailable("Bootstrap Worker is unavailable")

    async def provide_sudo_password(self, _: str, __: SecretStr) -> dict[str, Any]:
        raise BootstrapUnavailable("Bootstrap Worker is unavailable")

    async def cancel_job(self, _: str) -> dict[str, Any]:
        raise BootstrapUnavailable("Bootstrap Worker is unavailable")

    async def cancel_for_node(self, _: str) -> None:
        return None

    async def notify_enrollment_completed(self, _: str) -> None:
        return None


__all__ = [
    "ACTIVE_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "BootstrapClient",
    "BootstrapCoordinator",
    "BootstrapError",
    "BootstrapJobConflict",
    "BootstrapJobNotFound",
    "BootstrapRejected",
    "BootstrapSubmission",
    "BootstrapUnavailable",
    "BootstrapWorkerProtocolError",
    "BootstrapWorkerRestarted",
    "UnavailableBootstrapCoordinator",
]
