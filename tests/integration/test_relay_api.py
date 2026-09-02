from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import decrypt_destination_key
from app.main import create_app
from app.relay_api import _parse_srt_result
from app.services.mediamtx import IngestState, IngestStatus
from app.services.relays import RelayCommandStateError
from app.step_up_limiter import StepUpRateLimiter


class FakeMediaMTX:
    async def get_ingest_status(self, _: str) -> IngestStatus:
        return IngestStatus(IngestState.OFFLINE)

    async def kick_publishers(self, _: str) -> int:
        return 0


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


def heartbeat_payload(*, agent_version: str = "1.0.0") -> dict[str, Any]:
    return {
        "agent_version": agent_version,
        "protocol_version": 1,
        "hostname": "hk-relay",
        "relay": safe_state(),
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


def admin_headers(client: TestClient, settings: Settings, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"login": settings.admin_login, "password": password},
    )
    assert response.status_code == 200
    return {
        "X-CSRF-Token": str(response.json()["csrf_token"]),
        "Origin": "http://testserver",
    }


def test_malformed_srt_result_fails_closed_without_parser_exception() -> None:
    with pytest.raises(RelayCommandStateError, match="no valid SRT URL"):
        _parse_srt_result("Public: srt://[invalid-host")


def test_explicit_srt_labels_override_address_classification_heuristic() -> None:
    public_url = "srt://203.0.113.10:8890?streamid=publish:live:public"
    vpn_url = "srt://8.8.8.8:8890?streamid=publish:live:vpn"

    parsed = _parse_srt_result(f"Public URL: {public_url}\nVPN: {vpn_url}")

    assert parsed == {"public_url": public_url, "vpn_url": vpn_url}


def test_relay_api_returns_bounded_live_input_bitrate(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        payload = heartbeat_payload()
        payload["relay"] = safe_state(active=True)
        payload["relay"]["source"] = "LIVE"
        payload["relay"]["input_bitrate_bps"] = 4_000_000
        response = client.post(
            "/relay-agent/v1/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {grant.node_token}"},
        )
        assert response.status_code == 200
        admin_headers(client, settings, admin_password)

        status = client.get(f"/api/nodes/{grant.node_id}/relay/status")
        assert status.status_code == 200
        assert status.json()["status"]["input_bitrate_bps"] == 4_000_000


@pytest.mark.parametrize(
    "relay_update",
    [
        {"source": "SLATE", "input_bitrate_bps": 1},
        {"source": "LIVE", "input_bitrate_bps": True},
        {"source": "LIVE", "input_bitrate_bps": 1_000_000_001},
        {"source": "LIVE", "remoteAddr": "198.51.100.4:40000"},
    ],
)
def test_relay_api_rejects_invalid_or_labeled_input_telemetry(
    relay_update: dict[str, Any], settings: Settings
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        payload = heartbeat_payload()
        payload["relay"].update(relay_update)
        response = client.post(
            "/relay-agent/v1/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {grant.node_token}"},
        )

    assert response.status_code == 422


