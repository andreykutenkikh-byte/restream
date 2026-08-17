from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from bootstrap_worker.__main__ import prepare_socket_path
from bootstrap_worker.api import (
    BOOTSTRAP_SECRET_HEADER,
    WORKER_INSTANCE_HEADER,
    WorkerSettings,
    create_app,
)
from bootstrap_worker.jobs import MIN_TERMINAL_RESULT_TTL_SECONDS, JobRecord, JobStore
from bootstrap_worker.models import JobState, TimeoutPolicy
from bootstrap_worker.state_machine import JobStateMachine
from bootstrap_worker.targets import TargetPolicy

IMAGE = f"ghcr.io/andreykutenkikh-byte/restream-node@sha256:{'d' * 64}"
PASSWORD_MARKER = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("172.20.0.9",)


class IdleExecutor:
    def __init__(self) -> None:
        self.timeouts = TimeoutPolicy(overall_seconds=60)

    async def run(self, record: JobRecord) -> None:
        record.transition(JobState.RESOLVING)
        await record.wait_for_enrollment_or_cancel()


def payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "node_id": str(uuid4()),
        "address": "ci-ssh-target",
        "port": 22,
        "username": "root",
        "password": PASSWORD_MARKER,
        "control_url": "http://backend:8000",
        "node_agent_image": IMAGE,
        "node_agent_environment": "test",
    }
    values.update(updates)
    return values


def app_and_store() -> tuple[object, JobStore]:
    selected_policy = TargetPolicy(
        environment="test",
        test_allowlist=("ci-ssh-target:22",),
        resolver=Resolver(),
    )
    store = JobStore(
        target_policy=selected_policy,
        executor=IdleExecutor(),  # type: ignore[arg-type]
    )
    app = create_app(
        settings=WorkerSettings(
            environment="test",
            test_target_allowlist=("ci-ssh-target:22",),
            bootstrap_secret=SecretStr("worker-internal-secret-that-is-long"),
        ),
        store=store,
    )
    return app, store


