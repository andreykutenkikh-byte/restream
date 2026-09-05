from __future__ import annotations

import base64
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "moblin-relay"
SELF_TEST = BUNDLE / "self-test"
NORMALIZER = BUNDLE / "moblin-relay-normalize"


def load_self_test() -> dict[str, object]:
    fcntl = ModuleType("fcntl")
    resource = ModuleType("resource")
    with (
        patch.dict(sys.modules, {"fcntl": fcntl, "resource": resource}),
        patch.dict(os.environ, {"MOBLIN_RELAY_SELF_TEST_STAGE_FILE": ""}),
    ):
        return runpy.run_path(str(SELF_TEST), run_name="_relay_recovery_self_test")


def load_normalizer() -> dict[str, object]:
    return runpy.run_path(str(NORMALIZER), run_name="_relay_recovery_normalizer")


def test_recovery_test_dut_uses_only_a_scoped_hashed_control_credential() -> None:
    loaded = load_self_test()
    write_configs = loaded["write_configs"]
    assert callable(write_configs)
    captured: list[dict[str, object]] = []

    def capture_config(
        _path: Path,
        value: dict[str, object],
        _mode: int = 0o600,
    ) -> None:
        captured.append(value)

    token = b"A" * 43
    with patch.dict(write_configs.__globals__, {"atomic_json": capture_config}):  # type: ignore[attr-defined]
        write_configs(
            Path("/var/lib/moblin-relay/tests/.run-contract"),
            "test_user",
            "test_password",
            "test_passphrase",
            token,
        )

    assert len(captured) == 2
    dut = captured[1]
    assert dut["readTimeout"] == "10s"
    users = dut["authInternalUsers"]
    assert isinstance(users, list)
    recovery_users = [item for item in users if item.get("user") == "relay-recovery"]
    expected_hash = base64.b64encode(hashlib.sha256(token).digest()).decode("ascii")
    assert recovery_users == [
        {
            "user": "relay-recovery",
            "pass": f"sha256:{expected_hash}",
            "ips": ["127.0.0.1", "::1"],
            "permissions": [{"action": "api"}],
        }
    ]
    assert not any(
        permission.get("action") == "api"
        for item in users
        if item.get("user") == "any"
        for permission in item.get("permissions", [])
    )
    assert token.decode("ascii") not in json.dumps(dut, sort_keys=True)


def test_recovery_chaos_budget_and_markers_match_the_runtime_contract() -> None:
    self_test = load_self_test()
    normalizer = load_normalizer()

    assert self_test["BRIDGE_FAILURES_TO_INJECT"] == 3
    assert normalizer["RECOVERY_FAILURE_THRESHOLD"] == 3
    assert normalizer["SOURCE_RESET_ELIGIBLE_REASONS"] == frozenset()
    assert (
        2 * self_test["SUPERVISOR_RESTART_TIMEOUT_SECONDS"]
        < normalizer["RECOVERY_FAILURE_WINDOW_SECONDS"]
    )
    tokens = normalizer["RECOVERY_EVENT_TOKENS"]
    assert self_test["NORMALIZER_RESET_REQUESTED_MARKER"] == tokens[
        normalizer["RECOVERY_EVENT_THRESHOLD"]
    ].encode("ascii")
    assert self_test["NORMALIZER_RESET_SUCCEEDED_MARKER"] == tokens[
        normalizer["RECOVERY_RESULT_KICKED"]
    ].encode("ascii")
    assert self_test["NORMALIZER_BRIDGE_ACTIVE_MARKER"] == normalizer["STATE_EVENT_TOKENS"][
        normalizer["STATE_EVENT_BRIDGE_ACTIVE"]
    ].encode("ascii")
    assert (
        self_test["PERSISTENT_INPUT_STALL_RESET_TIMEOUT_SECONDS"]
        > normalizer["CONFIRMED_INPUT_STALL_GRACE_SECONDS"]
    )
    assert (
        self_test["CONFIRMED_INPUT_STALL_GRACE_SECONDS"]
        == normalizer["CONFIRMED_INPUT_STALL_GRACE_SECONDS"]
    )
    assert self_test["SOURCE_HELPER_PEER_IDLE_TIMEOUT_MILLISECONDS"] == 10_000
    assert normalizer["CONFIRMED_INPUT_STALL_GRACE_SECONDS"] > (
        self_test["SRT_LATENCY_MILLISECONDS"] / 1000
    )
    assert self_test["PERSISTENT_INPUT_STALL_RESET_TIMEOUT_SECONDS"] < (
        self_test["SOURCE_HELPER_PEER_IDLE_TIMEOUT_MILLISECONDS"] / 1000
    )
    assert self_test["NORMALIZER_CONFIRMED_INPUT_STALL_MARKER"] == normalizer["RESTART_LOG_TOKENS"][
        normalizer["RESTART_REASON_INGEST_CONFIRMED_STALL"]
    ].encode("ascii")