def test_relay_api_encrypts_config_and_reports_only_terminal_success(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    key_marker = "YTKEY_CANARY_83"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        heartbeat = client.post(
            "/relay-agent/v1/heartbeat", json=heartbeat_payload(), headers=bearer
        )
        assert heartbeat.status_code == 200
        generic_protocol = client.get("/node-api/v1/commands/next?wait=0", headers=bearer)
        assert generic_protocol.status_code == 401
        headers = admin_headers(client, settings, admin_password)

        nodes = client.get("/api/nodes").json()["items"]
        assert nodes[0]["id"] == grant.node_id
        assert "moblin_relay" in nodes[0]["capabilities"]
        relay_status = client.get(f"/api/nodes/{grant.node_id}/relay")
        assert relay_status.status_code == 200
        assert relay_status.json()["status"]["input_bitrate_bps"] is None
        generic_ping = client.post(
            f"/api/nodes/{grant.node_id}/ping",
            headers=headers,
        )
        assert generic_ping.status_code == 409
        assert generic_ping.json()["error"]["code"] == "node_unavailable"

        no_origin = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": key_marker,
                "admin_password": admin_password,
            },
            headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
        )
        assert no_origin.status_code == 403

        queued = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": " rtmps://a.rtmps.youtube.com/live2\n",
                "stream_key": f" {key_marker}\n",
                "admin_password": admin_password,
            },
            headers={**headers, "Idempotency-Key": "ui:configure:001"},
        )
        assert queued.status_code == 202
        assert key_marker not in queued.text
        command_id = queued.json()["command_id"]
        with settings.database_path.open("rb") as raw_database:
            assert key_marker.encode() not in raw_database.read()

        mismatch_marker = "YTKEY_IDEMPOTENCY_MISMATCH_89"
        mismatched_replay = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": mismatch_marker,
                "admin_password": admin_password,
            },
            headers={**headers, "Idempotency-Key": "ui:configure:001"},
        )
        assert mismatched_replay.status_code == 409
        assert mismatched_replay.json()["error"]["code"] == "idempotency_key_conflict"
        assert mismatch_marker not in mismatched_replay.text
        assert mismatch_marker not in str(app.state.database.list_audit_events())

        conflicting_mutation = client.request(
            "DELETE",
            f"/api/nodes/{grant.node_id}/relay/youtube",
            json={"admin_password": admin_password},
            headers=headers,
        )
        assert conflicting_mutation.status_code == 409
        assert conflicting_mutation.json()["error"]["code"] == "relay_command_pending"

        lease = client.get("/relay-agent/v1/commands/next?wait=0", headers=bearer)
        assert lease.status_code == 200
        assert lease.json()["payload"]["youtube_stream_key"] == key_marker
        assert (
            client.post(
                f"/relay-agent/v1/commands/{command_id}/ack", json={}, headers=bearer
            ).status_code
            == 200
        )
        completed = client.post(
            f"/relay-agent/v1/commands/{command_id}/complete",
            json={
                "status": "ok",
                "completed_at": datetime.now(UTC).isoformat(),
                "safe_result": safe_state(),
                "secret_result": None,
            },
            headers=bearer,
        )
        assert completed.status_code == 200
        poll = client.get(f"/api/nodes/{grant.node_id}/relay/commands/{command_id}")
        assert poll.json()["state"] == "completed"
        assert poll.json()["completion_status"] == "ok"
        assert key_marker not in poll.text
        assert key_marker not in str(app.state.database.list_audit_events())

        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE relay_nodes SET service_state = 'active' WHERE node_id = ?",
                (grant.node_id,),
            )
        conflict_marker = "YTKEY_CONFLICT_84"
        conflict = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": conflict_marker,
                "admin_password": admin_password,
            },
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "relay_active"
        assert conflict_marker not in conflict.text

        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE relay_nodes SET service_state = 'inactive', main_process = 'running' "
                "WHERE node_id = ?",
                (grant.node_id,),
            )
        inconsistent_marker = "YTKEY_INCONSISTENT_RUNNING_94"
        inconsistent = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": inconsistent_marker,
                "admin_password": admin_password,
            },
            headers=headers,
        )
        assert inconsistent.status_code == 409
        assert inconsistent.json()["error"]["code"] == "relay_active"
        assert inconsistent_marker not in inconsistent.text
        assert inconsistent_marker not in str(app.state.database.list_audit_events())


def test_key_only_api_is_capability_gated_for_old_agents(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    marker = "KEY_ONLY_OLD_AGENT_SECRET_6c2e"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat",
                json=heartbeat_payload(agent_version="1.1.0"),
                headers=bearer,
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)

        response = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube-key",
            json={"stream_key": marker, "admin_password": admin_password},
            headers=headers,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "unsupported_protocol"
        assert marker not in response.text
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM relay_commands").fetchone()[0] == 0


