#!/usr/bin/python3
"""Source-level contract tests for the production MediaMTX renderer."""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path, PurePosixPath
from unittest.mock import patch

sys.dont_write_bytecode = True

if os.name != "posix":
    account = types.SimpleNamespace(pw_uid=0, pw_gid=0)
    sys.modules.setdefault("grp", types.SimpleNamespace(getgrnam=lambda _name: account))
    sys.modules.setdefault("pwd", types.SimpleNamespace(getpwnam=lambda _name: account))


def load_renderer():
    path = Path(__file__).with_name("moblin-relay-render-config")
    loader = importlib.machinery.SourceFileLoader("moblin_relay_render_config", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError("unable to load renderer source")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_normalizer():
    path = Path(__file__).with_name("moblin-relay-normalize")
    loader = importlib.machinery.SourceFileLoader("moblin_relay_normalize", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError("unable to load normalizer source")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def assert_normalizer_contract(normalizer) -> None:
    argv = normalizer.build_ffmpeg_argv(18554, 11936)
    joined = " ".join(argv)
    assert argv[0] == "/usr/bin/ffmpeg"
    assert "rtsp://127.0.0.1:18554/iphone-live" in argv
    assert "rtmp://127.0.0.1:11936/relay-output" in argv
    assert "-rw_timeout" not in argv
    assert "-timeout" not in argv
    assert argv.count("-fflags") == 1
    assert argv[argv.index("-fflags") + 1] == "+genpts"
    assert argv.index("-fflags") < argv.index("-i")
    assert "-use_wallclock_as_timestamps" not in argv
    assert "-copyts" not in argv
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-copyinkf" not in argv
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-profile:a") + 1] == "aac_low"
    assert argv[argv.index("-af") + 1] == "aresample=48000:async=1:first_pts=0"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-max_muxing_queue_size") + 1] == "2048"
    assert "-bsf:v" not in argv
    assert "-output_ts_offset" not in argv
    assert "-nostats" in argv
    assert "rtmps://" not in joined
    assert "passphrase=" not in joined
    assert argv.count("-flush_packets") == 1
    assert "-tcp_nodelay" not in argv
    assert argv[-3:] == [
        "-f",
        "flv",
        "rtmp://127.0.0.1:11936/relay-output",
    ]

    source_id = "11111111-2222-3333-4444-555555555555"
    assert normalizer.build_ingest_metrics_path(source_id) == (
        "/metrics?type=srt_conns&srt_conn=" + source_id
    )
    metric = (
        'srt_conns_bytes_received_unique{state="publish",path="iphone-live",'
        f'remoteAddr="198.51.100.10:54321",id="{source_id}"}} 123456\n'
    )
    assert normalizer.parse_ingest_sample(metric, source_id) == (source_id, 123456)
    assert normalizer.parse_ingest_sample(metric + metric, source_id) is None
    rejected_candidate = (
        'srt_conns_bytes_received_unique{state="idle",path="",'
        'remoteAddr="198.51.100.20:54322",'
        'id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"} 999999\n'
    )
    assert normalizer.parse_ingest_sample(metric + rejected_candidate, source_id) == (
        source_id,
        123456,
    )
    assert (
        normalizer.parse_ingest_sample(
            metric.replace('path="iphone-live"', 'path="other"'), source_id
        )
        is None
    )
    assert (
        normalizer.parse_ingest_sample(metric.replace('state="publish"', 'state="idle"'), source_id)
        is None
    )
    assert (
        normalizer.parse_ingest_sample(
            metric.replace("srt_conns_bytes_received_unique", "srt_conns_bytes_received"),
            source_id,
        )
        is None
    )
    output_metric = (
        'rtmp_conns_inbound_bytes{state="publish",path="relay-output",'
        'remoteAddr="127.0.0.1:54321",id="normalizer-id"} 456789\n'
    )
    assert normalizer.parse_output_sample(output_metric) == (
        "normalizer-id",
        456789,
    )
    assert normalizer.parse_output_sample(output_metric + output_metric) is None
    assert (
        normalizer.parse_output_sample(output_metric.replace('path="relay-output"', 'path="other"'))
        is None
    )
    assert (
        normalizer.parse_output_sample(
            output_metric.replace("127.0.0.1:54321", "203.0.113.10:54321")
        )
        is None
    )

    gate = normalizer.GrowthGate()
    assert gate.observe(100) is False
    assert gate.observe(110) is False
    assert gate.observe(110) is False
    assert gate.observe(120) is True
    gate.reset()
    assert gate.observe(120) is False
    assert gate.observe(120) is False

    output_gate = normalizer.ConnectionGrowthGate()
    assert output_gate.observe(("connection-a", 100)) is False
    assert output_gate.observe(("connection-a", 110)) is False
    assert output_gate.observe(("connection-a", 120)) is True
    assert output_gate.observe(("connection-b", 130)) is False

    assert normalizer.VERIFIED_STALL_TIMEOUT_SECONDS == 0.50
    assert normalizer.OUTPUT_IDLE_FALLBACK_SECONDS == 0.90
    assert normalizer.REQUIRED_IDLE_OBSERVATIONS == 2
    assert normalizer.REQUIRED_VERIFIED_STALL_OBSERVATIONS == 3
    assert normalizer.METRICS_BLIND_TIMEOUT_SECONDS == 0.75
    assert (
        normalizer.VERIFIED_STALL_TIMEOUT_SECONDS
        + (2 * normalizer.MEDIA_POLL_INTERVAL_SECONDS)
        + normalizer.CHILD_STOP_GRACE_SECONDS
        + (1024 / 48000)
        < 1.1
    )
    assert (
        normalizer.OUTPUT_IDLE_FALLBACK_SECONDS
        + normalizer.MEDIA_POLL_INTERVAL_SECONDS
        + normalizer.CHILD_STOP_GRACE_SECONDS
        + (1024 / 48000)
        < 1.0
    )

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.49) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.49, 1.491) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.552) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.552, 1.553) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.60) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.60, 1.601) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.61) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.61, 1.611) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    for observed_at, ingest_counter in ((1.05, 500), (1.10, 501), (1.15, 502)):
        assert watchdog.observe_output(True, ("connection-a", 120), observed_at) == (True, True)
        assert (
            watchdog.observe_ingest(
                True,
                ("ingest-a", ingest_counter),
                observed_at,
                observed_at + 0.001,
            )
            is True
        )
    assert watchdog.observe_output(True, ("connection-a", 121), 1.16) == (True, False)

    assert watchdog.observe_output(True, ("connection-a", 121), 1.21) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.21, 1.211) is True
    assert watchdog.observe_output(True, ("connection-a", 121), 1.60) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.60, 1.601) is True
    assert watchdog.observe_output(True, ("connection-a", 121), 1.72) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.72, 1.721) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    for observed_at, ingest_counter in (
        (1.05, 500),
        (1.15, 501),
        (1.25, 502),
        (1.35, 503),
        (1.45, 504),
        (1.55, 505),
        (1.899, 506),
    ):
        assert watchdog.observe_output(True, ("connection-a", 120), observed_at) == (
            True,
            True,
        )
        assert (
            watchdog.observe_ingest(
                True,
                ("ingest-a", ingest_counter),
                observed_at,
                observed_at + 0.001,
            )
            is True
        )
    assert watchdog.observe_output(True, ("connection-a", 120), 1.901) == (False, False)

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.10, 1.30) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.56) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.56, 1.561) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(False, None, 1.10, 1.11) is True
    assert watchdog.ingest_counter is None
    assert watchdog.joint_idle_since is None
    assert watchdog.joint_unchanged_observations == 0
    assert watchdog.observe_output(True, ("connection-a", 120), 1.11) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.11, 1.111) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.55) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.55, 1.551) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.62) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.62, 1.621) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(False, None, 1.10) == (True, False)
    assert watchdog.ingest_counter is None
    assert watchdog.joint_idle_since is None
    assert watchdog.joint_unchanged_observations == 0
    assert watchdog.observe_output(True, ("connection-a", 120), 1.11) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.11, 1.111) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.55) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.55, 1.551) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.62) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.62, 1.621) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, None, 1.05, 1.051) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    blind_limit = normalizer.METRICS_BLIND_TIMEOUT_SECONDS
    assert watchdog.observe_output(False, None, 1.0 + blind_limit - 0.001) == (True, False)
    assert watchdog.observe_output(False, None, 1.0 + blind_limit + 0.001) == (False, False)
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, None, 1.0 + blind_limit - 0.001) == (True, False)
    assert watchdog.observe_output(True, None, 1.0 + blind_limit + 0.001) == (False, False)
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-b", 121), 1.01) == (False, False)
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 119), 1.01) == (False, False)
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 499), 1.10, 1.101) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-b", 501), 1.10, 1.101) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.20, 1.19) is False
    assert hasattr(normalizer, "make_parent_death_setup")
    assert set(normalizer.RESTART_LOG_TOKENS) == {
        "child-exit",
        "output-start-timeout",
        "metrics-blind",
        "output-identity",
        "output-regression",
        "output-fallback",
        "ingest-timing",
        "ingest-missing",
        "ingest-identity",
        "ingest-regression",
        "verified-stall",
        "ingest-confirmed-stall",
        "watchdog-unknown",
    }
    assert all(
        token.startswith("moblin-relay-normalize:restart:")
        and token.replace("moblin-relay-normalize:restart:", "").replace("-", "").isalpha()
        for token in normalizer.RESTART_LOG_TOKENS.values()
    )
    assert set(normalizer.RECOVERY_EVENT_TOKENS) == {
        "threshold-reached",
        "source-kicked",
        "source-gone",
        "credential-unavailable",
        "api-unreachable",
        "api-rejected",
        "api-invalid-response",
        "attempts-exhausted",
        "source-resumed-before-reset",
    }
    assert all(
        re.fullmatch(r"moblin-relay-normalize:recovery:[a-z-]+", token)
        for token in normalizer.RECOVERY_EVENT_TOKENS.values()
    )
    assert normalizer.STATE_EVENT_TOKENS == {
        "source-attached": "moblin-relay-normalize:state:source-attached",
        "source-detached": "moblin-relay-normalize:state:source-detached",
        "bridge-active": "moblin-relay-normalize:state:bridge-active",
    }

    confirmed_stall_grace = normalizer.CONFIRMED_INPUT_STALL_GRACE_SECONDS
    assert confirmed_stall_grace == 6.0
    confirmed_stall = normalizer.ConfirmedInputStallGate(source_id, 500, 1.0)
    assert (
        confirmed_stall.observe(
            True,
            (source_id, 500),
            1.0 + confirmed_stall_grace - 0.001,
        )
        is False
    )
    assert confirmed_stall.observe(True, (source_id, 500), 1.0 + confirmed_stall_grace) is True
    confirmed_stall = normalizer.ConfirmedInputStallGate(source_id, 500, 1.0)
    assert confirmed_stall.observe(False, None, 6.9) is False
    assert confirmed_stall.observe(True, (source_id, 500), 7.0) is False
    assert confirmed_stall.observe(True, (source_id, 501), 8.0) is False
    assert confirmed_stall.observe(True, (source_id, 501), 13.999) is False
    assert confirmed_stall.observe(True, (source_id, 501), 14.0) is False
    assert confirmed_stall.observe(True, (source_id, 501), 14.001) is True

    circuit = normalizer.RecoveryCircuitBreaker(source_id)
    for observed_at in (1.0, 2.0, 3.0):
        assert circuit.record_failure("verified-stall", observed_at) is False
    assert len(circuit.failures) == 0
    assert circuit.record_failure("output-fallback", 3.5) is False
    assert circuit.record_failure("child-exit", 4.0) is False
    assert circuit.record_failure("output-start-timeout", 5.0) is False
    assert circuit.record_failure("output-regression", 6.0) is False
    assert circuit.opened is False
    assert len(circuit.failures) == 0
    assert not normalizer.SOURCE_RESET_ELIGIBLE_REASONS

    circuit = normalizer.RecoveryCircuitBreaker(source_id)
    assert circuit.open_after_confirmed_input_stall(500, 10.0) is True
    assert circuit.reason == "ingest-confirmed-stall"
    assert circuit.should_attempt(10.0) is False
    assert circuit.observe_before_reset(True, (source_id, 500), 10.05) == "wait"
    assert circuit.observe_before_reset(True, (source_id, 500), 10.10) == "ready"
    assert circuit.should_attempt(10.10) is True
    assert circuit.open_after_confirmed_input_stall(500, 11.0) is False
    assert circuit.observe_before_reset(True, (source_id, 501), 10.11) == "resumed"
    assert circuit.cancel_after_source_resumed() is True

    circuit = normalizer.RecoveryCircuitBreaker(source_id)
    for observed_at in (1.0, 2.0, 3.0):
        assert circuit.record_failure("metrics-blind", observed_at) is False
    assert circuit.opened is False
    assert len(circuit.failures) == 0
    assert circuit.record_failure("child-exit", 4.0) is False
    assert circuit.record_failure("child-exit", 35.0) is False
    assert circuit.record_failure("child-exit", 36.0) is False
    assert circuit.record_failure("child-exit", 37.0) is False

    assert_control_api_contract(normalizer, source_id)

    environment = normalizer.sanitized_environment(18554, 11936, 19998, source_id)
    assert environment == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT": "18554",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT": "11936",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT": "19998",
        "MOBLIN_RELAY_INTERNAL_SRT_CONNECTION_ID": source_id,
    }
    test_token_path = PurePosixPath("/var/lib/moblin-relay/tests/.run-contract/control-api.token")
    test_environment = normalizer.sanitized_environment(
        18554,
        11936,
        19998,
        source_id,
        29997,
        test_token_path,
    )
    assert test_environment["MOBLIN_RELAY_INTERNAL_CONTROL_API_PORT"] == "29997"
    assert test_environment["MOBLIN_RELAY_INTERNAL_CONTROL_TOKEN_FILE"] == str(test_token_path)
    for rejected in (
        "/tmp/control-api.token",  # noqa: S108 - deliberately rejected fixture
        "/var/lib/moblin-relay/tests/control-api.token",
        "/var/lib/moblin-relay/tests/.run-a/../control-api.token",
    ):
        try:
            normalizer.validated_control_token_path(rejected)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe control credential path was accepted")


