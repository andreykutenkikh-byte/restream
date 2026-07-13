from __future__ import annotations

import ipaddress
import json
import time
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.validation import ValidatedDestinationURL
from app.main import create_app
from app.services.mediamtx import IngestState, IngestStatus


class FakeMediaMTX:
    def __init__(self) -> None:
        self.status = IngestStatus(IngestState.OFFLINE, message="Incoming stream is offline")
        self.kicked: list[str] = []

    async def get_ingest_status(self, _: str) -> IngestStatus:
        return self.status

    async def kick_publishers(self, path_name: str) -> int:
        self.kicked.append(path_name)
        return 1


class FailingKickMediaMTX(FakeMediaMTX):
    async def kick_publishers(self, path_name: str) -> int:
        raise RuntimeError("control API unavailable")


def allow_test_destination(value: str) -> ValidatedDestinationURL:
    scheme = value.split(":", 1)[0]
    return ValidatedDestinationURL(
        value=value,
        scheme=scheme,
        hostname="example.test",
        port=443 if scheme == "rtmps" else 1935,
        resolved_addresses=(ipaddress.ip_address("8.8.8.8"),),
    )


def login(client: TestClient, settings: Settings, password: str) -> tuple[str, Any]:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return response.json()["csrf_token"], response


def wait_for_state(client: TestClient, destination_id: int, state: str) -> None:
    for _ in range(30):
        response = client.get("/api/destinations")
        item = next(item for item in response.json()["items"] if item["id"] == destination_id)
        if item["state"] == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"Destination did not reach {state}")


def test_full_administrative_smoke_flow(settings: Settings, admin_password: str) -> None:
    media = FakeMediaMTX()
    app = create_app(
        settings,
        mediamtx=media,  # type: ignore[arg-type]
        url_validator=allow_test_destination,
    )
    with TestClient(app) as client:
        live = client.get("/health/live")
        assert live.json() == {"status": "ok"}
        assert live.headers["x-content-type-options"] == "nosniff"
        assert "'unsafe-inline'" not in live.headers["content-security-policy"]
        assert client.get("/health/ready").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        assert client.get("/api/ingest").status_code == 401
        assert client.get("/").history[0].status_code == 303

        csrf, _ = login(client, settings, admin_password)
        ingest = client.get("/api/ingest")
        assert ingest.status_code == 200
        initial_key = ingest.json()["stream_key"]
        assert ingest.json()["rtmp_server_url"] == "rtmp://testserver/live"

        auth_payload = {
            "user": "",
            "password": "",
            "token": "",
            "ip": "172.18.0.1",
            "action": "publish",
            "path": f"live/{initial_key}",
            "protocol": "rtmp",
            "id": None,
            "query": "",
            "userAgent": "FMLE/3.0 (compatible; Lavf)",
        }
        assert (
            client.post(
                "/internal/mediamtx/auth",
                json=auth_payload,
                headers={"Host": "backend:8000"},
            ).status_code
            == 204
        )
        assert (
            client.post(
                "/internal/mediamtx/auth",
                json=auth_payload,
                headers={"X-Forwarded-For": "203.0.113.10"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/internal/mediamtx/auth",
                json={**auth_payload, "path": "live/arbitrary"},
            ).status_code
            == 401
        )
        reader_payload = {
            **auth_payload,
            "action": "read",
            "user": settings.worker_auth_user,
            "password": "wrong-worker-password",
        }
        assert client.post("/internal/mediamtx/auth", json=reader_payload).status_code == 401
        assert (
            client.post(
                "/internal/mediamtx/auth",
                json={**reader_payload, "password": settings.worker_auth_password},
            ).status_code
            == 204
        )

        destination_secret = "test-destination-secret"
        create_payload = {
            "name": "YouTube",
            "server_url": "rtmps://example.test/live2",
            "stream_key": destination_secret,
            "enabled": False,
        }
        assert client.post("/api/destinations", json=create_payload).status_code == 403
        created = client.post(
            "/api/destinations",
            json=create_payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        destination_id = created.json()["id"]
        assert "stream_key" not in created.json()
        assert destination_secret not in json.dumps(created.json())

        listed = client.get("/api/destinations")
        assert listed.status_code == 200
        assert destination_secret not in json.dumps(listed.json())
        assert settings.worker_auth_password not in json.dumps(listed.json())
        assert settings.worker_auth_password not in client.get("/").text

        started = client.post(
            f"/api/destinations/{destination_id}/start",
            headers={"X-CSRF-Token": csrf},
        )
        assert started.status_code == 200
        wait_for_state(client, destination_id, "waiting_for_input")

        stopped = client.post(
            f"/api/destinations/{destination_id}/stop",
            headers={"X-CSRF-Token": csrf},
        )
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "stopped"

        rotated = client.post("/api/ingest/rotate", headers={"X-CSRF-Token": csrf})
        assert rotated.status_code == 200
        replacement_key = rotated.json()["stream_key"]
        assert replacement_key != initial_key
        assert media.kicked == [f"live/{initial_key}"]
        assert client.post("/internal/mediamtx/auth", json=auth_payload).status_code == 401
        assert (
            client.post(
                "/internal/mediamtx/auth",
                json={**auth_payload, "path": f"live/{replacement_key}"},
            ).status_code
            == 204
        )

        deleted = client.delete(
            f"/api/destinations/{destination_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert deleted.status_code == 204

        logged_out = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
        )
        assert logged_out.status_code == 200
        assert client.get("/api/destinations").status_code == 401


def test_login_failure_and_destination_limit(settings: Settings, admin_password: str) -> None:
    app = create_app(
        settings,
        mediamtx=FakeMediaMTX(),  # type: ignore[arg-type]
        url_validator=allow_test_destination,
    )
    with TestClient(app) as client:
        failed = client.post(
            "/api/auth/login",
            json={"login": settings.admin_login, "password": "wrong password"},
        )
        assert failed.status_code == 401
        csrf, _ = login(client, settings, admin_password)
        for index in range(settings.max_destinations):
            response = client.post(
                "/api/destinations",
                json={
                    "name": f"Destination {index}",
                    "server_url": f"rtmp://example.test/live/{index}",
                    "stream_key": f"key-{index}",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 201
        limited = client.post(
            "/api/destinations",
            json={
                "name": "Too many",
                "server_url": "rtmp://example.test/live/last",
                "stream_key": "another-key",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert limited.status_code == 409


def test_failed_publisher_kick_rolls_back_rotation(settings: Settings, admin_password: str) -> None:
    app = create_app(
        settings,
        mediamtx=FailingKickMediaMTX(),  # type: ignore[arg-type]
        url_validator=allow_test_destination,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        csrf, _ = login(client, settings, admin_password)
        initial_key = client.get("/api/ingest").json()["stream_key"]
        failed = client.post("/api/ingest/rotate", headers={"X-CSRF-Token": csrf})
        assert failed.status_code == 500
        assert client.get("/api/ingest").json()["stream_key"] == initial_key