def test_key_only_api_is_strict_secret_safe_and_idempotent(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    marker = "KEY_ONLY_API_SECRET_FIRST_2a7d"
    mismatch = "KEY_ONLY_API_SECRET_SECOND_4b8e"
    malformed = "KEY_ONLY_API_SECRET_INVALID_9f1c!"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat",
                json=heartbeat_payload(agent_version="1.2.0"),
                headers=bearer,
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)
        endpoint = f"/api/nodes/{grant.node_id}/relay/configure-youtube-key"
        idempotent_headers = {**headers, "Idempotency-Key": "ui:key-only:0001"}

        malformed_response = client.put(
            endpoint,
            json={"stream_key": malformed, "admin_password": admin_password},
            headers=headers,
        )
        assert malformed_response.status_code == 422
        assert malformed not in malformed_response.text
        expanded_response = client.put(
            endpoint,
            json={
                "stream_key": marker,
                "admin_password": admin_password,
                "url": "rtmps://b.rtmps.youtube.com/live2",
            },
            headers=headers,
        )
        assert expanded_response.status_code == 422
        assert marker not in expanded_response.text

        first = client.put(
            endpoint,
            json={"stream_key": f" {marker}\n", "admin_password": admin_password},
            headers=idempotent_headers,
        )
        replay = client.put(
            endpoint,
            json={"stream_key": marker, "admin_password": admin_password},
            headers=idempotent_headers,
        )
        assert first.status_code == replay.status_code == 202
        assert replay.json()["command_id"] == first.json()["command_id"]
        assert marker not in first.text
        assert marker not in replay.text

        conflict = client.put(
            endpoint,
            json={"stream_key": mismatch, "admin_password": admin_password},
            headers=idempotent_headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"
        assert mismatch not in conflict.text
        assert mismatch not in str(app.state.database.list_audit_events())

        lease = client.get(
            "/relay-agent/v1/commands/next?wait=0",
            headers={**bearer, "X-Relay-Agent-Version": "1.2.0"},
        )
        assert lease.status_code == 200
        assert lease.json()["action"] == "CONFIGURE_YOUTUBE_KEY"
        assert lease.json()["payload"] == {"youtube_stream_key": marker}
        with settings.database_path.open("rb") as raw_database:
            database_bytes = raw_database.read()
        assert marker.encode() not in database_bytes
        assert mismatch.encode() not in database_bytes


def test_old_poll_terminalizes_queued_key_only_secret_before_downgrade_heartbeat(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    marker = "KEY_ONLY_API_DOWNGRADE_RACE_SECRET_8e5a"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat",
                json=heartbeat_payload(agent_version="1.2.0"),
                headers=bearer,
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)
        queued = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube-key",
            json={"stream_key": marker, "admin_password": admin_password},
            headers={**headers, "Idempotency-Key": "ui:key-only:downgrade-race:0001"},
        )
        assert queued.status_code == 202

        # The pre-assertion client has not yet sent its 1.1 downgrade
        # heartbeat, so the stored node capability is intentionally stale.
        old_poll = client.get("/relay-agent/v1/commands/next?wait=0", headers=bearer)
        assert old_poll.status_code == 204

        terminal = client.get(
            f"/api/nodes/{grant.node_id}/relay/commands/{queued.json()['command_id']}"
        )
        assert terminal.status_code == 200
        assert terminal.json()["state"] == "failed"
        assert terminal.json()["completion_status"] == "failed"
        assert terminal.json()["safe_result"] == {"error_code": "unsupported_command"}
        assert marker not in terminal.text

        with app.state.database.connect() as connection:
            stored = connection.execute(
                "SELECT payload_encrypted FROM relay_commands WHERE id = ?",
                (queued.json()["command_id"],),
            ).fetchone()
        assert stored is not None
        assert (
            decrypt_destination_key(
                stored["payload_encrypted"],
                app.state.relays.master_encryption_key,
            )
            == "{}"
        )
        with settings.database_path.open("rb") as raw_database:
            assert marker.encode() not in raw_database.read()

        following = client.request(
            "DELETE",
            f"/api/nodes/{grant.node_id}/relay/youtube",
            json={"admin_password": admin_password},
            headers={**headers, "Idempotency-Key": "ui:key-only:downgrade-race:0002"},
        )
        assert following.status_code == 202