def test_repeated_bridge_failures_preserve_growing_srt_and_restore_live() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    block = source.split(
        'print(\n            "  checking repeated FFmpeg recovery without resetting healthy SRT"',
        1,
    )[1].split('print("[4/8] Running required outages:', 1)[0]

    ordered = (
        'mark_self_test_stage("reset-start")',
        "failure_baseline = wait_healthy_live(",
        "for failure_index in range(1, BRIDGE_FAILURES_TO_INJECT + 1):",
        "os.kill(child_pid, signal.SIGKILL)",
        "wait_process_exit(child_pid, 1.0)",
        "NORMALIZER_CHILD_EXIT_MARKER",
        'mark_self_test_stage("reset-slate")',
        "wait_slate_with_live_srt(",
        "wait_slate_downstream_recovery(",
        "final_replacement = wait_healthy_live(",
        "NORMALIZER_BRIDGE_ACTIVE_MARKER",
        'mark_self_test_stage("reset-source")',
        'mark_self_test_stage("reset-cont")',
        "maximum_capture_no_growth_seconds(failure_capture_samples)",
        'result["repeated_bridge_failure_recovery"]',
    )
    positions = [block.index(item) for item in ordered]
    assert positions == sorted(positions)
    assert 'final_replacement.get("ingest_ids") != failure_ingest_ids' in block
    assert "feeder is not failure_feeder" in block
    assert "(publisher.pid, primary_helper.pid) != failure_source_pids" in block
    assert "NORMALIZER_RESET_REQUESTED_MARKER in recovery_tail" in block
    assert "NORMALIZER_RESET_SUCCEEDED_MARKER in recovery_tail" in block
    assert '"source_reset_requested": False' in block
    assert '"source_reset_succeeded": False' in block
    assert '"srt_session_preserved": True' in block
    assert '"live_restored_after_each_failure": True' in block
    assert '"automatic_rtmp_recovery": True' in block
    assert '"restored_bridge_marker": True' in block
    assert "CAPTURE_NO_GROWTH_LIMIT_SECONDS" in block


def test_fixed_recovery_log_wait_is_bounded_and_rejects_untrusted_requests() -> None:
    loaded = load_self_test()
    wait_marker = loaded["wait_log_marker_count"]
    failure = loaded["TestFailure"]
    assert callable(wait_marker)
    marker = b"moblin-relay-normalize:recovery:reset-succeeded"

    with patch.dict(
        wait_marker.__globals__,  # type: ignore[attr-defined]
        {"read_validated_log_tail": lambda *_args: marker + b"\n" + marker},
    ):
        assert wait_marker(-1, 0, marker, 2, 0, 0) == 2

    for invalid_marker, count, timeout in (
        (b"", 1, 0),
        (b"x" * 257, 1, 0),
        (b"x", 0, 0),
        (b"x", 1, -1),
    ):
        with pytest.raises(failure, match="invalid DUT log marker count request"):
            wait_marker(-1, 0, invalid_marker, count, 0, timeout)


