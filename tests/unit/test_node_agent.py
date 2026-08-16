from __future__ import annotations

import inspect
import json
import logging
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

import httpx
import pytest

from app.schemas import NodeCommandCompleteRequest, NodeEnrollmentRequest, NodeHeartbeatRequest
from node_agent.__main__ import _run_agent
from node_agent.client import NodeAPIClient
from node_agent.commands import CommandJournal, CommandProcessor, LocalSelfTestProbe
from node_agent.credentials import CredentialStore, SensitiveToken
from node_agent.errors import (
    AgentError,
    ConfigurationError,
    ControlAPIError,
    CredentialError,
    CredentialsRejected,
    EnrollmentRejected,
    ProtocolError,
    ProtocolRejected,
)
from node_agent.metrics import LinuxMetricsProbe, MetricsCollector
from node_agent.models import (
    SELF_TEST_CHECKS,
    CommandCompletion,
    EnrollmentResponse,
    NodeCommand,
    NodeSnapshot,
)
from node_agent.service import AgentService, ExponentialBackoff
from node_agent.settings import HEARTBEAT_INTERVAL_SECONDS, PROTOCOL_VERSION, AgentSettings

ENROLLMENT_TOKEN = "e" * 64
NODE_TOKEN = "n" * 64
NODE_ID = "123e4567-e89b-42d3-a456-426614174001"
COMMAND_ID = "123e4567-e89b-42d3-a456-426614174000"


def snapshot(
    *, ffmpeg_version: str | None = "ffmpeg version 7", cpu_percent: float = 12.5
) -> NodeSnapshot:
    return NodeSnapshot(
        hostname="node-01",
        os_name="Linux",
        os_version="6.8.0",
        architecture="x86_64",
        cpu_count=2,
        uptime_seconds=1234.5,
        load_1m=0.25,
        cpu_percent=cpu_percent,
        memory_total_bytes=2 * 1024**3,
        memory_available_bytes=1024**3,
        disk_total_bytes=40 * 1024**3,
        disk_free_bytes=30 * 1024**3,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version="ffprobe version 7",
    )


def command_payload(command_id: str = COMMAND_ID, command_type: str = "PING") -> dict[str, object]:
    return {
        "id": command_id,
        "command_type": command_type,
        "payload": {},
        "lease_seconds": 30,
        "attempt_count": 1,
    }


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)


class FakeMetricsProbe:
    def hostname(self) -> str:
        return "node-01"

    def os_identity(self) -> tuple[str, str]:
        return "Linux", "6.8.0"

    def architecture(self) -> str:
        return "x86_64"

    def cpu_count(self) -> int:
        return 2

    def uptime_seconds(self) -> float:
        return 123.0

    def load_1m(self) -> float:
        return 0.1

    def cpu_percent(self) -> float:
        return 10.0

    def memory_bytes(self) -> tuple[int, int]:
        return 2048, 1024

    def disk_bytes(self, _path: Path) -> tuple[int, int]:
        return 4096, 3072

    def tool_version(self, binary: str) -> str | None:
        return f"{binary} version 7"


class PassingSelfTestProbe:
    def control_https(self) -> bool:
        return True

    def dns(self) -> bool:
        return True

    def data_writable(self) -> bool:
        return True

    def no_inbound_ports(self) -> bool:
        return True


def test_settings_require_https_and_keep_credentials_out_of_repr(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="HTTPS origin"):
        AgentSettings(control_url="http://control.test", data_dir=tmp_path)
    with pytest.raises(ConfigurationError, match="HTTPS origin"):
        AgentSettings(control_url="https://user:pass@control.test", data_dir=tmp_path)

    settings = AgentSettings(control_url="https://control.test", data_dir=tmp_path)

    assert NODE_TOKEN not in repr(settings)
    assert settings.command_wait_seconds == 20


def test_test_environment_allows_insecure_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NODE_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("NODE_CONTROL_URL", "http://backend:8000")
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))

    settings = AgentSettings.from_env()

    assert settings.allow_insecure_http is True
    assert NODE_TOKEN not in repr(settings)
    assert "NODE_TOKEN" not in inspect.getsource(AgentSettings.from_env)


