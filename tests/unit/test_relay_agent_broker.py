from __future__ import annotations

import copy
import json
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_agent.broker import (
    BROKER_CLIENT_TIMEOUT_SECONDS,
    BROKER_RESPONSE_RESERVE_SECONDS,
    MAX_BROKER_MESSAGE_BYTES,
    RelayBroker,
    _acquire_relay_transaction_lock,
    _enable_child_subreaper,
    _execute_bounded_request,
    _read_bounded_json,
    _unwrap_request_deadline,
    peer_credentials,
    peer_is_expected_agent,
    validate_official_youtube_endpoint,
)
from relay_agent.errors import RelayAgentError
from relay_agent.models import RelaySnapshot


class FakeSocket:
    def __init__(self, chunks: list[bytes] | None = None, peer: tuple[int, int, int] = (9, 10, 11)):
        self.chunks = list(chunks or [])
        self.peer = peer

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def getsockopt(self, _level: int, _option: int, _length: int) -> bytes:
        return struct.pack("3i", *self.peer)


class FakeRelayCtl:
    def __init__(self) -> None:
        self.active = False
        self.enabled = False
        self.lock_depth = 0
        self.saved = 0
        self.start_outside_lock = False
        self.stop_outside_lock = False
        self.secrets = {
            "youtube": {"url": "", "key": ""},
            "srt": {"user": "u", "password": "p", "passphrase": "phrase"},
        }

        owner = self

        class Lock:
            def __enter__(self) -> Lock:
                owner.lock_depth += 1
                return self

            def __exit__(self, *_args: object) -> None:
                owner.lock_depth -= 1

        self.Lock = Lock

    def namespace(self) -> dict[str, object]:
        return {
            "RelayLock": self.Lock,
            "atomic_save_secrets": self.atomic_save,
            "cmd_health": self.cmd_health,
            "cmd_show_moblin_url": self.show_moblin,
            "cmd_start": self.start,
            "cmd_stop": self.stop,
            "get_main_pid": lambda: 4242 if self.active else 0,
            "load_secrets": self.load,
            "parse_metric_samples": self.parse_metrics,
            "read_metrics": lambda: "metrics",
            "run_quiet": self.run_quiet,
            "service_allows_reconfiguration": lambda: not self.active,
            "service_is_active": lambda: self.active,
            "service_is_enabled": lambda: self.enabled,
            "validate_youtube": self.validate_youtube,
            "youtube_state": lambda data: (
                bool(data["youtube"]["url"]),
                bool(data["youtube"]["key"]),
            ),
            "PUBLIC_HOST": "203.0.113.10",
            "VPN_HOST": "10.0.0.1",
            "SRT_PATH": "iphone-live",
            "SRT_PORT": 8890,
        }

    def load(self, optional: bool = False) -> dict[str, object]:
        del optional
        return copy.deepcopy(self.secrets)

    def atomic_save(self, value: dict[str, object]) -> None:
        self.saved += 1
        self.secrets = copy.deepcopy(value)

    @staticmethod
    def validate_youtube(url: str, key: str) -> tuple[str, str]:
        url = url.strip()
        key = key.strip()
        if not url.startswith("rtmps://") or not key or "#" in url:
            raise ValueError
        return url, key

    def start(self) -> int:
        self.start_outside_lock = self.lock_depth == 0
        with self.Lock():
            self.active = True
            self.enabled = True
        return 0

    def stop(self) -> int:
        self.stop_outside_lock = self.lock_depth == 0
        with self.Lock():
            self.active = False
            self.enabled = False
        return 0

    def cmd_health(self) -> int:
        print(f"Service: {'active' if self.active else 'inactive'}")
        print(f"Enabled at boot: {'yes' if self.enabled else 'no'}")
        print(f"Main process: {'running' if self.active else 'not running'}")
        print(f"SRT listener UDP/8890: {'listening' if self.active else 'not listening'}")
        print(f"Source: {'SLATE' if self.active else 'unknown'}")
        print(f"YouTube forward: {'active' if self.active else 'not active'}")
        print(f"Overall: {'PASS' if self.active else 'FAIL'}")
        return 0 if self.active else 1

    @staticmethod
    def parse_metrics(_metrics: str, name: str) -> list[tuple[dict[str, str], float]]:
        if name == "paths":
            return [({"name": "iphone-live", "state": "ready"}, 1.0)]
        if name == "forward_dests":
            return [
                (
                    {
                        "path": "iphone-live",
                        "protocol": "rtmps",
                        "state": "forwarding",
                    },
                    1.0,
                )
            ]
        return []

    def run_quiet(self, args: list[str]) -> SimpleNamespace:
        if args[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout="active\n" if self.active else "inactive\n")
        if args[0] == "/usr/bin/ss":
            return SimpleNamespace(returncode=0, stdout=":8890\n" if self.active else "")
        return SimpleNamespace(returncode=1, stdout="")

    @staticmethod
    def show_moblin() -> int:
        query = (
            "streamid=publish:iphone-live:u:p&passphrase=phrase"
            "&pbkeylen=32&latency=2000&payloadsize=1316"
        )
        print("Public SRT URL:")
        print(f"srt://203.0.113.10:8890?{query}")
        print("VPN SRT URL (fallback):")
        print(f"srt://10.0.0.1:8890?{query}")
        return 0