def test_persistent_live_socket_without_media_recovers_by_exact_reset() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    block = source.split(
        'print("  checking persistent SRT socket with stopped media", flush=True)',
        1,
    )[1].split(
        'print("  checking supervisor crash containment and automatic recovery", flush=True)',
        1,
    )[0]

    ordered = (
        'mark_self_test_stage("stuck-start")',
        "stuck_baseline = wait_healthy_live(",
        "if not feeder.pause():",
        'mark_self_test_stage("stuck-slate")',
        "stuck_slate = wait_slate_with_live_srt(",
        'observer.checked_snapshot("persistent-stall pre-reset proof")',
        'mark_self_test_stage("stuck-open")',
        'mark_self_test_stage("stuck-kicked")',
        "reset_completed = time.monotonic()",
        "wait_slate_capture_growth(",
        "wait_slate_downstream_recovery(",
        '"stalled SRT session replacement"',
        "if not feeder.resume():",
        'mark_self_test_stage("stuck-live")',
        "stuck_ingest = wait_new_authenticated_ingest(",
        "stuck_live = wait_healthy_live(",
        'mark_self_test_stage("stuck-source")',
        'mark_self_test_stage("stuck-cont")',
        "maximum_capture_no_growth_seconds(stuck_capture_samples)",
        'result["persistent_input_stall_recovery"]',
    )
    positions = [block.index(item) for item in ordered]
    assert positions == sorted(positions)
    for marker in (
        "NORMALIZER_CONFIRMED_INPUT_STALL_MARKER",
        "NORMALIZER_RESET_REQUESTED_MARKER",
        "NORMALIZER_RESET_SUCCEEDED_MARKER",
    ):
        assert marker in block
    assert 'pre_reset.get("ingest_ids") != stuck_ingest_ids' in block
    assert 'stuck_live.get("ingest_ids") == stuck_ingest_ids' in block
    assert "feeder is not stuck_feeder" in block
    assert "(publisher.pid, primary_helper.pid) != stuck_source_pids" in block
    assert 'sink_transition_started=float(stuck_ingest["finished"])' in block
    assert '"recovery_path": "exact-api-reset"' in block
    assert "natural-media-timeout" not in block
    assert 'mark_self_test_stage("stuck-kicked")' in block
    assert "marker_positions" in block
    assert "reset_elapsed < CONFIRMED_INPUT_STALL_GRACE_SECONDS" in block
    assert '"reset_before_transport_idle_timeout": True' in block
    assert '"automatic_srt_recovery": True' in block
    assert '"automatic_rtmp_recovery": True' in block


def test_control_credential_lifecycle_is_atomic_bounded_and_wiped() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    writer = source.split("def write_control_api_token", 1)[1].split("def run(", 1)[0]
    cleanup = source.split("finally:", 1)[-1]

    assert 'work / "control-api.token"' in writer
    assert "secrets.token_bytes(32)" in writer
    assert "os.O_EXCL" in writer
    assert "os.O_NOFOLLOW" in writer
    assert "os.fchmod(descriptor, 0o600)" in writer
    assert "os.fsync(descriptor)" in writer
    assert (
        'work / "control-api.token"'
        in source.split("def discover_secret_configs", 1)[1].split("def cleanup_stale_workdirs", 1)[
            0
        ]
    )
    assert "wipe_secret_config(path, work)" in cleanup
    assert 'dut_environment["MOBLIN_RELAY_CONTROL_TOKEN_FILE"] = str(control_token_path)' in source
    assert (
        "control_api_token"
        not in source.split('dut_environment["MOBLIN_RELAY_CONTROL_TOKEN_FILE"]', 1)[1].split(
            "dut = subprocess.Popen", 1
        )[0]
    )