def test_development_environment_allows_insecure_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NODE_AGENT_ENVIRONMENT", "development")
    monkeypatch.setenv("NODE_CONTROL_URL", "http://control.local:8000")
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))

    settings = AgentSettings.from_env()

    assert settings.allow_insecure_http is True
    assert settings.control_url == "http://control.local:8000"


def test_host_identity_overrides_are_validated_and_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NODE_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("NODE_CONTROL_URL", "http://backend:8000")
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODE_HOSTNAME", "edge-node-01")
    monkeypatch.setenv("NODE_OS_NAME", "ubuntu")
    monkeypatch.setenv("NODE_OS_VERSION", "24.04")
    monkeypatch.setenv("NODE_ARCHITECTURE", "x86_64")

    settings = AgentSettings.from_env()
    collected = MetricsCollector(
        tmp_path,
        FakeMetricsProbe(),
        host_hostname=settings.host_hostname,
        host_os_name=settings.host_os_name,
        host_os_version=settings.host_os_version,
        host_architecture=settings.host_architecture,
    ).collect()

    assert (
        collected.hostname,
        collected.os_name,
        collected.os_version,
        collected.architecture,
    ) == (
        "edge-node-01",
        "ubuntu",
        "24.04",
        "x86_64",
    )

    with pytest.raises(ConfigurationError, match="NODE_ARCHITECTURE"):
        AgentSettings(
            control_url="https://control.test",
            data_dir=tmp_path,
            host_architecture="x86_64; unsafe",
        )
    with pytest.raises(ConfigurationError, match="NODE_HOSTNAME"):
        AgentSettings(
            control_url="https://control.test",
            data_dir=tmp_path,
            host_hostname="edge-node\nunsafe",
        )


def test_control_https_self_test_makes_bounded_credential_free_probe(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def reachable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"code": "node_authentication_failed"}})

    probe = LocalSelfTestProbe(
        "https://control.test",
        tmp_path,
        transport=httpx.MockTransport(reachable),
    )
    assert probe.control_https() is True
    assert len(requests) == 1
    assert requests[0].url.path == "/node-api/v1/commands/next"
    assert requests[0].url.params["wait"] == "0"
    assert "authorization" not in requests[0].headers
    assert requests[0].content == b""
    test_probe = LocalSelfTestProbe(
        "http://control.test",
        tmp_path,
        allow_insecure_http=True,
        transport=httpx.MockTransport(reachable),
    )
    assert test_probe.control_https() is True