@pytest.mark.parametrize("failure", ["active", "missing_url"])
def test_key_only_api_refuses_unsafe_or_unconfigured_state(
    failure: str, settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    marker = f"KEY_ONLY_API_{failure.upper()}_3d6a"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        payload = heartbeat_payload(agent_version="1.2.0")
        if failure == "active":
            payload["relay"] = safe_state(active=True)
        else:
            payload["relay"]["youtube_url_configured"] = False
            payload["relay"]["youtube_key_configured"] = False
            payload["relay"]["error_code"] = "youtube_not_configured"
        assert (
            client.post("/relay-agent/v1/heartbeat", json=payload, headers=bearer).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)

        response = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube-key",
            json={"stream_key": marker, "admin_password": admin_password},
            headers=headers,
        )

        assert response.status_code == 409
        expected = "relay_active" if failure == "active" else "youtube_not_configured"
        assert response.json()["error"]["code"] == expected
        assert marker not in response.text
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM relay_commands").fetchone()[0] == 0


def test_reveal_result_is_returned_once_and_removed_from_sqlite(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    srt_marker = "SRT_SECRET_CANARY_85"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=heartbeat_payload(), headers=bearer
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)
        reveal_headers = {**headers, "Idempotency-Key": "ui:reveal:001"}
        first = client.post(
            f"/api/nodes/{grant.node_id}/relay/reveal-moblin-url?wait=0",
            json={"admin_password": admin_password},
            headers=reveal_headers,
        )
        assert first.status_code == 202
        command_id = first.json()["command_id"]
        lease = client.get("/relay-agent/v1/commands/next?wait=0", headers=bearer)
        assert lease.json()["action"] == "REVEAL_MOBLIN_URL"
        assert (
            client.post(
                f"/relay-agent/v1/commands/{command_id}/ack", json={}, headers=bearer
            ).status_code
            == 200
        )
        secret = (
            "Public: srt://relay.example:8890?streamid=publish:live:"
            f"{srt_marker}&passphrase=opaque\n"
            "VPN: srt://172.29.0.1:8890?streamid=publish:live:vpn"
        )
        assert (
            client.post(
                f"/relay-agent/v1/commands/{command_id}/complete",
                json={
                    "status": "ok",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "safe_result": safe_state(),
                    "secret_result": secret,
                },
                headers=bearer,
            ).status_code
            == 200
        )
        with settings.database_path.open("rb") as raw_database:
            assert srt_marker.encode() not in raw_database.read()

        revealed = client.post(
            f"/api/nodes/{grant.node_id}/relay/reveal-moblin-url?wait=0",
            json={"admin_password": admin_password},
            headers=reveal_headers,
        )
        assert revealed.status_code == 200
        assert srt_marker in str(revealed.json()["public_url"])
        assert revealed.headers["cache-control"] == "no-store"
        with app.state.database.connect() as connection:
            row = connection.execute(
                """
                SELECT secret_result_encrypted, secret_consumed_at
                FROM relay_commands WHERE id = ?
                """,
                (command_id,),
            ).fetchone()
        assert row["secret_result_encrypted"] is None
        assert row["secret_consumed_at"] is not None
        assert srt_marker not in str(app.state.database.list_audit_events())
        with app.state.database.connect() as connection:
            command_count = connection.execute(
                "SELECT COUNT(*) FROM relay_commands WHERE node_id = ? "
                "AND command_type = 'REVEAL_MOBLIN_URL'",
                (grant.node_id,),
            ).fetchone()[0]
        assert command_count == 1


