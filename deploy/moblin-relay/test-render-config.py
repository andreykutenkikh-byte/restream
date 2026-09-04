#!/usr/bin/python3
"""Source-level contract tests for the production MediaMTX renderer."""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

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
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-copyinkf" not in argv
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-profile:a") + 1] == "aac_low"
    assert argv[argv.index("-af") + 1] == "aresample=48000:async=1:first_pts=0"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-max_muxing_queue_size") + 1] == "2048"
    assert "-nostats" in argv
    assert "rtmps://" not in joined
    assert "passphrase=" not in joined
    assert argv.count("-flush_packets") == 1
    assert "-tcp_nodelay" not in argv
    assert argv[-5:] == [
        "-flush_packets",
        "1",
        "-f",
        "flv",
        "rtmp://127.0.0.1:11936/relay-output",
    ]

    metric = 'paths_inbound_bytes{state="ready",name="iphone-live"} 123456\n'
    assert normalizer.parse_inbound_bytes(metric) == 123456
    assert normalizer.parse_inbound_bytes(metric + metric) is None
    assert (
        normalizer.parse_inbound_bytes('paths_inbound_bytes{name="other",state="ready"} 123456\n')
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
    assert gate.observe(120) is True
    gate.reset()
    assert gate.observe(120) is False
    assert gate.observe(120) is False

    output_gate = normalizer.OutputGrowthGate()
    assert output_gate.observe(("connection-a", 100)) is False
    assert output_gate.observe(("connection-a", 110)) is False
    assert output_gate.observe(("connection-a", 120)) is True
    assert output_gate.observe(("connection-b", 130)) is False

    assert normalizer.VERIFIED_STALL_TIMEOUT_SECONDS == 0.075
    assert normalizer.OUTPUT_IDLE_FALLBACK_SECONDS == 0.5
    assert normalizer.REQUIRED_IDLE_OBSERVATIONS == 2
    assert normalizer.METRICS_BLIND_TIMEOUT_SECONDS == 0.75
    assert (
        normalizer.VERIFIED_STALL_TIMEOUT_SECONDS
        + (2 * normalizer.MEDIA_POLL_INTERVAL_SECONDS)
        + normalizer.CHILD_STOP_GRACE_SECONDS
        + (1024 / 48000)
        < 0.25
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
    assert watchdog.observe_ingest(True, 500, 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.10, 1.101) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.15) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.15, 1.152) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    for observed_at, ingest_counter in ((1.05, 500), (1.10, 501), (1.15, 502)):
        assert watchdog.observe_output(True, ("connection-a", 120), observed_at) == (True, True)
        assert (
            watchdog.observe_ingest(
                True,
                ingest_counter,
                observed_at,
                observed_at + 0.001,
            )
            is True
        )
    assert watchdog.observe_output(True, ("connection-a", 121), 1.16) == (True, False)

    assert watchdog.observe_output(True, ("connection-a", 121), 1.21) == (True, True)
    assert watchdog.observe_ingest(True, 502, 1.21, 1.211) is True
    assert watchdog.observe_output(True, ("connection-a", 121), 1.26) == (True, True)
    assert watchdog.observe_ingest(True, 502, 1.26, 1.261) is True
    assert watchdog.observe_output(True, ("connection-a", 121), 1.31) == (True, True)
    assert watchdog.observe_ingest(True, 502, 1.31, 1.311) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    for observed_at, ingest_counter in (
        (1.05, 500),
        (1.15, 501),
        (1.25, 502),
        (1.35, 503),
        (1.45, 504),
        (1.499, 505),
    ):
        assert watchdog.observe_output(True, ("connection-a", 120), observed_at) == (
            True,
            True,
        )
        assert (
            watchdog.observe_ingest(
                True,
                ingest_counter,
                observed_at,
                observed_at + 0.001,
            )
            is True
        )
    assert watchdog.observe_output(True, ("connection-a", 120), 1.501) == (False, False)

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.10, 1.30) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.31) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.31, 1.311) is False

    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_output(True, ("connection-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(False, None, 1.10, 1.11) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.15) == (True, True)
    assert watchdog.observe_ingest(True, 500, 1.15, 1.151) is False
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
    assert watchdog.observe_ingest(True, 500, 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("connection-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, 499, 1.10, 1.101) is False
    watchdog = normalizer.MediaWatchdog(("connection-a", 120), 1.0)
    assert watchdog.observe_ingest(True, 500, 1.20, 1.19) is False
    assert hasattr(normalizer, "make_parent_death_setup")

    environment = normalizer.sanitized_environment(18554, 11936, 19998)
    assert environment == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT": "18554",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT": "11936",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT": "19998",
    }


def assert_preview_contract(renderer) -> None:
    token = b"renderer-preview-test-token-0123456789AB"
    config = renderer.build_runtime_config(
        "test_publisher",
        "publisher-password-0123456789",
        "passphrase-0123456789",
        "rtmps://example.invalid/live2",
        "test-youtube-key",
        token,
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
    for feature in ("api", "playback", "webrtc"):
        assert config[feature] is False

    ingest = config["paths"]["iphone-live"]
    output = config["paths"]["relay-output"]
    assert ingest["runOnAvailable"] == renderer.NORMALIZER
    assert ingest["runOnAvailableRestart"] is True
    assert "alwaysAvailable" not in ingest
    assert "forward" not in ingest
    assert output["alwaysAvailable"] is True
    assert output["alwaysAvailableFile"] == renderer.SLATE_FILE
    assert output["forward"] == [{"dest": "rtmps://example.invalid/live2#test-youtube-key"}]

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

    disabled = renderer.build_runtime_config(
        "test_publisher",
        "publisher-password-0123456789",
        "passphrase-0123456789",
        "rtmps://example.invalid/live2",
        "test-youtube-key",
        None,
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
    print("Renderer and audio normalizer contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