def test_control_https_self_test_fails_closed_on_transport_or_insecure_scheme(
    tmp_path: Path,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    transport = httpx.MockTransport(unavailable)
    assert (
        LocalSelfTestProbe(
            "https://control.test",
            tmp_path,
            transport=transport,
        ).control_https()
        is False
    )
    assert (
        LocalSelfTestProbe(
            "http://control.test",
            tmp_path,
            transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": {}})),
        ).control_https()
        is False
    )


def test_sensitive_token_never_renders_its_value() -> None:
    token = SensitiveToken.parse(NODE_TOKEN)

    assert NODE_TOKEN not in repr(token)
    assert NODE_TOKEN not in str(token)
    assert token.reveal() == NODE_TOKEN


def test_enrollment_promotion_is_private_atomic_and_one_time(tmp_path: Path) -> None:
    enrollment_path = tmp_path / "enrollment.token"
    node_path = tmp_path / "node.token"
    write_private(enrollment_path, ENROLLMENT_TOKEN)
    store = CredentialStore(enrollment_path, node_path)

    assert store.load_permanent() is None
    assert store.load_enrollment().reveal() == ENROLLMENT_TOKEN
    store.promote(SensitiveToken.parse(NODE_TOKEN))

    assert node_path.read_text(encoding="ascii").strip() == NODE_TOKEN
    if os.name == "posix":
        assert node_path.stat().st_mode & 0o077 == 0
    assert not enrollment_path.exists()
    assert list(tmp_path.glob(".node.token.*.tmp")) == []


def test_enrollment_is_not_deleted_when_permanent_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enrollment_path = tmp_path / "enrollment.token"
    node_path = tmp_path / "node.token"
    write_private(enrollment_path, ENROLLMENT_TOKEN)
    store = CredentialStore(enrollment_path, node_path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(CredentialError, match="could not be stored"):
        store.promote(SensitiveToken.parse(NODE_TOKEN))

    assert enrollment_path.exists()
    assert not node_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are enforced in the Linux image")
def test_unsafe_credential_permissions_are_rejected(tmp_path: Path) -> None:
    enrollment_path = tmp_path / "enrollment.token"
    write_private(enrollment_path, ENROLLMENT_TOKEN)
    enrollment_path.chmod(0o644)

    with pytest.raises(CredentialError, match="0600"):
        CredentialStore(enrollment_path, tmp_path / "node.token").load_enrollment()


def test_metrics_collector_builds_validated_snapshot(tmp_path: Path) -> None:
    collected = MetricsCollector(tmp_path, FakeMetricsProbe()).collect()

    assert collected.cpu_percent == 10.0
    assert collected.memory_available_bytes <= collected.memory_total_bytes
    assert collected.ffmpeg_version == "ffmpeg version 7"


def test_media_tool_probe_uses_only_fixed_binary_and_caches_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "node_agent.metrics.shutil.which", lambda binary, **_kwargs: f"/usr/bin/{binary}"
    )

    def run_fixed(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"ffmpeg version 7\n")

    monkeypatch.setattr("node_agent.metrics.subprocess.run", run_fixed)
    probe = LinuxMetricsProbe()

    assert probe.tool_version("ffmpeg") == "ffmpeg version 7"
    assert probe.tool_version("ffmpeg") == "ffmpeg version 7"
    assert calls == [["/usr/bin/ffmpeg", "-version"]]
    with pytest.raises(ValueError, match="fixed media tools"):
        probe.tool_version("sh")


@pytest.mark.parametrize("invalid_cpu", [-0.1, 100.1, float("nan"), float("inf")])
def test_metrics_reject_invalid_cpu_values(invalid_cpu: float) -> None:
    with pytest.raises(ProtocolError, match="CPU percent"):
        snapshot(cpu_percent=invalid_cpu)


def test_enrollment_and_heartbeat_payloads_match_protocol_v1() -> None:
    collected = snapshot(ffmpeg_version=None)
    enrollment = collected.enrollment_payload(
        enrollment_token=SensitiveToken.parse(ENROLLMENT_TOKEN), agent_version="0.1.0"
    )
    heartbeat = collected.heartbeat_payload(
        agent_version="0.1.0",
        current_command_id=None,
        control_latency_ms=None,
    )

    assert enrollment["protocol_version"] == PROTOCOL_VERSION
    assert enrollment["enrollment_token"] == ENROLLMENT_TOKEN
    assert "uptime_seconds" not in enrollment
    assert heartbeat["protocol_version"] == PROTOCOL_VERSION
    assert heartbeat["ffmpeg_version"] is None
    assert heartbeat["control_latency_ms"] is None
    assert "enrollment_token" not in heartbeat
    with pytest.raises(ProtocolError, match="control latency"):
        collected.heartbeat_payload(
            agent_version="0.1.0",
            current_command_id=None,
            control_latency_ms=60_000.1,
        )


def test_agent_payloads_validate_against_control_plane_v1_models(tmp_path: Path) -> None:
    collected = snapshot()
    enrollment = collected.enrollment_payload(
        enrollment_token=SensitiveToken.parse(ENROLLMENT_TOKEN), agent_version="0.1.0"
    )
    heartbeat = collected.heartbeat_payload(
        agent_version="0.1.0",
        current_command_id=COMMAND_ID,
        control_latency_ms=12.5,
    )
    command = NodeCommand.parse(command_payload(command_type="SELF_TEST"))
    completion = CommandProcessor(
        agent_version="0.1.0",
        journal=CommandJournal(tmp_path / "commands.json"),
        snapshot_supplier=snapshot,
        self_test_probe=PassingSelfTestProbe(),
    ).process(command)

    assert NodeEnrollmentRequest.model_validate(enrollment).protocol_version == 1
    assert NodeHeartbeatRequest.model_validate(heartbeat).current_command_id is not None
    validated_completion = NodeCommandCompleteRequest.model_validate(completion.to_payload())
    assert validated_completion.received_at is None
    assert validated_completion.checks is not None


def test_http_client_uses_fixed_user_agent_and_bearer_only_after_enrollment(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    heartbeat_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/enroll"):
            body = json.loads(request.content)
            assert body["enrollment_token"] == ENROLLMENT_TOKEN
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "node_id": NODE_ID,
                    "node_token": NODE_TOKEN,
                    "heartbeat_interval_seconds": 5,
                    "command_poll_interval_seconds": 5,
                },
            )
        if request.url.path.endswith("/heartbeat"):
            assert request.headers["authorization"] == f"Bearer {NODE_TOKEN}"
            assert NODE_TOKEN not in request.content.decode("utf-8")
            heartbeat_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "node_status": "ready"})
        return httpx.Response(204)

    settings = AgentSettings(control_url="https://control.test", data_dir=tmp_path)
    clock_values = iter((10.0, 10.0125, 20.0, 20.02))
    client = NodeAPIClient(
        settings,
        agent_version="0.1.0",
        transport=httpx.MockTransport(handler),
        clock=lambda: next(clock_values),
    )
    response = client.enroll(SensitiveToken.parse(ENROLLMENT_TOKEN), snapshot())
    client.heartbeat(response.node_token, snapshot(), current_command_id=None)
    client.heartbeat(response.node_token, snapshot(), current_command_id=None)
    assert client.next_command(response.node_token, wait_seconds=20) is None
    client.close()

    assert all(
        request.headers["user-agent"] == "AdoJapan-Restream-Node/0.1.0" for request in requests
    )
    assert all(NODE_TOKEN not in request.headers["user-agent"] for request in requests)
    assert requests[-1].url.params["wait"] == "20"
    assert heartbeat_bodies[0]["control_latency_ms"] is None
    assert heartbeat_bodies[1]["control_latency_ms"] == pytest.approx(12.5)
    assert client.control_latency_ms() == pytest.approx(20.0)