def test_exhausted_recovery_remains_quiescent_without_retry_or_log_storm() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    breaker_type = loaded["RecoveryCircuitBreaker"]
    source_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    breaker = breaker_type(source_id)
    breaker.open_after_confirmed_input_stall(100, 3.0)
    assert breaker.opened is True

    clock = 3.0
    exhausted_sleeps = 0
    registered_handlers: dict[int, object] = {}
    recovery_events: list[str] = []
    kick_calls: list[tuple[int, str, Path]] = []
    readers: list[FakeMetricsReader] = []

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]
            self.sample_calls = 0
            self.close_calls = 0
            readers.append(self)

        def sample(self):
            self.sample_calls += 1
            return True, (source_id, 100)

        def close(self) -> None:
            self.close_calls += 1

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock, exhausted_sleeps
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds
        if breaker.exhausted:
            exhausted_sleeps += 1
            # Let the supervisor execute several fully exhausted loop iterations.
            # It must neither retry the API nor emit the terminal marker again.
            if exhausted_sleeps == 4:
                registered_handlers[2](0, None)

    def fake_kick(port: int, observed_source_id: str, token_path: Path) -> str:
        kick_calls.append((port, observed_source_id, token_path))
        return loaded["RECOVERY_RESULT_TRANSPORT"]

    def reject_child_launch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exhausted recovery launched a new FFmpeg child")

    fake_signal_module = SimpleNamespace(
        SIGHUP=1,
        SIGINT=2,
        SIGTERM=15,
        signal=fake_signal,
    )
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    token_path = Path("/var/lib/moblin-relay/tests/.run-contract/control-api.token")
    globals_patch = {
        "RecoveryCircuitBreaker": lambda observed_source_id: breaker,
        "MetricsReader": FakeMetricsReader,
        "emit_recovery_event": recovery_events.append,
        "kick_srt_source": fake_kick,
        "signal": fake_signal_module,
        "time": fake_time_module,
    }
    with (
        patch.dict(run_supervisor.__globals__, globals_patch),  # type: ignore[attr-defined]
        patch.object(loaded["subprocess"], "Popen", reject_child_launch),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id, 19997, token_path)

    failed_event = loaded["RECOVERY_RESULT_TRANSPORT"]
    exhausted_event = loaded["RECOVERY_EVENT_EXHAUSTED"]
    assert result == 0
    assert kick_calls == [(19997, source_id, token_path)] * loaded["RECOVERY_MAX_API_ATTEMPTS"]
    assert recovery_events == [
        failed_event,
        failed_event,
        failed_event,
        exhausted_event,
    ]
    assert breaker.attempts == loaded["RECOVERY_MAX_API_ATTEMPTS"]
    assert breaker.exhausted is True
    assert exhausted_sleeps == 4
    assert len(readers) == 2
    ingest_reader = next(reader for reader in readers if "srt_conns" in reader.path)
    output_reader = next(reader for reader in readers if "rtmp_conns" in reader.path)
    assert ingest_reader.sample_calls > loaded["RECOVERY_MAX_API_ATTEMPTS"]
    assert output_reader.sample_calls == 0