async def test_liveness_is_open_but_readiness_requires_internal_secret() -> None:
    app, store = app_and_store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["worker_instance_id"] == str(store.instance_id)
        assert (await client.get("/health/ready")).status_code == 401
        assert (
            await client.get(
                "/health/ready",
                headers={BOOTSTRAP_SECRET_HEADER: "mismatched-internal-secret-value"},
            )
        ).status_code == 401
        ready = await client.get(
            "/health/ready",
            headers={BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"},
        )
        assert ready.status_code == 200
        assert ready.json()["worker_instance_id"] == str(store.instance_id)
        assert ready.json()["terminal_ttl_seconds"] == MIN_TERMINAL_RESULT_TTL_SECONDS
        assert (await client.get("/openapi.json")).status_code == 404
    await store.shutdown()


async def test_internal_api_requires_secret_and_never_echoes_request_password() -> None:
    app, store = app_and_store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        denied = await client.post("/v1/jobs", json=payload())
        assert denied.status_code == 401
        assert PASSWORD_MARKER not in denied.text

        accepted = await client.post(
            "/v1/jobs",
            json=payload(),
            headers={BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"},
        )
        assert accepted.status_code == 202
        body = accepted.json()
        assert set(body) == {"job_id", "state", "worker_instance_id"}
        assert PASSWORD_MARKER not in accepted.text
        assert "enrollment-value" not in accepted.text

        status_response = await client.get(
            f"/v1/jobs/{body['job_id']}",
            headers={
                BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long",
                WORKER_INSTANCE_HEADER: body["worker_instance_id"],
            },
        )
        assert status_response.status_code == 200
        assert PASSWORD_MARKER not in status_response.text
        assert "enrollment-value" not in status_response.text
    await store.shutdown()


async def test_create_is_idempotent_by_caller_uuid_and_authenticated_discovery() -> None:
    app, store = app_and_store()
    headers = {BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"}
    job_id = str(uuid4())
    node_id = str(uuid4())
    body = payload(job_id=job_id, node_id=node_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        first = await client.post("/v1/jobs", json=body, headers=headers)
        second = await client.post("/v1/jobs", json=body, headers=headers)
        discovered = await client.get(f"/v1/jobs/{job_id}/accepted", headers=headers)
        mismatched = await client.post(
            "/v1/jobs",
            json={**body, "node_id": str(uuid4())},
            headers=headers,
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert discovered.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"] == discovered.json()["job_id"]
    assert first.json()["worker_instance_id"] == discovered.json()["worker_instance_id"]
    assert len(store._jobs) == 1
    assert mismatched.status_code == 409
    await store.shutdown()


async def test_jit_enrollment_endpoint_is_authenticated_idempotent_and_secret_free() -> None:
    app, store = app_and_store()
    headers = {BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"}
    marker = "jit-enrollment-secret-value-that-must-never-echo"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        accepted = (await client.post("/v1/jobs", json=payload(), headers=headers)).json()
        record = next(iter(store._jobs.values()))
        record.machine = JobStateMachine(JobState.NEEDS_ENROLLMENT_TOKEN)
        instance_headers = {
            **headers,
            WORKER_INSTANCE_HEADER: accepted["worker_instance_id"],
        }
        denied = await client.post(
            f"/v1/jobs/{accepted['job_id']}/enrollment-token",
            json={"enrollment_token": marker},
            headers={WORKER_INSTANCE_HEADER: accepted["worker_instance_id"]},
        )
        supplied = await client.post(
            f"/v1/jobs/{accepted['job_id']}/enrollment-token",
            json={"enrollment_token": marker},
            headers=instance_headers,
        )
        duplicate = await client.post(
            f"/v1/jobs/{accepted['job_id']}/enrollment-token",
            json={"enrollment_token": "different-secret-value-that-is-long-enough"},
            headers=instance_headers,
        )

    assert denied.status_code == 401
    assert supplied.status_code == 202
    assert duplicate.status_code == 202
    assert supplied.json()["enrollment_token_received"] is True
    assert marker not in supplied.text
    assert "different-secret" not in duplicate.text
    assert record.enrollment_tokens.qsize() == 1
    await store.cancel(
        record.job_id,
        expected_instance_id=store.instance_id,
    )
    await store.shutdown()
    assert record.request is None
    assert record.enrollment_tokens.empty()


async def test_validation_error_does_not_echo_secret_input() -> None:
    app, store = app_and_store()
    invalid = payload(username="invalid user", password=PASSWORD_MARKER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        response = await client.post(
            "/v1/jobs",
            json=invalid,
            headers={BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"},
        )
        assert response.status_code == 422
        assert response.json()["safe_error"]["code"] == "invalid_request"
        assert PASSWORD_MARKER not in response.text
        assert "invalid user" not in response.text
    await store.shutdown()


async def test_worker_instance_mismatch_has_explicit_restart_semantics() -> None:
    old_app, old_store = app_and_store()
    new_app, new_store = app_and_store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=old_app),  # type: ignore[arg-type]
        base_url="http://old-worker",
    ) as client:
        accepted = await client.post(
            "/v1/jobs",
            json=payload(),
            headers={BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"},
        )
        body = accepted.json()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=new_app),  # type: ignore[arg-type]
        base_url="http://new-worker",
    ) as client:
        response = await client.get(
            f"/v1/jobs/{body['job_id']}",
            headers={
                BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long",
                WORKER_INSTANCE_HEADER: body["worker_instance_id"],
            },
        )
        assert response.status_code == 409
        assert response.json()["safe_error"]["code"] == "bootstrap_worker_restarted"
        assert response.json()["worker_instance_id"] == str(new_store.instance_id)
    await old_store.shutdown()
    await new_store.shutdown()


async def test_cancel_endpoint_cooperatively_finishes_job() -> None:
    app, store = app_and_store()
    headers = {BOOTSTRAP_SECRET_HEADER: "worker-internal-secret-that-is-long"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://worker",
    ) as client:
        accepted = (await client.post("/v1/jobs", json=payload(), headers=headers)).json()
        instance_headers = {
            **headers,
            WORKER_INSTANCE_HEADER: accepted["worker_instance_id"],
        }
        response = await client.post(
            f"/v1/jobs/{accepted['job_id']}/cancel",
            headers=instance_headers,
        )
        assert response.status_code == 202
        for _ in range(100):
            response = await client.get(
                f"/v1/jobs/{accepted['job_id']}",
                headers=instance_headers,
            )
            if response.json()["state"] == "cancelled":
                break
            await asyncio.sleep(0.005)
        assert response.json()["state"] == "cancelled"
    await store.shutdown()


def test_production_worker_requires_file_backed_internal_secret() -> None:
    try:
        WorkerSettings(environment="production")
    except ValueError as exc:
        assert "BOOTSTRAP_SECRET_FILE" in str(exc)
    else:
        raise AssertionError("production worker accepted a missing internal secret")


def test_worker_settings_fail_closed_for_unknown_environment_and_weak_secret() -> None:
    with pytest.raises(ValueError, match="ENVIRONMENT"):
        WorkerSettings(environment="prod")
    with pytest.raises(ValueError, match="too short"):
        WorkerSettings(environment="production", bootstrap_secret=SecretStr("short"))
    with pytest.raises(ValueError, match="forbidden"):
        WorkerSettings(
            environment="production",
            bootstrap_secret=SecretStr("x" * 32 + "\nembedded"),
        )
    with pytest.raises(ValueError, match="positive"):
        WorkerSettings(terminal_ttl_seconds=float("nan"))
    with pytest.raises(ValueError, match="at least 1200"):
        WorkerSettings(
            environment="production",
            bootstrap_secret=SecretStr("x" * 32),
            terminal_ttl_seconds=1199,
        )


def test_production_worker_enforces_single_active_job() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        WorkerSettings(
            environment="production",
            bootstrap_secret=SecretStr("x" * 32),
            max_active_jobs=2,
        )


def test_socket_path_preparation_rejects_relative_and_regular_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        prepare_socket_path("relative/bootstrap.sock")

    regular_file = tmp_path / "bootstrap.sock"
    regular_file.write_text("not-a-socket", encoding="utf-8")
    with pytest.raises(ValueError, match="not a socket"):
        prepare_socket_path(str(regular_file))

    socket_path = tmp_path / "worker" / "bootstrap.sock"
    assert prepare_socket_path(str(socket_path)) == str(socket_path)
    assert socket_path.parent.is_dir()
