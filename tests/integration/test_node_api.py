from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.node_api import (
    ConcurrentNodeEnrollmentError,
    ConcurrentNodePollError,
    NodeCommandPollGate,
    NodeEnrollmentCapacityError,
    NodeEnrollmentGate,
    NodeEnrollmentRateLimitError,
    NodePollRateLimitError,
)
from app.schemas import NodeEnrollmentRequest
from app.services.mediamtx import IngestState, IngestStatus
from app.services.nodes import EnrollmentTokenError, NodeService


class FakeMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


class StartupOrderingBootstrap:
    def __init__(self) -> None:
        self.monitor_calls = 0

    async def recover_interrupted_jobs(self) -> None:
        return None

    async def monitor_active_jobs(self) -> None:
        self.monitor_calls += 1
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def login(client: TestClient, settings: Settings, password: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def enrollment_payload(token: str) -> dict[str, Any]:
    return {
        "enrollment_token": token,
        "agent_version": "0.1.0",
        "protocol_version": 1,
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


def test_enrollment_secret_repr_and_failures_never_expose_raw_value(
    settings: Settings,
) -> None:
    marker = "enrollment-secret-marker-" + "x" * 32
    parsed = NodeEnrollmentRequest.model_validate(enrollment_payload(marker))
    assert parsed.enrollment_token.get_secret_value() == marker
    assert marker not in repr(parsed)
    assert marker not in str(parsed)
    assert marker not in repr(parsed.model_dump())

    with pytest.raises(ValueError) as validation_error:
        NodeEnrollmentRequest.model_validate({**enrollment_payload(marker), "cpu_count": 0})
    assert marker not in str(validation_error.value)
    assert marker not in repr(validation_error.value)

    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post("/node-api/v1/enroll", json=enrollment_payload(marker))
        assert response.status_code == 401
        assert marker not in response.text

        short_marker = "short-enrollment-secret-marker"
        short_response = client.post(
            "/node-api/v1/enroll",
            json=enrollment_payload(short_marker),
        )
        assert short_response.status_code == 401
        assert short_marker not in short_response.text

    with pytest.raises(EnrollmentTokenError) as domain_error:
        app.state.nodes.enroll(
            marker,
            public_ip="198.51.100.10",
            profile=enrollment_payload(marker),
        )
    assert marker not in str(domain_error.value)
    assert marker not in repr(domain_error.value)


@pytest.mark.asyncio()
async def test_enrollment_gate_is_per_ip_concurrent_rate_and_memory_bounded() -> None:
    now = [0.0]
    gate = NodeEnrollmentGate(
        attempts=2,
        window_seconds=10,
        max_identities=2,
        max_concurrent=2,
        clock=lambda: now[0],
    )

    async with gate.hold("198.51.100.1"):
        with pytest.raises(ConcurrentNodeEnrollmentError):
            async with gate.hold("198.51.100.1"):
                pass
        async with gate.hold("198.51.100.2"):
            with pytest.raises(NodeEnrollmentCapacityError):
                async with gate.hold("198.51.100.3"):
                    pass

    async with gate.hold("198.51.100.1"):
        pass
    with pytest.raises(NodeEnrollmentRateLimitError):
        async with gate.hold("198.51.100.1"):
            pass

    # A different peer retains an independent budget, and old attempts expire.
    async with gate.hold("198.51.100.2"):
        pass
    now[0] = 11.0
    async with gate.hold("198.51.100.1"):
        pass
    async with gate.hold("198.51.100.3"):
        pass
    assert len(gate._attempts) <= 2  # noqa: SLF001


def test_enrollment_endpoint_returns_429_after_per_ip_budget(settings: Settings) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    app.state.node_enrollments = NodeEnrollmentGate(attempts=1, window_seconds=60)
    with TestClient(app) as client:
        first = client.post(
            "/node-api/v1/enroll",
            json=enrollment_payload("invalid-enrollment-token-" + "a" * 32),
        )
        second = client.post(
            "/node-api/v1/enroll",
            json=enrollment_payload("invalid-enrollment-token-" + "b" * 32),
        )
    assert first.status_code == 401
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert second.json()["error"]["code"] == "enrollment_rate_limited"


def heartbeat_payload() -> dict[str, Any]:
    return {
        "agent_version": "0.1.0",
        "protocol_version": 1,
        "hostname": "node-01",
        "uptime_seconds": 100,
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


def create_pending(service: NodeService, *, suffix: int = 10) -> str:
    node = service.create_pending_node(
        display_name=f"server-{suffix}",
        address=f"198.51.100.{suffix}",
        resolved_ip=f"198.51.100.{suffix}",
        ssh_port=22,
        ssh_username="root",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=f"SHA256:test-{suffix}",
        host_key_trust_mode="tofu",
    )
    return str(node["id"])


def enroll(client: TestClient, service: NodeService, node_id: str) -> str:
    one_time = service.issue_enrollment(node_id)
    response = client.post("/node-api/v1/enroll", json=enrollment_payload(one_time))
    assert response.status_code == 200
    assert response.json()["node_id"] == node_id
    return str(response.json()["node_token"])


def test_enrollment_heartbeat_limits_and_replay_are_fail_closed(
    settings: Settings,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    clock = MutableClock(datetime(2026, 8, 16, tzinfo=UTC))
    app.state.nodes = NodeService(app.state.database, clock=clock)
    with TestClient(app) as client:
        service: NodeService = app.state.nodes
        node_id = create_pending(service)
        one_time = service.issue_enrollment(node_id)
        response = client.post("/node-api/v1/enroll", json=enrollment_payload(one_time))
        assert response.status_code == 200
        body = response.json()
        assert body["node_id"] == node_id
        assert body["heartbeat_interval_seconds"] == 5
        assert body["command_poll_interval_seconds"] == 5
        node_token = str(body["node_token"])
        assert node_token.startswith(f"node_{node_id}_")
        assert client.get("/openapi.json").status_code == 404

        replay = client.post("/node-api/v1/enroll", json=enrollment_payload(one_time))
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "enrollment_failed"
        bad = client.post(
            "/node-api/v1/enroll",
            json=enrollment_payload("x" * 43),
        )
        assert bad.status_code == 401

        no_header = client.post(
            "/node-api/v1/heartbeat",
            params={"token": node_token},
            json=heartbeat_payload(),
        )
        assert no_header.status_code == 401
        body_token = client.post(
            "/node-api/v1/heartbeat",
            json={**heartbeat_payload(), "node_token": node_token},
        )
        assert body_token.status_code == 401

        heartbeat = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json=heartbeat_payload(),
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["node_status"] == "ready"
        assert service.get_node(node_id)["public_ip"] == "testclient"  # type: ignore[index]
        assert service.get_node(node_id)["control_latency_ms"] == 12.5  # type: ignore[index]
        rate_limited = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json=heartbeat_payload(),
        )
        assert rate_limited.status_code == 429
        assert rate_limited.headers["retry-after"] == "1"

        invalid_metrics = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json={**heartbeat_payload(), "cpu_percent": 100.1},
        )
        assert invalid_metrics.status_code == 422
        invalid_latency = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json={**heartbeat_payload(), "control_latency_ms": 60_000.1},
        )
        assert invalid_latency.status_code == 422
        raw = json.dumps({**heartbeat_payload(), "unexpected": "x" * 20_000})
        oversized_response = client.post(
            "/node-api/v1/heartbeat",
            headers={
                "Authorization": f"Bearer {node_token}",
                "Content-Type": "application/json",
            },
            content=raw,
        )
        assert oversized_response.status_code == 413
        raw_bytes = raw.encode("utf-8")
        streamed_response = client.post(
            "/node-api/v1/heartbeat",
            headers={
                "Authorization": f"Bearer {node_token}",
                "Content-Type": "application/json",
            },
            content=(chunk for chunk in (raw_bytes[:100], raw_bytes[100:])),
        )
        assert streamed_response.status_code == 413
        assert streamed_response.json()["error"]["code"] == "payload_too_large"


def test_protocol_v2_reaches_domain_409_without_consuming_enrollment(
    settings: Settings,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    app.state.nodes = NodeService(app.state.database)
    with TestClient(app) as client:
        service: NodeService = app.state.nodes
        node_id = create_pending(service)
        one_time = service.issue_enrollment(node_id)
        incompatible_enrollment = client.post(
            "/node-api/v1/enroll",
            json={**enrollment_payload(one_time), "protocol_version": 2},
        )
        assert incompatible_enrollment.status_code == 409
        assert incompatible_enrollment.json()["error"]["code"] == "unsupported_protocol"

        accepted = client.post(
            "/node-api/v1/enroll",
            json=enrollment_payload(one_time),
        )
        assert accepted.status_code == 200
        node_token = str(accepted.json()["node_token"])
        incompatible_heartbeat = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json={**heartbeat_payload(), "protocol_version": 2},
        )
        assert incompatible_heartbeat.status_code == 409
        assert incompatible_heartbeat.json()["error"]["code"] == "unsupported_protocol"
        assert service.get_node(node_id)["last_seen_at"] is None  # type: ignore[index]


def test_admin_and_agent_command_flow_revoke_and_secret_safe_views(
    settings: Settings,
    admin_password: str,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    clock = MutableClock(datetime(2026, 8, 16, tzinfo=UTC))
    app.state.nodes = NodeService(app.state.database, clock=clock)
    with TestClient(app) as client:
        service: NodeService = app.state.nodes
        node_id = create_pending(service)
        node_token = enroll(client, service, node_id)
        service.record_heartbeat(node_token, heartbeat_payload())
        assert client.get("/api/nodes").status_code == 401
        csrf = login(client, settings, admin_password)

        listed = client.get("/api/nodes")
        assert listed.status_code == 200
        serialized = json.dumps(listed.json())
        assert node_token not in serialized
        assert "token_digest" not in serialized
        assert "enrollment_token" not in serialized

        assert (
            client.patch(
                f"/api/nodes/{node_id}",
                json={"display_name": "Tokyo node"},
            ).status_code
            == 403
        )
        renamed = client.patch(
            f"/api/nodes/{node_id}",
            json={"display_name": "Tokyo node"},
            headers={"X-CSRF-Token": csrf},
        )
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Tokyo node"

        queued = client.post(
            f"/api/nodes/{node_id}/self-test",
            headers={"X-CSRF-Token": csrf},
        )
        assert queued.status_code == 202
        command_id = str(queued.json()["id"])
        delivery = client.get(
            "/node-api/v1/commands/next",
            params={"wait": 0},
            headers={"Authorization": f"Bearer {node_token}"},
        )
        assert delivery.status_code == 200
        assert delivery.json()["id"] == command_id
        assert delivery.json()["command_type"] == "SELF_TEST"
        assert delivery.json()["payload"] == {}
        assert delivery.json()["attempt_count"] == 1
        assert client.post(
            f"/node-api/v1/commands/{command_id}/ack",
            headers={"Authorization": f"Bearer {node_token}"},
            json={},
        ).json() == {"status": "acknowledged"}
        result: dict[str, Any] = {
            "status": "ok",
            "completed_at": "2026-08-16T00:00:03Z",
            "agent_version": "0.1.0",
            "checks": {
                "control_https": True,
                "dns": True,
                "ffmpeg": True,
                "ffprobe": True,
                "memory": True,
                "disk": True,
                "data_writable": True,
                "no_inbound_ports": True,
            },
        }
        mismatched_result = {
            **result,
            "checks": {**result["checks"], "dns": False},
        }
        mismatch = client.post(
            f"/node-api/v1/commands/{command_id}/complete",
            headers={"Authorization": f"Bearer {node_token}"},
            json=mismatched_result,
        )
        assert mismatch.status_code == 422
        complete = client.post(
            f"/node-api/v1/commands/{command_id}/complete",
            headers={"Authorization": f"Bearer {node_token}"},
            json=result,
        )
        assert complete.status_code == 200
        assert complete.json() == {"status": "completed"}
        repeated = client.post(
            f"/node-api/v1/commands/{command_id}/complete",
            headers={"Authorization": f"Bearer {node_token}"},
            json=result,
        )
        assert repeated.status_code == 200
        admin_command = client.get(f"/api/nodes/{node_id}/commands/{command_id}")
        assert admin_command.json()["safe_result"]["checks"]["dns"] is True

        pending = client.post(
            f"/api/nodes/{node_id}/ping",
            headers={"X-CSRF-Token": csrf},
        )
        assert pending.status_code == 202
        pending_id = str(pending.json()["id"])
        assert client.post(f"/api/nodes/{node_id}/revoke").status_code == 403
        revoked = client.post(
            f"/api/nodes/{node_id}/revoke",
            headers={"X-CSRF-Token": csrf},
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert service.get_command(node_id, pending_id)["state"] == "cancelled"  # type: ignore[index]
        denied = client.post(
            "/node-api/v1/heartbeat",
            headers={"Authorization": f"Bearer {node_token}"},
            json=heartbeat_payload(),
        )
        assert denied.status_code == 401


def test_command_endpoints_fail_closed_for_persisted_unsupported_protocol(
    settings: Settings,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    app.state.nodes = NodeService(app.state.database)
    with TestClient(app) as client:
        service: NodeService = app.state.nodes
        node_id = create_pending(service)
        node_token = enroll(client, service, node_id)
        with service.database.connect() as connection:
            connection.execute(
                "UPDATE restream_nodes SET protocol_version = 2 WHERE id = ?",
                (node_id,),
            )

        response = client.get(
            "/node-api/v1/commands/next",
            params={"wait": 0},
            headers={"Authorization": f"Bearer {node_token}"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "unsupported_protocol"


def test_node_command_poll_gate_is_single_flight_per_node() -> None:
    async def exercise() -> None:
        now = [0.0]
        gate = NodeCommandPollGate(clock=lambda: now[0])
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_first() -> None:
            async with gate.hold("node-1"):
                entered.set()
                await release.wait()

        first = asyncio.create_task(hold_first())
        await entered.wait()
        with pytest.raises(ConcurrentNodePollError):
            async with gate.hold("node-1"):
                raise AssertionError("concurrent same-node poll was admitted")
        async with gate.hold("node-2"):
            pass
        release.set()
        await first
        with pytest.raises(NodePollRateLimitError):
            async with gate.hold("node-1"):
                raise AssertionError("same-node poll rate limit was bypassed")
        now[0] = 1.0
        async with gate.hold("node-1"):
            pass

    asyncio.run(exercise())


def test_bootstrap_monitor_is_not_started_when_runtime_startup_fails(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = StartupOrderingBootstrap()

    async def fail_startup(_runtime: object) -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr("app.main.ApplicationRuntime.startup", fail_startup)
    app = create_app(
        settings,
        mediamtx=FakeMediaMTX(),  # type: ignore[arg-type]
        bootstrap=bootstrap,
    )

    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass
    assert bootstrap.monitor_calls == 0


def test_command_ownership_result_shape_and_wait_are_bounded(settings: Settings) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    app.state.nodes = NodeService(app.state.database)
    with TestClient(app) as client:
        service: NodeService = app.state.nodes
        first_id = create_pending(service, suffix=10)
        second_id = create_pending(service, suffix=11)
        first_token = enroll(client, service, first_id)
        second_token = enroll(client, service, second_id)
        command = service.create_command(first_id, "PING")
        delivery = client.get(
            "/node-api/v1/commands/next",
            params={"wait": 0},
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert delivery.status_code == 200
        command_id = str(command["id"])

        cross_node = client.post(
            f"/node-api/v1/commands/{command_id}/ack",
            headers={"Authorization": f"Bearer {second_token}"},
            json={},
        )
        assert cross_node.status_code == 404
        invalid_ping = client.post(
            f"/node-api/v1/commands/{command_id}/complete",
            headers={"Authorization": f"Bearer {first_token}"},
            json={
                "status": "ok",
                "completed_at": "2026-08-16T00:00:03Z",
                "agent_version": "0.1.0",
                "checks": {
                    "control_https": True,
                    "dns": True,
                    "ffmpeg": True,
                    "ffprobe": True,
                    "memory": True,
                    "disk": True,
                    "data_writable": True,
                    "no_inbound_ports": True,
                },
            },
        )
        assert invalid_ping.status_code == 422
        assert (
            client.get(
                "/node-api/v1/commands/next",
                params={"wait": 21},
                headers={"Authorization": f"Bearer {second_token}"},
            ).status_code
            == 422
        )
        empty = client.get(
            "/node-api/v1/commands/next",
            params={"wait": 0},
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert empty.status_code == 204


def test_revoke_during_bootstrap_is_rejected_without_remote_cancellation(
    settings: Settings,
    admin_password: str,
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]

    class CancelSpy:
        def __init__(self) -> None:
            self.called = False

        async def cancel_for_node(self, _: str) -> None:
            self.called = True

    with TestClient(app) as client:
        csrf = login(client, settings, admin_password)
        node = app.state.nodes.create_pending_node(
            display_name="server-active",
            address="198.51.100.80",
            resolved_ip="198.51.100.80",
            ssh_port=22,
            ssh_username="root",
            host_key_fingerprint=None,
            host_key_trust_mode=None,
        )
        spy = CancelSpy()
        app.state.bootstrap = spy

        response = client.post(
            f"/api/nodes/{node['id']}/revoke",
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "node_bootstrap_active"
    assert spy.called is False
    assert app.state.nodes.get_node(str(node["id"]))["status"] == "installing"