def test_successful_recovery_waits_quiescently_for_source_teardown() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    breaker_type = loaded["RecoveryCircuitBreaker"]
    source_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    breaker = breaker_type(source_id)
    breaker.open_after_confirmed_input_stall(100, 3.0)
    assert breaker.opened is True

    clock = 3.0
    completed_sleeps = 0
    registered_handlers: dict[int, object] = {}
    recovery_events: list[str] = []
    kick_calls: list[tuple[int, str, Path]] = []
    readers: list[FakeMetricsReader] = []

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]
            self.sample_calls = 0
            readers.append(self)

        def sample(self):
            self.sample_calls += 1
            return True, (source_id, 100)

        def close(self) -> None:
            return None

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock, completed_sleeps
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds
        if breaker.completed:
            completed_sleeps += 1
            if completed_sleeps == 4:
                registered_handlers[2](0, None)

    def fake_kick(port: int, observed_source_id: str, token_path: Path) -> str:
        kick_calls.append((port, observed_source_id, token_path))
        return loaded["RECOVERY_RESULT_KICKED"]

    def reject_child_launch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("completed recovery launched a new FFmpeg child")

    fake_signal_module = SimpleNamespace(
        SIGHUP=1,
        SIGINT=2,
        SIGTERM=15,
        signal=fake_signal,
    )
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    token_path = Path("/var/lib/moblin-relay/tests/.run-contract/control-api.token")
    globals_patch = {
        "RecoveryCircuitBreaker": lambda observed_source_id: breaker,
        "MetricsReader": FakeMetricsReader,
        "emit_recovery_event": recovery_events.append,
        "kick_srt_source": fake_kick,
        "signal": fake_signal_module,
        "time": fake_time_module,
    }
    with (
        patch.dict(run_supervisor.__globals__, globals_patch),  # type: ignore[attr-defined]
        patch.object(loaded["subprocess"], "Popen", reject_child_launch),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id, 19997, token_path)

    assert result == 0
    assert kick_calls == [(19997, source_id, token_path)]
    assert recovery_events == [loaded["RECOVERY_RESULT_KICKED"]]
    assert breaker.attempts == 1
    assert breaker.completed is True
    assert completed_sleeps == 4
    assert len(readers) == 2
    ingest_reader = next(reader for reader in readers if "srt_conns" in reader.path)
    output_reader = next(reader for reader in readers if "rtmp_conns" in reader.path)
    assert ingest_reader.sample_calls == loaded["RECOVERY_PRE_RESET_REQUIRED_OBSERVATIONS"]
    assert output_reader.sample_calls == 0


def test_recovered_source_at_retry_boundary_defers_reset_and_reopens_bridge() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    breaker_type = loaded["RecoveryCircuitBreaker"]
    source_id = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
    breaker = breaker_type(source_id)
    breaker.open_after_confirmed_input_stall(100, 3.0)
    assert breaker.opened is True

    clock = 3.0
    first_attempt_due = clock + loaded["RECOVERY_PRE_RESET_CONFIRMATION_SECONDS"]
    first_retry_due = first_attempt_due + loaded["RECOVERY_RETRY_COOLDOWN_SECONDS"]
    registered_handlers: dict[int, object] = {}
    recovery_events: list[str] = []
    state_events: list[str] = []
    kick_calls: list[tuple[int, str, Path, float]] = []
    launched_children = []
    readers = []

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]
            self.sample_calls = 0
            readers.append(self)

        def sample(self):
            self.sample_calls += 1
            if "rtmp_conns" in self.path:
                raise AssertionError("output metrics were sampled before the bridge reopened")
            if breaker.opened:
                if clock < first_retry_due:
                    counter = 100
                elif clock < first_retry_due + 1.0:
                    counter = 110
                else:
                    assert breaker.next_attempt_at > clock
                    counter = 120
            else:
                counter = 120 + (10 * self.sample_calls)
            return True, (source_id, counter)

        def close(self) -> None:
            return None

    class FakeChild:
        def __init__(self) -> None:
            self.running = True

        def poll(self):
            return None if self.running else 0

        def send_signal(self, _signum: int) -> None:
            self.running = False

        def kill(self) -> None:
            self.running = False

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.running = False
            return 0

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds

    def fake_kick(port: int, observed_source_id: str, token_path: Path) -> str:
        kick_calls.append((port, observed_source_id, token_path, clock))
        return loaded["RECOVERY_RESULT_TRANSPORT"]

    def fake_popen(*_args: object, **_kwargs: object) -> FakeChild:
        child = FakeChild()
        launched_children.append(child)
        registered_handlers[2](0, None)
        return child

    fake_signal_module = SimpleNamespace(
        SIGHUP=1,
        SIGINT=2,
        SIGTERM=15,
        signal=fake_signal,
    )
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    token_path = Path("/var/lib/moblin-relay/tests/.run-contract/control-api.token")
    globals_patch = {
        "RecoveryCircuitBreaker": lambda observed_source_id: breaker,
        "MetricsReader": FakeMetricsReader,
        "emit_recovery_event": recovery_events.append,
        "emit_state_event": state_events.append,
        "kick_srt_source": fake_kick,
        "make_parent_death_setup": lambda _pid: lambda: None,
        "signal": fake_signal_module,
        "time": fake_time_module,
    }
    with (
        patch.dict(run_supervisor.__globals__, globals_patch),  # type: ignore[attr-defined]
        patch.object(loaded["subprocess"], "Popen", fake_popen),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id, 19997, token_path)

    assert result == 0
    assert kick_calls == [(19997, source_id, token_path, first_attempt_due)]
    assert recovery_events == [
        loaded["RECOVERY_RESULT_TRANSPORT"],
        loaded["RECOVERY_EVENT_SOURCE_RESUMED"],
    ]
    assert breaker.opened is False
    assert breaker.attempts == 0
    assert launched_children
    assert loaded["STATE_EVENT_SOURCE_ATTACHED"] in state_events
    assert loaded["STATE_EVENT_SOURCE_DETACHED"] in state_events