def broker_with_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[RelayBroker, FakeRelayCtl]:
    fake = FakeRelayCtl()
    broker = RelayBroker(fake.namespace())
    monkeypatch.setattr(broker, "_portrait_profile", lambda: True)
    return broker, fake


@pytest.mark.parametrize(
    "url",
    [
        "rtmps://a.rtmps.youtube.com/live2",
        "rtmps://a.rtmps.youtube.com:443/live2",
        "rtmps://b.rtmps.youtube.com/live2",
    ],
)
def test_official_youtube_endpoint_accepts_studio_forms(url: str) -> None:
    validate_official_youtube_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "rtmp://a.rtmp.youtube.com/live2",
        "rtmps://a.rtmp.youtube.com/live2",
        "rtmps://x.rtmps.youtube.com/live2",
        "rtmps://sub.a.rtmps.youtube.com/live2",
        "rtmps://youtube.example/live2",
        "rtmps://a.rtmp.youtube.com.evil.test/live2",
        "rtmps://a.rtmp.youtube.com/other",
        "rtmps://a.rtmp.youtube.com/live2?x=1",
        "rtmps://a.rtmp.youtube.com:444/live2",
    ],
)
def test_official_youtube_endpoint_rejects_non_studio_forms(url: str) -> None:
    with pytest.raises(RelayAgentError, match="invalid_configuration"):
        validate_official_youtube_endpoint(url)


