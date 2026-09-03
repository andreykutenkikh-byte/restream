from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.services.mediamtx import IngestState, IngestStatus


class OfflineMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


class FakeBootstrap:
    def __init__(self) -> None:
        self.password_seen = False
        self.sudo_seen = False
        self.cancelled = False
        self.active_job_id: str | None = None

    async def healthy(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def create_job(self, **values: Any) -> dict[str, str]:
        password = values.pop("password")
        assert isinstance(password, SecretStr)
        self.password_seen = password.get_secret_value().startswith("CI_SSH_PASSWORD")
        assert values == {
            "address": "203.0.113.10",
            "port": 22,
            "username": "root",
            "expected_host_fingerprint": None,
            "install_profile": "moblin_relay",
        }
        self.active_job_id = "11111111-1111-4111-8111-111111111111"
        return {"job_id": self.active_job_id, "state": "queued"}

    async def get_active_job(self) -> dict[str, Any] | None:
        if self.active_job_id is None:
            return None
        return await self.get_job(self.active_job_id)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "node_id": "22222222-2222-4222-8222-222222222222",
            "state": "needs_sudo_password",
            "current_step": "checking_privileges",
            "progress_percent": 20,
            "steps": [{"name": "checking_privileges", "state": "running"}],
            "safe_error": None,
        }

    async def provide_sudo_password(self, job_id: str, password: SecretStr) -> dict[str, Any]:
        self.sudo_seen = password.get_secret_value() == "sudo-temporary"
        return {**(await self.get_job(job_id)), "state": "checking_system"}

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.cancelled = True
        return {**(await self.get_job(job_id)), "state": "cancelled"}

    async def notify_enrollment_completed(self, _: str) -> None:
        return None


def login(client: TestClient, settings: Settings, password: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def test_servers_page_and_bootstrap_api_are_session_and_csrf_protected(
    settings: Settings,
    admin_password: str,
) -> None:
    bootstrap = FakeBootstrap()
    app = create_app(
        settings,
        mediamtx=OfflineMediaMTX(),  # type: ignore[arg-type]
        bootstrap=bootstrap,
    )
    marker = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"
    payload = {
        "address": "203.0.113.10",
        "port": 22,
        "username": "root",
        "password": marker,
        "expected_host_fingerprint": None,
    }

    with TestClient(app) as client:
        anonymous = client.get("/servers", follow_redirects=False)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/login"
        assert client.post("/api/nodes/bootstrap", json=payload).status_code == 401
        assert client.get("/api/nodes/bootstrap/active").status_code == 401

        csrf = login(client, settings, admin_password)
        page = client.get("/servers")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert 'type="password"' in page.text
        assert 'autocomplete="new-password"' in page.text
        assert marker not in page.text
        assert client.get("/health/ready").json()["bootstrap"] == "ready"
        assert client.get("/api/nodes/bootstrap/active").json() is None

        assert client.post("/api/nodes/bootstrap", json=payload).status_code == 403
        accepted = client.post(
            "/api/nodes/bootstrap",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert accepted.status_code == 202
        assert bootstrap.password_seen is True
        assert marker not in json.dumps(accepted.json())

        job_id = accepted.json()["job_id"]
        active = client.get("/api/nodes/bootstrap/active")
        assert active.status_code == 200
        assert active.json()["job_id"] == job_id
        assert active.json()["state"] == "needs_sudo_password"
        job = client.get(f"/api/nodes/bootstrap/{job_id}")
        assert job.status_code == 200
        assert job.json()["state"] == "needs_sudo_password"
        assert marker not in json.dumps(job.json())

        continued = client.post(
            f"/api/nodes/bootstrap/{job_id}/sudo-password",
            json={"sudo_password": "sudo-temporary"},
            headers={"X-CSRF-Token": csrf},
        )
        assert continued.status_code == 200
        assert bootstrap.sudo_seen is True

        cancelled = client.post(
            f"/api/nodes/bootstrap/{job_id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert cancelled.status_code == 200
        assert bootstrap.cancelled is True


def test_bootstrap_request_rejects_url_and_invalid_port(
    settings: Settings,
    admin_password: str,
) -> None:
    bootstrap = FakeBootstrap()
    app = create_app(
        settings,
        mediamtx=OfflineMediaMTX(),  # type: ignore[arg-type]
        bootstrap=bootstrap,
    )
    with TestClient(app) as client:
        csrf = login(client, settings, admin_password)
        headers = {"X-CSRF-Token": csrf}
        invalid_url = client.post(
            "/api/nodes/bootstrap",
            json={
                "address": "ssh://root@example.test",
                "port": 22,
                "username": "root",
                "password": "temporary",
            },
            headers=headers,
        )
        assert invalid_url.status_code == 422
        invalid_port = client.post(
            "/api/nodes/bootstrap",
            json={
                "address": "example.test",
                "port": 0,
                "username": "root",
                "password": "temporary",
            },
            headers=headers,
        )
        assert invalid_port.status_code == 422
        client_selected_profile = client.post(
            "/api/nodes/bootstrap",
            json={
                "address": "example.test",
                "port": 22,
                "username": "root",
                "password": "temporary",
                "install_profile": "generic_node",
            },
            headers=headers,
        )
        assert client_selected_profile.status_code == 422
        assert bootstrap.password_seen is False