def test_growth_at_initial_reset_boundary_cancels_post_and_reopens_bridge() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    source_id = "dddddddd-eeee-4fff-8aaa-bbbbbbbbbbbb"
    breaker = loaded["RecoveryCircuitBreaker"](source_id)
    assert breaker.open_after_confirmed_input_stall(100, 10.0) is True

    clock = 10.0
    registered_handlers: dict[int, object] = {}
    recovery_events: list[str] = []
    launched_children: list[object] = []
    ingest_counter = 100

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]

        def sample(self):
            nonlocal ingest_counter
            if "rtmp_conns" in self.path:
                raise AssertionError("output metrics were sampled before shutdown")
            ingest_counter += 10
            return True, (source_id, ingest_counter)

        def close(self) -> None:
            return None

    class FakeChild:
        def __init__(self) -> None:
            self.running = True

        def poll(self):
            return None if self.running else 0

        def send_signal(self, _signum: int) -> None:
            self.running = False

        def kill(self) -> None:
            self.running = False

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.running = False
            return 0

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds

    def reject_kick(*_args: object) -> str:
        raise AssertionError("recovered source was kicked")

    def fake_popen(*_args: object, **_kwargs: object) -> FakeChild:
        child = FakeChild()
        launched_children.append(child)
        registered_handlers[2](0, None)
        return child

    fake_signal_module = SimpleNamespace(SIGHUP=1, SIGINT=2, SIGTERM=15, signal=fake_signal)
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    with (
        patch.dict(
            run_supervisor.__globals__,  # type: ignore[attr-defined]
            {
                "RecoveryCircuitBreaker": lambda _source_id: breaker,
                "MetricsReader": FakeMetricsReader,
                "emit_recovery_event": recovery_events.append,
                "kick_srt_source": reject_kick,
                "make_parent_death_setup": lambda _pid: lambda: None,
                "signal": fake_signal_module,
                "time": fake_time_module,
            },
        ),
        patch.object(loaded["subprocess"], "Popen", fake_popen),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id)

    assert result == 0
    assert recovery_events == [loaded["RECOVERY_EVENT_SOURCE_RESUMED"]]
    assert breaker.opened is False
    assert breaker.attempts == 0
    assert launched_children


