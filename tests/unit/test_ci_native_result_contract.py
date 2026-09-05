"""Current native media result gates, executed without a server or credentials."""

import copy
import io
import json
import os
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from bootstrap_worker.relay_installer import RELAY_RELEASE
from scripts import ci_node_onboarding_smoke as smoke
from scripts.ci_output_smoke import SmokeFailure


@pytest.fixture
def native_result() -> dict[str, Any]:
    events = [
        "initial healthy LIVE",
        "same-session SLATE transition",
        "same-session LIVE recovery",
        "persistent-stall SLATE transition",
        "persistent-stall LIVE recovery",
        "supervisor-crash SLATE transition",
        "supervisor-crash LIVE recovery",
    ]
    for index in range(1, 4):
        events += [
            f"repeated-failure {index} SLATE transition",
            f"LIVE after forced bridge failure {index}",
        ]
    for index in range(1, 4):
        events += [f"outage {index} SLATE transition", f"outage {index} LIVE recovery"]
    events += ["forced RTMP sink reconnect", "final SLATE transition"]
    return {
        "status": "PASS",
        "mode": "quick",
        "result_phase": "final",
        "outage_targets_seconds": [15, 17, 19],
        "same_session_stall": {
            "srt_connection_preserved": True,
            "upstream_processes_preserved": True,
            "feeder_thread_preserved": True,
            "feeder_remux_preserved": True,
            "normalizer_reconnected": True,
            "max_capture_no_growth_seconds": 2.0,
        },
        "supervisor_crash_recovery": {
            "ffmpeg_parent_death_passed": True,
            "srt_connection_preserved": True,
            "automatic_rtmp_recovery": True,
            "delivery_byte_growth_validated": True,
            "rtsp_capture_pause_diagnostic_only": True,
            "recovery_limit_seconds": 12.0,
            "total_recovery_seconds": 11.96,
            "max_capture_no_growth_seconds": 8.0,
        },
        "repeated_bridge_failure_recovery": {
            "forced_ffmpeg_failures": 3,
            "source_reset_requested": False,
            "source_reset_succeeded": False,
            "srt_session_preserved": True,
            "source_processes_preserved": True,
            "slate_available": True,
            "live_restored_after_each_failure": True,
            "restored_bridge_marker": True,
            "automatic_rtmp_recovery": True,
            "delivery_byte_growth_validated": True,
            "rtsp_capture_pause_diagnostic_only": True,
            "max_capture_no_growth_seconds": 8.0,
        },
        "persistent_input_stall_recovery": {
            "same_srt_connection_confirmed_stalled": True,
            "confirmation_grace_observed": True,
            "stalled_srt_session_observed_before_recovery": True,
            "recovery_path": "exact-api-reset",
            "confirmed_stall_marker_seen": True,
            "source_detached_marker_seen": True,
            "source_reset_succeeded": True,
            "reset_before_transport_idle_timeout": True,
            "reset_elapsed_seconds": 6.25,
            "automatic_srt_recovery": True,
            "srt_session_replaced": True,
            "source_processes_preserved": True,
            "automatic_rtmp_recovery": True,
            "max_capture_no_growth_seconds": 2.0,
        },
        "srt_idle_expiry_seconds": [8.2, 8.3, 8.4],
        "outage_max_capture_no_growth_seconds": [2.0, 2.0, 2.0],
        "final_transition": {"srt_idle_expiry_seconds": 8.2, "max_capture_no_growth_seconds": 2.0},
        "overall_max_capture_no_growth_seconds": 2.0,
        "max_metrics_blind_seconds": 0.2,
        "rtsp_capture_session_preserved": True,
        "event_to_delivery_recovery": [
            {
                "event": event,
                "recovery_seconds": 11.0,
                "publisher_rotated": event == "forced RTMP sink reconnect",
                "known_dts_marker_count": 0,
            }
            for event in events
        ],
        "automatic_rtmp_forward_recovery": {
            "recovery_limit_seconds": 15.0,
            "event_count": 21,
            "maximum_event_to_delivery_seconds": 11.0,
            "max_delivery_outage_seconds": 11.0,
            "id_rotation_count": 1,
            "events_with_publisher_rotation": 1,
            "duplicate_publishers": False,
            "counter_regression": False,
            "invalid_samples": 0,
            "forced_disconnect_recovered": True,
            "final_active": True,
            "forced_disconnect_max_rtsp_capture_no_growth_seconds": 2.0,
            "test_scope": "isolated-loopback-immediate-failure",
        },
        "rtmp_transition_log_contract": {
            "dts_reconnects_recovered": True,
            "missing_or_unset_timestamps_absent": True,
            "mux_invalid_argument_absent": True,
        },
        "strict_sink_segment_capture": {
            "segments": 13,
            "capture_bytes": 100000,
            "pending_validation": 0,
        },
        "strict_sink_segment_validation": {
            "segments": 13,
            "capture_bytes": 100000,
            "video_frames": 1170,
            "audio_frames": 1800,
            "keyframes": 26,
            "temporary_segments_retained": 0,
        },
        "landscape_1280x720_regression_absent": True,
        "legacy_portrait_720x1280_regression_absent": True,
        "test_ports_released": True,
        "workdir_removed": True,
        "secret_configs_wiped": 5,
        "secret_scan_while_live": {
            "unexpected_file_hits": [],
            "journal_hit": False,
            "process_cmdline_hit": False,
            "process_environment_hit": False,
        },
        "secret_scan": {
            "unexpected_file_hits": [],
            "journal_hit": False,
            "process_cmdline_hit": False,
            "process_environment_hit": False,
        },
    }