def test_http_client_acknowledges_and_completes_fixed_command(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json=command_payload())
        if request.url.path.endswith("/ack"):
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"status": "acknowledged"})
        assert json.loads(request.content)["status"] == "ok"
        return httpx.Response(200, json={"status": "completed"})

    client = NodeAPIClient(
        AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        agent_version="0.1.0",
        transport=httpx.MockTransport(handler),
    )
    token = SensitiveToken.parse(NODE_TOKEN)
    command = client.next_command(token, wait_seconds=20)
    assert command is not None
    client.ack_command(token, command.command_id)
    completion = CommandCompletion(
        status="ok",
        received_at=command.received_at,
        completed_at=command.received_at,
        agent_version="0.1.0",
    )
    client.complete_command(token, command.command_id, completion)
    client.close()

    assert paths == [
        "/node-api/v1/commands/next",
        f"/node-api/v1/commands/{COMMAND_ID}/ack",
        f"/node-api/v1/commands/{COMMAND_ID}/complete",
    ]


def test_http_failures_do_not_expose_bearer_token(tmp_path: Path) -> None:
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": NODE_TOKEN})

    client = NodeAPIClient(
        AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        agent_version="0.1.0",
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(CredentialsRejected) as captured:
        client.heartbeat(SensitiveToken.parse(NODE_TOKEN), snapshot(), current_command_id=None)
    client.close()

    assert NODE_TOKEN not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_http_protocol_rejection_is_permanent_and_secret_safe(tmp_path: Path) -> None:
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "unsupported_protocol",
                    "message": NODE_TOKEN,
                }
            },
        )

    client = NodeAPIClient(
        AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        agent_version="0.1.0",
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(ProtocolRejected) as captured:
        client.heartbeat(SensitiveToken.parse(NODE_TOKEN), snapshot(), current_command_id=None)
    client.close()

    assert captured.value.code == "unsupported_protocol"
    assert NODE_TOKEN not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_http_enrollment_rejection_is_permanent_and_secret_safe(tmp_path: Path) -> None:
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "enrollment_failed", "message": ENROLLMENT_TOKEN}},
        )

    client = NodeAPIClient(
        AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        agent_version="0.1.0",
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(EnrollmentRejected) as captured:
        client.enroll(SensitiveToken.parse(ENROLLMENT_TOKEN), snapshot())
    client.close()

    assert captured.value.code == "enrollment_rejected"
    assert ENROLLMENT_TOKEN not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_transport_exception_drops_secret_bearing_request_from_exception_chain(
    tmp_path: Path,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    client = NodeAPIClient(
        AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        agent_version="0.1.0",
        transport=httpx.MockTransport(fail),
    )
    with pytest.raises(ControlAPIError) as captured:
        client.heartbeat(SensitiveToken.parse(NODE_TOKEN), snapshot(), current_command_id=None)
    client.close()

    assert captured.value.__cause__ is None
    assert NODE_TOKEN not in repr(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        command_payload(command_type="SHELL"),
        {**command_payload(), "payload": {"command": "id"}},
        {**command_payload(), "id": "../unsafe"},
        {**command_payload(), "lease_seconds": 301},
    ],
)
def test_command_parser_rejects_arbitrary_execution_surfaces(payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        NodeCommand.parse(payload)


@pytest.mark.parametrize(
    ("status", "failed_check"),
    [("ok", "dns"), ("failed", None)],
)
def test_self_test_completion_status_must_match_all_checks(
    status: Literal["ok", "failed"],
    failed_check: str | None,
) -> None:
    checks = {name: True for name in SELF_TEST_CHECKS}
    if failed_check is not None:
        checks[failed_check] = False

    with pytest.raises(ProtocolError, match="does not match"):
        CommandCompletion(
            status=status,
            received_at=None,
            completed_at="2026-08-16T00:00:01+00:00",
            agent_version="0.1.0",
            checks=checks,
        )


def test_command_processor_is_idempotent_across_journal_reload(tmp_path: Path) -> None:
    supplier_calls = 0

    def supply_snapshot() -> NodeSnapshot:
        nonlocal supplier_calls
        supplier_calls += 1
        return snapshot()

    journal_path = tmp_path / "commands.json"
    command = NodeCommand.parse(command_payload(command_type="SELF_TEST"))
    first_processor = CommandProcessor(
        agent_version="0.1.0",
        journal=CommandJournal(journal_path),
        snapshot_supplier=supply_snapshot,
        self_test_probe=PassingSelfTestProbe(),
    )
    first = first_processor.process(command)
    second_processor = CommandProcessor(
        agent_version="0.1.0",
        journal=CommandJournal(journal_path),
        snapshot_supplier=supply_snapshot,
        self_test_probe=PassingSelfTestProbe(),
    )
    second = second_processor.process(command)

    assert first == second
    assert supplier_calls == 1
    assert first.status == "ok"
    assert first.checks is not None
    assert tuple(first.checks) == SELF_TEST_CHECKS
    if os.name == "posix":
        assert journal_path.stat().st_mode & 0o077 == 0


def test_command_id_reuse_with_another_type_fails_closed(tmp_path: Path) -> None:
    processor = CommandProcessor(
        agent_version="0.1.0",
        journal=CommandJournal(tmp_path / "commands.json"),
        snapshot_supplier=snapshot,
        self_test_probe=PassingSelfTestProbe(),
    )
    processor.process(NodeCommand.parse(command_payload(command_type="PING")))

    with pytest.raises(ProtocolError, match="reused"):
        processor.process(NodeCommand.parse(command_payload(command_type="SELF_TEST")))


def test_backoff_is_bounded_and_resettable() -> None:
    backoff = ExponentialBackoff(
        1.0,
        4.0,
        random_source=random.Random(1),  # noqa: S311 - deterministic test source
    )

    delays = [backoff.next_delay() for _ in range(8)]
    backoff.reset()

    assert all(0 <= delay <= 4.0 for delay in delays)
    assert backoff.next_delay() <= 1.0


class FakeControlClient:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.heartbeat_seen = threading.Event()
        self.enrollment_calls = 0
        self.command_delivered = False
        self.acked: list[str] = []
        self.completed: list[str] = []

    def enroll(
        self, enrollment_token: SensitiveToken, _snapshot: NodeSnapshot
    ) -> EnrollmentResponse:
        assert enrollment_token.reveal() == ENROLLMENT_TOKEN
        self.enrollment_calls += 1
        return EnrollmentResponse(
            node_id=NODE_ID,
            node_token=SensitiveToken.parse(NODE_TOKEN),
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            command_poll_interval_seconds=5,
        )

    def heartbeat(
        self,
        node_token: SensitiveToken,
        _snapshot: NodeSnapshot,
        *,
        current_command_id: str | None,
    ) -> None:
        assert node_token.reveal() == NODE_TOKEN
        assert current_command_id is None
        self.heartbeat_seen.set()

    def next_command(self, _node_token: SensitiveToken, *, wait_seconds: int) -> NodeCommand | None:
        assert wait_seconds == 20
        assert self.heartbeat_seen.wait(2)
        if not self.command_delivered:
            self.command_delivered = True
            return NodeCommand.parse(command_payload())
        self.stop_event.set()
        return None

    def ack_command(self, _node_token: SensitiveToken, command_id: str) -> None:
        self.acked.append(command_id)

    def complete_command(
        self,
        _node_token: SensitiveToken,
        command_id: str,
        _completion: CommandCompletion,
    ) -> None:
        self.completed.append(command_id)


def test_service_enrolls_then_runs_heartbeat_ack_and_complete(tmp_path: Path) -> None:
    stop_event = threading.Event()
    enrollment_path = tmp_path / "enrollment.token"
    write_private(enrollment_path, ENROLLMENT_TOKEN)
    credentials = CredentialStore(enrollment_path, tmp_path / "node.token")
    metrics = MetricsCollector(tmp_path, FakeMetricsProbe())
    client = FakeControlClient(stop_event)
    commands = CommandProcessor(
        agent_version="0.1.0",
        journal=CommandJournal(tmp_path / "commands.json"),
        snapshot_supplier=metrics.collect,
        self_test_probe=PassingSelfTestProbe(),
    )
    service = AgentService(
        settings=AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        client=client,
        credentials=credentials,
        metrics=metrics,
        commands=commands,
        stop_event=stop_event,
    )

    service.run()

    assert client.enrollment_calls == 1
    assert client.heartbeat_seen.is_set()
    assert client.acked == [COMMAND_ID]
    assert client.completed == [COMMAND_ID]
    assert not enrollment_path.exists()
    assert credentials.load_permanent() is not None


class PermanentlyRejectedControlClient:
    def __init__(self, rejection: AgentError) -> None:
        self.rejection = rejection
        self.rejection_seen = threading.Event()
        self.heartbeat_calls = 0
        self.command_calls = 0

    def enroll(
        self, _enrollment_token: SensitiveToken, _snapshot: NodeSnapshot
    ) -> EnrollmentResponse:
        raise AssertionError("permanent credential must be reused")

    def heartbeat(
        self,
        _node_token: SensitiveToken,
        _snapshot: NodeSnapshot,
        *,
        current_command_id: str | None,
    ) -> None:
        assert current_command_id is None
        self.heartbeat_calls += 1
        self.rejection_seen.set()
        raise self.rejection

    def next_command(self, _node_token: SensitiveToken, *, wait_seconds: int) -> NodeCommand | None:
        assert wait_seconds == 20
        self.command_calls += 1
        assert self.rejection_seen.wait(2)
        return None

    def ack_command(self, _node_token: SensitiveToken, _command_id: str) -> None:
        raise AssertionError("no command may be acknowledged after permanent rejection")

    def complete_command(
        self,
        _node_token: SensitiveToken,
        _command_id: str,
        _completion: CommandCompletion,
    ) -> None:
        raise AssertionError("no command may be completed after permanent rejection")


@pytest.mark.parametrize(
    "rejection",
    [
        CredentialsRejected("node_credential_rejected", "credential rejected"),
        ProtocolRejected("unsupported_protocol", "protocol rejected"),
    ],
)
def test_service_remains_quiescent_until_shutdown_after_permanent_rejection(
    tmp_path: Path,
    rejection: AgentError,
) -> None:
    stop_event = threading.Event()
    write_private(tmp_path / "node.token", NODE_TOKEN)
    metrics = MetricsCollector(tmp_path, FakeMetricsProbe())
    client = PermanentlyRejectedControlClient(rejection)
    service = AgentService(
        settings=AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        client=client,
        credentials=CredentialStore(tmp_path / "enrollment.token", tmp_path / "node.token"),
        metrics=metrics,
        commands=CommandProcessor(
            agent_version="0.1.0",
            journal=CommandJournal(tmp_path / "commands.json"),
            snapshot_supplier=metrics.collect,
            self_test_probe=PassingSelfTestProbe(),
        ),
        stop_event=stop_event,
    )
    failures: list[BaseException] = []

    def run_service() -> None:
        try:
            service.run()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=run_service)
    thread.start()
    assert client.rejection_seen.wait(2)
    time.sleep(0.1)
    assert thread.is_alive()
    calls = (client.heartbeat_calls, client.command_calls)
    time.sleep(0.2)
    assert (client.heartbeat_calls, client.command_calls) == calls

    stop_event.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failures == []


class RejectedEnrollmentControlClient:
    def __init__(self) -> None:
        self.rejection_seen = threading.Event()

    def enroll(
        self, _enrollment_token: SensitiveToken, _snapshot: NodeSnapshot
    ) -> EnrollmentResponse:
        self.rejection_seen.set()
        raise EnrollmentRejected("enrollment_rejected", "enrollment rejected")

    def heartbeat(
        self,
        _node_token: SensitiveToken,
        _snapshot: NodeSnapshot,
        *,
        current_command_id: str | None,
    ) -> None:
        raise AssertionError("heartbeat must not start after enrollment rejection")

    def next_command(self, _node_token: SensitiveToken, *, wait_seconds: int) -> NodeCommand | None:
        raise AssertionError("command polling must not start after enrollment rejection")

    def ack_command(self, _node_token: SensitiveToken, _command_id: str) -> None:
        raise AssertionError("command ack must not start after enrollment rejection")

    def complete_command(
        self,
        _node_token: SensitiveToken,
        _command_id: str,
        _completion: CommandCompletion,
    ) -> None:
        raise AssertionError("command completion must not start after enrollment rejection")


def test_service_quiesces_and_preserves_file_after_enrollment_rejection(tmp_path: Path) -> None:
    stop_event = threading.Event()
    enrollment_path = tmp_path / "enrollment.token"
    write_private(enrollment_path, ENROLLMENT_TOKEN)
    client = RejectedEnrollmentControlClient()
    metrics = MetricsCollector(tmp_path, FakeMetricsProbe())
    service = AgentService(
        settings=AgentSettings(control_url="https://control.test", data_dir=tmp_path),
        client=client,
        credentials=CredentialStore(enrollment_path, tmp_path / "node.token"),
        metrics=metrics,
        commands=CommandProcessor(
            agent_version="0.1.0",
            journal=CommandJournal(tmp_path / "commands.json"),
            snapshot_supplier=metrics.collect,
            self_test_probe=PassingSelfTestProbe(),
        ),
        stop_event=stop_event,
    )
    failures: list[BaseException] = []

    def run_service() -> None:
        try:
            service.run()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=run_service)
    thread.start()
    assert client.rejection_seen.wait(2)
    time.sleep(0.1)
    assert thread.is_alive()
    assert enrollment_path.exists()
    assert not (tmp_path / "node.token").exists()

    stop_event.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failures == []