def test_relay_step_up_rate_limit_is_independent_and_never_audits_secrets(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    app.state.relay_step_up_limiter = StepUpRateLimiter(attempts=2, window_seconds=60)
    key_marker = "STEPUP_KEY_CANARY_86"
    password_markers = ("WRONG_PASSWORD_CANARY_87", "WRONG_PASSWORD_CANARY_88")
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=heartbeat_payload(), headers=bearer
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)
        endpoint = f"/api/nodes/{grant.node_id}/relay/configure-youtube"

        for password_marker in password_markers:
            rejected = client.put(
                endpoint,
                json={
                    "url": "rtmps://a.rtmps.youtube.com/live2",
                    "stream_key": key_marker,
                    "admin_password": password_marker,
                },
                headers=headers,
            )
            assert rejected.status_code == 401
            assert password_marker not in rejected.text
            assert key_marker not in rejected.text

        locked = client.put(
            endpoint,
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": key_marker,
                "admin_password": admin_password,
            },
            headers=headers,
        )
        assert locked.status_code == 429
        assert locked.headers["retry-after"] == "60"
        assert key_marker not in locked.text

        audit = str(app.state.database.list_audit_events())
        assert key_marker not in audit
        assert all(marker not in audit for marker in password_markers)

        # The dedicated relay throttle never consumes or resets the login budget.
        relogin = client.post(
            "/api/auth/login",
            json={"login": settings.admin_login, "password": admin_password},
        )
        assert relogin.status_code == 200
        fresh_headers = {
            "X-CSRF-Token": str(relogin.json()["csrf_token"]),
            "Origin": "http://testserver",
        }
        accepted = client.put(
            endpoint,
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": key_marker,
                "admin_password": admin_password,
            },
            headers=fresh_headers,
        )
        assert accepted.status_code == 202
        assert key_marker not in accepted.text
        final_audit = str(app.state.database.list_audit_events())
        assert key_marker not in final_audit
        assert all(marker not in final_audit for marker in password_markers)
        with app.state.database.connect() as connection:
            database_dump = "\n".join(connection.iterdump())
        assert key_marker not in database_dump
        assert all(marker not in database_dump for marker in password_markers)


def test_admin_revoke_cancels_and_erases_pending_relay_command(
    settings: Settings, admin_password: str
) -> None:
    app = create_app(settings, mediamtx=FakeMediaMTX())  # type: ignore[arg-type]
    marker = "REVOKE_API_STREAM_KEY_CANARY_93"
    with TestClient(app) as client:
        grant = app.state.relays.provision_node(display_name="HK relay", address="relay.example")
        bearer = {"Authorization": f"Bearer {grant.node_token}"}
        assert (
            client.post(
                "/relay-agent/v1/heartbeat", json=heartbeat_payload(), headers=bearer
            ).status_code
            == 200
        )
        headers = admin_headers(client, settings, admin_password)
        queued = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": marker,
                "admin_password": admin_password,
            },
            headers={**headers, "Idempotency-Key": "test:revoke:configure:001"},
        )
        assert queued.status_code == 202
        command_id = queued.json()["command_id"]

        revoked = client.post(
            f"/api/nodes/{grant.node_id}/revoke",
            headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
        )
        assert revoked.status_code == 200
        assert marker not in revoked.text
        with app.state.database.connect() as connection:
            row = connection.execute(
                """
                SELECT state, payload_encrypted FROM relay_commands WHERE id = ?
                """,
                (command_id,),
            ).fetchone()
        assert row is not None and row["state"] == "cancelled"
        assert (
            decrypt_destination_key(row["payload_encrypted"], settings.master_encryption_key)
            == "{}"
        )
        replay = client.put(
            f"/api/nodes/{grant.node_id}/relay/configure-youtube",
            json={
                "url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": marker,
                "admin_password": admin_password,
            },
            headers={**headers, "Idempotency-Key": "test:revoke:configure:001"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "relay_revoked"
        assert marker not in replay.text
        assert marker not in str(app.state.database.list_audit_events())