def test_current_native_report_and_diagnostic_only_local_pauses_pass(
    native_result: dict[str, Any],
) -> None:
    assert smoke.native_self_test_result_failure(native_result) is None
    assert "circuit_breaker_opened" not in native_result["repeated_bridge_failure_recovery"]


@pytest.mark.parametrize("value", [6.0, 6.2, 7.999, 8.0])
def test_early_srt_disconnect_requires_matching_emitted_reset_proof(
    native_result: dict[str, Any],
    value: float,
) -> None:
    native_result["srt_idle_expiry_seconds"][0] = value
    native_result["confirmed_reset_outage_disconnects"] = [
        {"event": "outage 1 SRT idle expiry", "elapsed_seconds": value}
    ]
    assert smoke.native_self_test_result_failure(native_result) is None
    native_result["final_transition"]["srt_idle_expiry_seconds"] = value
    native_result["confirmed_reset_outage_disconnects"].append(
        {"event": "final SRT idle expiry", "elapsed_seconds": value}
    )
    assert smoke.native_self_test_result_failure(native_result) is None


@pytest.mark.parametrize(
    "proof",
    [
        [],
        [{"event": "outage 2 SRT idle expiry", "elapsed_seconds": 6.2}],
        [{"event": "outage 1 SRT idle expiry", "elapsed_seconds": 6.3}],
        [{"event": "outage 1 SRT idle expiry", "elapsed_seconds": 6.2}] * 2,
        [{"event": "outage 1 SRT idle expiry", "elapsed_seconds": 6.2, "extra": True}],
        [{"event": [], "elapsed_seconds": 6.2}],
    ],
)
def test_unproved_stale_malformed_or_duplicate_early_reset_is_rejected(
    native_result: dict[str, Any],
    proof: Any,
) -> None:
    native_result["srt_idle_expiry_seconds"][0] = 6.2
    native_result["confirmed_reset_outage_disconnects"] = proof
    assert smoke.native_self_test_result_failure(native_result) == "native_result_idle"