@pytest.mark.parametrize("error_type", [ConfigurationError, CredentialError, ProtocolError])
def test_persistent_local_startup_errors_remain_quiescent_until_shutdown(
    error_type: type[AgentError],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = threading.Event()
    calls = 0

    def reject_settings() -> AgentSettings:
        nonlocal calls
        calls += 1
        raise error_type("persistent_local_error", "persistent local error")

    monkeypatch.setattr(AgentSettings, "from_env", staticmethod(reject_settings))
    caplog.set_level(logging.ERROR, logger="node_agent")
    results: list[int] = []
    thread = threading.Thread(target=lambda: results.append(_run_agent(stop_event)))
    thread.start()
    time.sleep(0.1)

    assert thread.is_alive()
    assert calls == 1
    matching = [
        record
        for record in caplog.records
        if "Agent entered quiescent state (persistent_local_error)" in record.getMessage()
    ]
    assert len(matching) == 1

    stop_event.set()
    thread.join(2)
    assert not thread.is_alive()
    assert results == [0]


def test_node_image_is_non_root_outbound_only_and_contains_media_tools() -> None:
    dockerfile = Path("Dockerfile.node").read_text(encoding="utf-8")
    lowered = dockerfile.lower()

    assert "user 10001:10001" in lowered
    assert "ffmpeg" in lowered
    assert 'cmd ["python", "-m", "node_agent"]' in lowered
    assert "expose " not in lowered
    assert "openssh" not in lowered
    assert "docker.sock" not in lowered
    assert "node_token=" not in lowered
    assert "enrollment_token=" not in lowered