def test_broker_configure_clear_start_stop_and_reveal(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = broker_with_fake(monkeypatch)
    configured = broker.handle(
        {
            "action": "configure_youtube",
            "payload": {
                "youtube_rtmps_url": "  rtmps://a.rtmps.youtube.com/live2\n",
                "youtube_stream_key": "  fixture-key_123\n",
            },
        }
    )
    assert configured["status"] == "ok"
    assert fake.secrets["youtube"] == {
        "url": "rtmps://a.rtmps.youtube.com/live2",
        "key": "fixture-key_123",
    }
    assert configured["secret_result"] is None

    started = broker.handle({"action": "start", "payload": {}})
    assert started["status"] == "ok"
    assert fake.start_outside_lock is True
    assert started["safe_result"] == {
        "service_state": "active",
        "enabled": True,
        "main_process": "running",
        "srt_listener": "listening",
        "source": "SLATE",
        "youtube_forward": "active",
        "overall": "healthy",
        "youtube_url_configured": True,
        "youtube_key_configured": True,
        "healthy": True,
        "portrait_profile": True,
        "error_code": None,
    }

    conflict = broker.handle(
        {
            "action": "configure_youtube",
            "payload": {
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": "replacement",
            },
        }
    )
    assert conflict["status"] == "conflict"
    assert conflict["safe_result"]["error_code"] == "relay_active"
    assert fake.saved == 1

    revealed = broker.handle({"action": "reveal_moblin_url", "payload": {}})
    assert revealed["status"] == "ok"
    assert str(revealed["secret_result"]).startswith("Public SRT URL:\nsrt://")

    stopped = broker.handle({"action": "stop", "payload": {}})
    assert stopped["status"] == "ok"
    assert fake.stop_outside_lock is True
    cleared = broker.handle({"action": "clear_youtube", "payload": {}})
    assert cleared["status"] == "ok"
    assert fake.secrets["youtube"] == {"url": "", "key": ""}


def test_broker_rejects_oversize_and_authenticates_exact_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = FakeSocket([b"x" * (MAX_BROKER_MESSAGE_BYTES + 1)])
    with pytest.raises(RelayAgentError, match="request_too_large"):
        _read_bounded_json(oversized)  # type: ignore[arg-type]
    monkeypatch.setattr("relay_agent.broker.socket.SO_PEERCRED", 17, raising=False)
    peer = FakeSocket(peer=(123, 456, 789))
    assert peer_credentials(peer) == (123, 456, 789)  # type: ignore[arg-type]
    assert peer_is_expected_agent(peer, 456) is True  # type: ignore[arg-type]
    assert peer_is_expected_agent(peer, 789) is False  # type: ignore[arg-type]


def test_broker_request_is_strict_json() -> None:
    encoded = json.dumps({"action": "status", "payload": {}}).encode()
    assert _read_bounded_json(FakeSocket([encoded])) == {  # type: ignore[arg-type]
        "action": "status",
        "payload": {},
    }
    with pytest.raises(RelayAgentError, match="invalid_request"):
        _read_bounded_json(  # type: ignore[arg-type]
            FakeSocket([b'{"action":"status","action":"stop","payload":{}}'])
        )
    with pytest.raises(RelayAgentError, match="invalid_request"):
        _read_bounded_json(  # type: ignore[arg-type]
            FakeSocket([b'{"action":"status","payload":{"value":NaN}}'])
        )


def test_configuration_commit_has_no_blocking_snapshot_after_atomic_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, fake = broker_with_fake(monkeypatch)
    original_snapshot = broker.snapshot
    snapshot_calls = 0

    def snapshot_before_commit_only():
        nonlocal snapshot_calls
        if fake.saved:
            raise AssertionError("snapshot ran after the durable commit")
        snapshot_calls += 1
        return original_snapshot()

    monkeypatch.setattr(broker, "snapshot", snapshot_before_commit_only)
    configured = broker.handle(
        {
            "action": "configure_youtube",
            "payload": {
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": "post-commit-fixture",
            },
        }
    )
    assert configured["status"] == "ok"
    assert configured["safe_result"]["youtube_key_configured"] is True
    assert fake.saved == 1
    assert snapshot_calls == 1

    fake.saved = 0
    snapshot_calls = 0
    cleared = broker.handle({"action": "clear_youtube", "payload": {}})
    assert cleared["status"] == "ok"
    assert cleared["safe_result"]["youtube_key_configured"] is False
    assert fake.saved == 1
    assert snapshot_calls == 1


def test_configuration_commit_preserves_non_youtube_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, fake = broker_with_fake(monkeypatch)
    monkeypatch.setattr(broker, "snapshot", lambda: RelaySnapshot.unavailable("relayctl_failed"))
    configured = broker.handle(
        {
            "action": "configure_youtube",
            "payload": {
                "youtube_rtmps_url": "rtmps://a.rtmps.youtube.com/live2",
                "youtube_stream_key": "failure-preservation-fixture",
            },
        }
    )
    assert configured["status"] == "ok"
    assert configured["safe_result"]["youtube_url_configured"] is True
    assert configured["safe_result"]["youtube_key_configured"] is True
    assert configured["safe_result"]["error_code"] == "relayctl_failed"

    cleared = broker.handle({"action": "clear_youtube", "payload": {}})
    assert cleared["status"] == "ok"
    assert cleared["safe_result"]["youtube_url_configured"] is False
    assert cleared["safe_result"]["youtube_key_configured"] is False
    assert cleared["safe_result"]["error_code"] == "relayctl_failed"


def test_deadline_envelope_rejects_expired_and_untrusted_long_values() -> None:
    base = {"action": "status", "payload": {}}
    with pytest.raises(RelayAgentError, match="invalid_request"):
        _unwrap_request_deadline({**base, "deadline_monotonic_ns": time.monotonic_ns() - 1})
    with pytest.raises(RelayAgentError, match="invalid_request"):
        _unwrap_request_deadline(
            {
                **base,
                "deadline_monotonic_ns": time.monotonic_ns()
                + int((BROKER_CLIENT_TIMEOUT_SECONDS + 1) * 1_000_000_000),
            }
        )


def test_fresh_but_short_mutation_deadline_never_starts_worker() -> None:
    class MustNotRun:
        called = False

        def reconciliation_state(self, _request: object) -> None:
            return None

        def handle(self, _request: object, *, relay_lock_held: bool = False) -> dict[str, object]:
            del relay_lock_held
            self.called = True
            return {}

    broker = MustNotRun()
    action_timeout = 0.5
    response = _execute_bounded_request(
        broker,  # type: ignore[arg-type]
        {"action": "configure_youtube", "payload": {}},
        client_deadline=time.monotonic() + action_timeout + BROKER_RESPONSE_RESERVE_SECONDS - 0.1,
        action_timeout_seconds=action_timeout,
    )
    assert json.loads(response)["status"] == "failed"
    assert broker.called is False


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX descriptor semantics only")
def test_relay_transaction_lock_closes_fd_when_validation_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "relay-lock-validation"
    lock_path = lock_directory / "control.lock"
    monkeypatch.setattr("relay_agent.broker._RELAY_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr("relay_agent.broker._RELAY_LOCK_PATH", lock_path)
    real_fstat = os.fstat
    opened_fd = -1

    def fail_fstat(fd: int):
        nonlocal opened_fd
        opened_fd = fd
        raise OSError("fixture fstat failure")

    monkeypatch.setattr("relay_agent.broker.os.fstat", fail_fstat)
    assert _acquire_relay_transaction_lock(time.monotonic() + 1.0) is None
    assert opened_fd >= 0
    with pytest.raises(OSError):
        real_fstat(opened_fd)


class FileBackedTimedMutationBroker:
    def __init__(
        self,
        state_path: Path,
        descendant_pid_path: Path,
        mutation_state: tuple[bool, bool],
    ) -> None:
        self.state_path = state_path
        self.descendant_pid_path = descendant_pid_path
        self.mutation_state = mutation_state

    def reconciliation_state(self, _request: object) -> tuple[bool, bool]:
        active, enabled = self.state_path.read_text(encoding="ascii").split(",")
        return active == "1", enabled == "1"

    def handle(self, _request: object, *, relay_lock_held: bool = False) -> dict[str, object]:
        assert relay_lock_held is True
        self._write_state(self.mutation_state)
        descendant_pid = os.fork()
        if descendant_pid == 0:
            self.descendant_pid_path.write_text(str(os.getpid()), encoding="ascii")
            time.sleep(0.7)
            self._write_state(self.mutation_state)
            os._exit(0)
        wait_until = time.monotonic() + 0.1
        while not self.descendant_pid_path.exists() and time.monotonic() < wait_until:
            time.sleep(0.002)
        time.sleep(2.0)
        return {}

    def _write_state(self, state: tuple[bool, bool]) -> None:
        self.state_path.write_text(f"{int(state[0])},{int(state[1])}", encoding="ascii")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux fork isolation only")
@pytest.mark.parametrize(
    ("action", "before", "during"),
    [
        ("start", (False, False), (True, True)),
        ("stop", (True, True), (False, False)),
    ],
)
def test_timed_out_start_stop_reconciles_and_reaps_entire_worker_group(
    action: str,
    before: tuple[bool, bool],
    during: tuple[bool, bool],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    lock_directory = tmp_path / "relay-lock"
    lock_path = lock_directory / "control.lock"
    monkeypatch.setattr("relay_agent.broker._RELAY_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr("relay_agent.broker._RELAY_LOCK_PATH", lock_path)
    _enable_child_subreaper()

    state_path = tmp_path / "state"
    state_path.write_text(f"{int(before[0])},{int(before[1])}", encoding="ascii")
    descendant_pid_path = tmp_path / "descendant.pid"
    lock_proven_path = tmp_path / "lock-proven"
    broker = FileBackedTimedMutationBroker(state_path, descendant_pid_path, during)

    def reconcile(active: bool, enabled: bool, _deadline: float) -> bool:
        competing_fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competing_fd)
        lock_proven_path.write_text("held", encoding="ascii")
        state_path.write_text(f"{int(active)},{int(enabled)}", encoding="ascii")
        return True

    started = time.monotonic()
    response = _execute_bounded_request(
        broker,  # type: ignore[arg-type]
        {"action": action, "payload": {}},
        client_deadline=time.monotonic() + 3.0,
        action_timeout_seconds=0.25,
        reconcile_timeout_seconds=0.5,
        reconcile=reconcile,
    )
    assert time.monotonic() - started < 2.0
    assert json.loads(response)["status"] == "failed"
    assert state_path.read_text(encoding="ascii") == f"{int(before[0])},{int(before[1])}"
    assert lock_proven_path.read_text(encoding="ascii") == "held"
    assert descendant_pid_path.exists()
    descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
    time.sleep(0.8)
    assert state_path.read_text(encoding="ascii") == f"{int(before[0])},{int(before[1])}"
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)