@pytest.mark.parametrize(
    ("path", "value", "failure"),
    [
        (("status",), "FAIL", "header"),
        (("result_phase",), "pre-cleanup", "header"),
        (("same_session_stall", "srt_connection_preserved"), False, "same_session"),
        (("same_session_stall", "max_capture_no_growth_seconds"), 3.001, "same_session"),
        (("supervisor_crash_recovery", "total_recovery_seconds"), 12.001, "supervisor"),
        (("supervisor_crash_recovery", "delivery_byte_growth_validated"), False, "supervisor"),
        (("repeated_bridge_failure_recovery", "source_reset_requested"), True, "bridge"),
        (("repeated_bridge_failure_recovery", "source_reset_succeeded"), True, "bridge"),
        (("repeated_bridge_failure_recovery", "srt_session_preserved"), False, "bridge"),
        (("repeated_bridge_failure_recovery", "live_restored_after_each_failure"), False, "bridge"),
        (("repeated_bridge_failure_recovery", "forced_ffmpeg_failures"), 2, "bridge"),
        (("persistent_input_stall_recovery", "confirmation_grace_observed"), False, "persistent"),
        (("persistent_input_stall_recovery", "reset_elapsed_seconds"), 5.999, "persistent"),
        (("persistent_input_stall_recovery", "reset_elapsed_seconds"), 9.001, "persistent"),
        (("persistent_input_stall_recovery", "max_capture_no_growth_seconds"), 3.001, "persistent"),
        (("srt_idle_expiry_seconds",), [5.99, 8.2, 8.3], "idle"),
        (("srt_idle_expiry_seconds",), [13.001, 8.2, 8.3], "idle"),
        (("srt_idle_expiry_seconds",), [True, 8.2, 8.3], "idle"),
        (("srt_idle_expiry_seconds",), [float("nan"), 8.2, 8.3], "idle"),
        (("srt_idle_expiry_seconds",), [10**1000, 8.2, 8.3], "idle"),
        (("outage_max_capture_no_growth_seconds",), [2, 3.001, 2], "continuity"),
        (("overall_max_capture_no_growth_seconds",), 3.001, "continuity"),
        (("max_metrics_blind_seconds",), 3.001, "continuity"),
        (("automatic_rtmp_forward_recovery", "recovery_limit_seconds"), 8, "forward"),
        (("automatic_rtmp_forward_recovery", "duplicate_publishers"), True, "forward"),
        (("automatic_rtmp_forward_recovery", "counter_regression"), True, "forward"),
        (("automatic_rtmp_forward_recovery", "invalid_samples"), 1, "forward"),
        (("automatic_rtmp_forward_recovery", "invalid_samples"), False, "forward"),
        (("automatic_rtmp_forward_recovery", "id_rotation_count"), 2, "forward"),
        (("automatic_rtmp_forward_recovery", "max_delivery_outage_seconds"), 15.001, "forward"),
        (("automatic_rtmp_forward_recovery", "final_active"), False, "forward"),
        (("rtmp_transition_log_contract", "mux_invalid_argument_absent"), False, "forward"),
        (("strict_sink_segment_capture", "pending_validation"), 1, "media"),
        (("strict_sink_segment_validation", "segments"), 12, "media"),
        (("strict_sink_segment_validation", "video_frames"), 0, "media"),
        (("strict_sink_segment_validation", "temporary_segments_retained"), 1, "media"),
        (("landscape_1280x720_regression_absent",), False, "media"),
        (("legacy_portrait_720x1280_regression_absent",), False, "media"),
        (("workdir_removed",), False, "cleanup"),
        (("secret_configs_wiped",), 0, "cleanup"),
        (("secret_configs_wiped",), True, "cleanup"),
        (("cleanup_failure",), ["PRIVATE_TEST_REPORT_CONTENT"], "cleanup"),
        (("secret_scan_while_live", "journal_hit"), True, "secrets"),
        (("secret_scan", "unexpected_file_hits"), ["PRIVATE_TEST_REPORT_CONTENT"], "secrets"),
        (("secret_scan", "process_cmdline_hit"), True, "secrets"),
    ],
)
def test_current_native_report_rejects_real_contract_failures(
    native_result: dict[str, Any],
    path: tuple[str, ...],
    value: Any,
    failure: str,
) -> None:
    target = native_result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert smoke.native_self_test_result_failure(native_result) == "native_result_" + failure


def test_forward_rotation_requires_new_event_evidence_and_exact_accounting(
    native_result: dict[str, Any],
) -> None:
    ledger = native_result["event_to_delivery_recovery"]
    ledger[1]["publisher_rotated"] = True
    assert smoke.native_self_test_result_failure(native_result) == "native_result_forward"
    for entry in ledger[1:]:
        entry["known_dts_marker_count"] = 1
    forward = native_result["automatic_rtmp_forward_recovery"]
    forward["id_rotation_count"] = forward["events_with_publisher_rotation"] = 2
    assert smoke.native_self_test_result_failure(native_result) is None
    ledger[2]["publisher_rotated"] = True
    assert smoke.native_self_test_result_failure(native_result) == "native_result_forward"


@pytest.mark.parametrize("mutation", ["late", "negative", "missing", "reordered", "unforced"])
def test_forward_event_ledger_is_complete_ordered_and_bounded(
    native_result: dict[str, Any],
    mutation: str,
) -> None:
    ledger = native_result["event_to_delivery_recovery"]
    if mutation == "late":
        ledger[0]["recovery_seconds"] = 15.001
    elif mutation == "negative":
        ledger[0]["recovery_seconds"] = -0.001
    elif mutation == "missing":
        ledger.pop()
    elif mutation == "reordered":
        ledger[0], ledger[1] = ledger[1], ledger[0]
    else:
        ledger[-2]["publisher_rotated"] = False
    assert smoke.native_self_test_result_failure(native_result) == "native_result_forward"