def assert_control_api_contract(normalizer, source_id: str) -> None:
    token_value = b"A" * 43
    issued_tokens: list[bytearray] = []
    requests: list[tuple[str, str, str, int, float]] = []

    class FakeResponse:
        def __init__(self, status: int, payload: bytes = b'{"status":"ok"}') -> None:
            self.status = status
            self.payload = payload

        def read(self, limit: int) -> bytes:
            assert limit == normalizer.CONTROL_API_RESPONSE_LIMIT_BYTES + 1
            return self.payload[:limit]

    class FakeConnection:
        response = FakeResponse(200)

        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            assert body == b""
            assert headers["Connection"] == "close"
            assert headers["Content-Length"] == "0"
            scheme, encoded = headers["Authorization"].split(" ", 1)
            assert scheme == "Basic"
            assert base64.b64decode(encoded) == b"relay-recovery:" + token_value
            requests.append((method, path, self.host, self.port, self.timeout))

        def getresponse(self) -> FakeResponse:
            return self.response

        def close(self) -> None:
            pass

    def issue_token(_path: Path) -> bytearray:
        token = bytearray(token_value)
        issued_tokens.append(token)
        return token

    with (
        patch.object(normalizer, "read_control_token", issue_token),
        patch.object(normalizer.http.client, "HTTPConnection", FakeConnection),
    ):
        result = normalizer.kick_srt_source(
            29997,
            source_id,
            Path("/unused-by-fixture"),
        )
        assert result == "source-kicked"
        assert requests == [
            (
                "POST",
                "/v3/srtconns/kick/" + source_id,
                "127.0.0.1",
                29997,
                normalizer.CONTROL_API_REQUEST_TIMEOUT_SECONDS,
            )
        ]
        assert issued_tokens and not any(issued_tokens[0])

        FakeConnection.response = FakeResponse(404)
        assert normalizer.kick_srt_source(29997, source_id, Path("/unused")) == "source-gone"
        FakeConnection.response = FakeResponse(401)
        assert normalizer.kick_srt_source(29997, source_id, Path("/unused")) == "api-rejected"
        FakeConnection.response = FakeResponse(
            200,
            b"x" * (normalizer.CONTROL_API_RESPONSE_LIMIT_BYTES + 1),
        )
        assert (
            normalizer.kick_srt_source(29997, source_id, Path("/unused")) == "api-invalid-response"
        )

    class FailedConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("fixture endpoint unavailable")

    with (
        patch.object(normalizer, "read_control_token", issue_token),
        patch.object(normalizer.http.client, "HTTPConnection", FailedConnection),
    ):
        assert normalizer.kick_srt_source(29997, source_id, Path("/unused")) == "api-unreachable"
    assert not any(issued_tokens[-1])
    assert token_value.decode("ascii") not in repr(requests)

    with patch.object(
        normalizer,
        "read_control_token",
        side_effect=ValueError("fixture credential unavailable"),
    ):
        assert (
            normalizer.kick_srt_source(29997, source_id, Path("/unused"))
            == "credential-unavailable"
        )
    try:
        normalizer.kick_srt_source(
            29997,
            "invalid/path?query=secret",
            Path("/unused"),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unbound SRT source identity was accepted")


def assert_preview_contract(renderer) -> None:
    token = b"renderer-preview-test-token-0123456789AB"
    control_token = b"C" * 43
    config = renderer.build_runtime_config(
        "test_publisher",
        "publisher-password-0123456789",
        "passphrase-0123456789",
        "rtmps://example.invalid/live2",
        "test-youtube-key",
        token,
        control_token,
    )
    assert config["hls"] is True
    assert config["hlsAddress"] == "127.0.0.1:8888"
    assert config["hlsVariant"] == "mpegts"
    assert config["hlsSegmentDuration"] == "2s"
    assert config["hlsSegmentCount"] == 4
    assert config["hlsSegmentMaxSize"] == "3M"
    assert config["hlsAlwaysRemux"] is False
    assert config["hlsAllowOrigins"] == []
    assert config["hlsTrustedProxies"] == []
    assert config["srt"] is True
    assert config["srtAddress"] == "0.0.0.0:8890"
    assert config["rtsp"] is True
    assert config["rtspAddress"] == "127.0.0.1:8554"
    assert config["rtspTransports"] == ["tcp"]
    assert config["rtmp"] is True
    assert config["rtmpAddress"] == "127.0.0.1:1935"
    for feature in ("playback", "webrtc"):
        assert config[feature] is False
    assert config["api"] is True
    assert config["apiAddress"] == "127.0.0.1:9997"
    assert config["apiEncryption"] is False
    assert config["apiAllowOrigins"] == []
    assert config["apiTrustedProxies"] == []

    ingest = config["paths"]["iphone-live"]
    output = config["paths"]["relay-output"]
    assert ingest["runOnAvailable"] == renderer.NORMALIZER
    assert ingest["runOnAvailableRestart"] is True
    assert "alwaysAvailable" not in ingest
    assert "forward" not in ingest
    assert output["alwaysAvailable"] is True
    assert output["alwaysAvailableFile"] == renderer.SLATE_FILE
    assert output["forward"] == [
        {"dest": "rtmps://example.invalid/live2#test-youtube-key"}
    ]
    assert "runOnAvailable" not in output
    assert set(config["paths"]) == {"iphone-live", "relay-output"}

    users = [item for item in config["authInternalUsers"] if item["user"] == "relay-preview"]
    assert len(users) == 1
    expected_hash = base64.b64encode(hashlib.sha256(token).digest()).decode("ascii")
    assert users[0] == {
        "user": "relay-preview",
        "pass": f"sha256:{expected_hash}",
        "ips": ["127.0.0.1", "::1"],
        "permissions": [{"action": "read", "path": "relay-output"}],
    }
    assert token.decode("ascii") not in json.dumps(config, sort_keys=True)

    recovery_users = [
        item for item in config["authInternalUsers"] if item["user"] == "relay-recovery"
    ]
    assert len(recovery_users) == 1
    control_hash = base64.b64encode(hashlib.sha256(control_token).digest()).decode("ascii")
    assert recovery_users[0] == {
        "user": "relay-recovery",
        "pass": f"sha256:{control_hash}",
        "ips": ["127.0.0.1", "::1"],
        "permissions": [{"action": "api"}],
    }
    assert control_token.decode("ascii") not in json.dumps(config, sort_keys=True)
    anonymous = [item for item in config["authInternalUsers"] if item["user"] == "any"]
    assert len(anonymous) == 1
    assert not any(permission["action"] == "api" for permission in anonymous[0]["permissions"])
    assert {tuple(permission.items()) for permission in anonymous[0]["permissions"]} == {
        (("action", "metrics"),),
        (("action", "read"), ("path", "iphone-live")),
        (("action", "publish"), ("path", "relay-output")),
    }

    generated = renderer.generate_control_token()
    assert isinstance(generated, bytearray)
    assert len(generated) == 43
    assert renderer.CONTROL_TOKEN_PATTERN.fullmatch(generated)
    for index in range(len(generated)):
        generated[index] = 0

    disabled = renderer.build_runtime_config(
        "test_publisher",
        "publisher-password-0123456789",
        "passphrase-0123456789",
        "rtmps://example.invalid/live2",
        "test-youtube-key",
        None,
        control_token,
    )
    assert disabled["hls"] is False
    assert "hlsAddress" not in disabled
    assert not any(item["user"] == "relay-preview" for item in disabled["authInternalUsers"])


def assert_token_reader_contract(renderer) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        renderer.PREVIEW_TOKEN_FILE = Path(temporary) / "missing-preview-reader.token"
        assert renderer.read_preview_token() is None
    if os.name != "posix":
        return
    try:
        account = renderer.pwd.getpwnam("restream-agent")
    except KeyError:
        return
    if os.geteuid() != 0:
        return

    with tempfile.TemporaryDirectory() as temporary:
        parent = Path(temporary) / "adojapan-relay-agent"
        parent.mkdir(mode=0o750)
        parent.chmod(0o750)
        token_file = parent / "preview-reader.token"
        if os.name == "posix":
            os.chown(parent, 0, account.pw_gid)
        renderer.PREVIEW_TOKEN_FILE = token_file

        token = b"renderer-preview-test-token-0123456789AB"
        token_file.write_bytes(token + b"\n")
        token_file.chmod(0o600)
        if os.name == "posix":
            os.chown(token_file, account.pw_uid, account.pw_gid)
        assert renderer.read_preview_token() == token

        # Keep the metadata length valid so this exercises content validation,
        # not the earlier unsafe-file-size gate.
        token_file.write_bytes(b"invalid token with spaces but long enough 123456\n")
        token_file.chmod(0o600)
        if os.name == "posix":
            os.chown(token_file, account.pw_uid, account.pw_gid)
        try:
            renderer.read_preview_token()
        except SystemExit as exc:
            assert str(exc) == "preview reader credential is invalid"
        else:
            raise AssertionError("invalid preview token was accepted")

        token_file.write_bytes(token + b"\n")
        token_file.chmod(0o644)
        if os.name == "posix":
            os.chown(token_file, account.pw_uid, account.pw_gid)
        try:
            renderer.read_preview_token()
        except SystemExit as exc:
            assert str(exc) == "preview reader credential is unsafe"
        else:
            raise AssertionError("unsafe preview token permissions were accepted")


def main() -> int:
    renderer = load_renderer()
    assert_preview_contract(renderer)
    assert_token_reader_contract(renderer)
    assert_normalizer_contract(load_normalizer())
    print("Renderer and normalizer contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