def test_metrics_failure_before_post_requires_a_new_full_six_second_proof() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    source_id = "eeeeeeee-ffff-4aaa-8bbb-cccccccccccc"
    breaker = loaded["RecoveryCircuitBreaker"](source_id)
    assert breaker.open_after_confirmed_input_stall(100, 10.0) is True

    clock = 10.0
    registered_handlers: dict[int, object] = {}
    recovery_events: list[str] = []
    restart_events: list[str] = []
    kick_times: list[float] = []
    ingest_samples = 0

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]

        def sample(self):
            nonlocal ingest_samples
            if "rtmp_conns" in self.path:
                raise AssertionError("output metrics were sampled without a child")
            ingest_samples += 1
            if ingest_samples == 2:
                return False, None
            return True, (source_id, 100)

        def close(self) -> None:
            return None

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds

    def fake_kick(_port: int, observed_source_id: str, _token_path: Path) -> str:
        assert observed_source_id == source_id
        kick_times.append(clock)
        registered_handlers[2](0, None)
        return loaded["RECOVERY_RESULT_KICKED"]

    def reject_child_launch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("flat source launched FFmpeg")

    fake_signal_module = SimpleNamespace(SIGHUP=1, SIGINT=2, SIGTERM=15, signal=fake_signal)
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    with (
        patch.dict(
            run_supervisor.__globals__,  # type: ignore[attr-defined]
            {
                "RecoveryCircuitBreaker": lambda _source_id: breaker,
                "MetricsReader": FakeMetricsReader,
                "emit_recovery_event": recovery_events.append,
                "emit_restart_reason": restart_events.append,
                "kick_srt_source": fake_kick,
                "signal": fake_signal_module,
                "time": fake_time_module,
            },
        ),
        patch.object(loaded["subprocess"], "Popen", reject_child_launch),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id)

    assert result == 0
    assert len(kick_times) == 1
    assert kick_times[0] >= 16.0
    assert restart_events == [loaded["RESTART_REASON_INGEST_CONFIRMED_STALL"]]
    assert recovery_events == [
        loaded["RECOVERY_EVENT_THRESHOLD"],
        loaded["RECOVERY_RESULT_KICKED"],
    ]


def test_three_child_exits_with_growing_input_never_authorize_srt_post() -> None:
    loaded = load_normalizer()
    run_supervisor = loaded["run_supervisor"]
    source_id = "ffffffff-aaaa-4bbb-8ccc-dddddddddddd"
    breaker = loaded["RecoveryCircuitBreaker"](source_id)

    clock = 1.0
    registered_handlers: dict[int, object] = {}
    restart_events: list[str] = []
    recovery_events: list[str] = []
    launch_count = 0
    exit_count = 0
    ingest_counter = 0

    class FakeMetricsReader:
        def __init__(self, *_args: object) -> None:
            self.path = _args[1]

        def sample(self):
            nonlocal ingest_counter
            if "rtmp_conns" in self.path:
                raise AssertionError("exited child published output unexpectedly")
            ingest_counter += 10
            return True, (source_id, ingest_counter)

        def close(self) -> None:
            return None

    class ExitedChild:
        def poll(self) -> int:
            nonlocal exit_count
            exit_count += 1
            return 1

    def fake_signal(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock
        assert 0 < seconds <= loaded["RECOVERY_WAIT_POLL_SECONDS"]
        clock += seconds
        if exit_count >= 3:
            registered_handlers[2](0, None)

    def reject_kick(*_args: object) -> str:
        raise AssertionError("bridge exits authorized an SRT reset")

    def fake_popen(*_args: object, **_kwargs: object) -> ExitedChild:
        nonlocal launch_count
        launch_count += 1
        return ExitedChild()

    fake_signal_module = SimpleNamespace(SIGHUP=1, SIGINT=2, SIGTERM=15, signal=fake_signal)
    fake_time_module = SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
    with (
        patch.dict(
            run_supervisor.__globals__,  # type: ignore[attr-defined]
            {
                "RecoveryCircuitBreaker": lambda _source_id: breaker,
                "MetricsReader": FakeMetricsReader,
                "emit_recovery_event": recovery_events.append,
                "emit_restart_reason": restart_events.append,
                "kick_srt_source": reject_kick,
                "make_parent_death_setup": lambda _pid: lambda: None,
                "signal": fake_signal_module,
                "time": fake_time_module,
            },
        ),
        patch.object(loaded["subprocess"], "Popen", fake_popen),
    ):
        result = run_supervisor(18554, 11936, 19998, source_id)

    assert result == 0
    assert launch_count == exit_count == 3
    assert restart_events == [loaded["RESTART_REASON_CHILD_EXIT"]] * 3
    assert recovery_events == []
    assert breaker.opened is False
    assert breaker.attempts == 0