def test_remote_probe_executes_same_validator_and_never_prints_report(
    native_result: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "PRIVATE_TEST_REPORT_CONTENT"
    payload = copy.deepcopy(native_result)
    payload["unexpected"] = marker

    class Reader(io.BytesIO):
        def fileno(self) -> int:
            return 17

    raw = json.dumps(payload).encode()
    monkeypatch.setattr(os, "open", lambda *_args: 17)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0x20000, raising=False)
    monkeypatch.setattr(os, "O_NONBLOCK", 0x800, raising=False)
    monkeypatch.setattr(os, "fdopen", lambda *_args: Reader(raw))
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=0,
            st_nlink=1,
            st_size=len(raw),
        ),
    )
    source = smoke.native_result_probe_source()
    exec(compile(source, "<fixed-native-result-probe>", "exec"), {})  # noqa: S102
    assert capsys.readouterr().out == "NATIVE_SELF_TEST_RESULT_OK\n"
    payload["status"] = marker
    raw = json.dumps(payload).encode()
    exec(compile(source, "<fixed-native-result-probe>", "exec"), {})  # noqa: S102
    assert capsys.readouterr().out == "native_result_header\n"


@pytest.mark.parametrize(
    "unsafe_metadata",
    [
        {"st_uid": 1000},
        {"st_mode": stat.S_IFREG | 0o644},
        {"st_mode": stat.S_IFDIR | 0o600},
        {"st_nlink": 2},
        {"st_size": 1048577},
        {"st_size": 0},
    ],
)
def test_remote_probe_rejects_unsafe_report_without_reading_contents(
    unsafe_metadata: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Unreadable(io.BytesIO):
        def fileno(self) -> int:
            return 17

        def read(self, *_args: Any) -> bytes:
            raise AssertionError("unsafe report contents must not be read")

    monkeypatch.setattr(os, "O_NOFOLLOW", 0x20000, raising=False)
    monkeypatch.setattr(os, "O_NONBLOCK", 0x800, raising=False)

    def checked_open(path: str, flags: int) -> int:
        assert path == "/var/lib/moblin-relay/tests/last-quick-result.json"
        assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        return 17

    metadata = {"st_mode": stat.S_IFREG | 0o600, "st_uid": 0, "st_nlink": 1, "st_size": 10}
    monkeypatch.setattr(os, "open", checked_open)
    monkeypatch.setattr(os, "fdopen", lambda *_args: Unreadable())
    monkeypatch.setattr(os, "fstat", lambda _fd: SimpleNamespace(**(metadata | unsafe_metadata)))
    exec(  # noqa: S102 - repository-owned probe, mocked protected file
        compile(smoke.native_result_probe_source(), "<fixed-native-result-probe>", "exec"), {}
    )
    assert capsys.readouterr().out == "native_result_unreadable\n"


@pytest.mark.parametrize("failure", [None, "native_result_bridge", "private-not-allowlisted"])
def test_lifecycle_uses_current_release_and_only_safe_result_failure_stages(
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    calls: list[tuple[str, ...]] = []
    stages: list[str] = []

    def fake_compose(*args: str, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "python3" in args:
            output = (failure or "NATIVE_SELF_TEST_RESULT_OK").encode() + b"\n"
        elif "REMOTE_NATIVE_LIFECYCLE_OK" in args[-1]:
            output = b"REMOTE_NATIVE_LIFECYCLE_OK\n"
        else:
            output = b""
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(smoke, "compose", fake_compose)
    monkeypatch.setattr(smoke, "set_smoke_stage", stages.append)
    if failure:
        with pytest.raises(SmokeFailure):
            smoke.verify_remote_lifecycle()
    else:
        smoke.verify_remote_lifecycle()
    assert calls[0][-1] == RELAY_RELEASE
    assert 'test "$(cat /etc/moblin-relay/release)" = "$1"' in calls[0][-3]
    assert stages[0:3] == [
        "remote_lifecycle_files",
        "remote_lifecycle_services",
        "remote_lifecycle_result",
    ]
    assert set(stages) <= smoke.SMOKE_STAGES
    if failure in smoke.NATIVE_RESULT_FAILURES:
        assert stages[-1] == failure
    elif failure:
        assert stages[-1] == "remote_lifecycle_result"
    else:
        assert stages[-1] == "remote_lifecycle_accounts"
