from __future__ import annotations

import ast
import os
import re
import runpy
import socket
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "moblin-relay"
RELAYCTL = BUNDLE / "relayctl"
SELF_TEST = BUNDLE / "self-test"
NORMALIZER = BUNDLE / "moblin-relay-normalize"


def load_relayctl() -> dict[str, object]:
    fcntl = ModuleType("fcntl")
    fcntl.LOCK_EX = 1  # type: ignore[attr-defined]
    fcntl.LOCK_UN = 2  # type: ignore[attr-defined]
    fcntl.flock = lambda *_args: None  # type: ignore[attr-defined]
    termios = ModuleType("termios")
    termios.ECHO = 1  # type: ignore[attr-defined]
    termios.ECHONL = 2  # type: ignore[attr-defined]
    termios.TCSANOW = 0  # type: ignore[attr-defined]
    termios.tcgetattr = lambda *_args: [0, 0, 0, 0]  # type: ignore[attr-defined]
    termios.tcsetattr = lambda *_args: None  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"fcntl": fcntl, "termios": termios}):
        return runpy.run_path(str(RELAYCTL), run_name="_moblin_relayctl_test")


def load_self_test() -> dict[str, object]:
    fcntl = ModuleType("fcntl")
    resource = ModuleType("resource")
    with (
        patch.dict(sys.modules, {"fcntl": fcntl, "resource": resource}),
        patch.dict(os.environ, {"MOBLIN_RELAY_SELF_TEST_STAGE_FILE": ""}),
    ):
        return runpy.run_path(str(SELF_TEST), run_name="_moblin_self_test")


def load_normalizer() -> dict[str, object]:
    return runpy.run_path(str(NORMALIZER), run_name="_moblin_normalizer_test")


def node_config(
    public_host: str,
    *,
    fallbacks: list[str] | None = None,
    port: int = 8890,
    path: str = "iphone-live",
) -> dict[str, object]:
    return {
        "schema": 1,
        "public_srt_host": public_host,
        "fallback_srt_hosts": list(fallbacks or []),
        "srt_port": port,
        "srt_path": path,
    }


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("203.0.113.25", "203.0.113.25"),
        ("Relay.Example.test", "relay.example.test"),
        ("[2001:0db8::25]", "[2001:db8::25]"),
    ],
)
def test_node_config_accepts_ipv4_dns_and_bracketed_ipv6(host: str, expected: str) -> None:
    namespace = load_relayctl()
    validate = namespace["validate_node_config"]
    assert callable(validate)

    result = validate(node_config(host))

    assert result == {
        "public_srt_host": expected,
        "fallback_srt_hosts": [],
        "srt_port": 8890,
        "srt_path": "iphone-live",
    }


def test_node_config_accepts_optional_canonical_fallback_hosts() -> None:
    namespace = load_relayctl()
    validate = namespace["validate_node_config"]
    assert callable(validate)

    result = validate(
        node_config(
            "relay.example.test",
            fallbacks=["198.51.100.20", "Backup.Example.test", "[2001:db8::20]"],
        )
    )

    assert result["fallback_srt_hosts"] == [
        "198.51.100.20",
        "backup.example.test",
        "[2001:db8::20]",
    ]


@pytest.mark.parametrize(
    "config",
    [
        node_config("relay.example.test") | {"schema": True},
        node_config("2001:db8::20"),
        node_config("srt://relay.example.test"),
        node_config("999.999.999.999"),
        node_config("relay.example.test", port=0),
        node_config("relay.example.test", port=8891),
        node_config("relay.example.test", path="other"),
        node_config("relay.example.test", path="bad:path"),
        node_config("relay.example.test", fallbacks=["relay.example.test"]),
        node_config("relay.example.test", fallbacks=[f"backup{index}.test" for index in range(5)]),
    ],
)
def test_node_config_rejects_ambiguous_host_port_and_path(config: dict[str, object]) -> None:
    namespace = load_relayctl()
    validate = namespace["validate_node_config"]
    assert callable(validate)

    with pytest.raises(ValueError):
        validate(config)


def test_build_moblin_urls_is_silent_and_uses_node_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = load_relayctl()
    build = namespace["build_moblin_urls"]
    assert callable(build)
    globals_ = build.__globals__  # type: ignore[attr-defined]
    globals_["load_secrets"] = lambda: {
        "srt": {
            "user": "publisher",
            "password": "publisher_password_1234",
            "passphrase": "srt_passphrase_1234",
        }
    }
    globals_["load_node_config"] = lambda: {
        "public_srt_host": "[2001:db8::25]",
        "fallback_srt_hosts": ["backup.example.test"],
        "srt_port": 8890,
        "srt_path": "iphone-live",
    }

    result = build()

    assert result == {
        "public_url": (
            "srt://[2001:db8::25]:8890?streamid=publish:iphone-live:publisher:"
            "publisher_password_1234&passphrase=srt_passphrase_1234"
            "&pbkeylen=32&latency=2000&payloadsize=1316"
        ),
        "fallback_urls": [
            "srt://backup.example.test:8890?streamid=publish:iphone-live:publisher:"
            "publisher_password_1234&passphrase=srt_passphrase_1234"
            "&pbkeylen=32&latency=2000&payloadsize=1316"
        ],
    }
    assert "peeridletimeo" not in result["public_url"]
    assert all("peeridletimeo" not in url for url in result["fallback_urls"])
    assert capsys.readouterr() == ("", "")


def test_self_test_source_helper_aligns_peer_idle_budget(tmp_path: Path) -> None:
    loaded = load_self_test()
    write_source_config = loaded["write_source_config"]
    captured: dict[str, object] = {}

    def capture_config(_path: Path, value: dict[str, object], _mode: int = 0o600) -> None:
        captured.update(value)

    globals_ = write_source_config.__globals__  # type: ignore[attr-defined]
    with patch.dict(
        globals_,
        {
            "atomic_json": capture_config,
            "validate_secret_config": lambda *_args: None,
        },
    ):
        write_source_config(
            tmp_path,
            "primary",
            "synthetic_user",
            "synthetic_password_1234",
            "synthetic_passphrase_1234",
            29350,
            29351,
        )

    paths = captured["paths"]
    assert isinstance(paths, dict)
    source = paths[loaded["SOURCE_PATH"]]
    assert isinstance(source, dict)
    forward = source["forward"]
    assert isinstance(forward, list)
    assert forward == [
        {
            "dest": (
                "srt://127.0.0.1:18890?streamid=publish:iphone-live:synthetic_user:"
                "synthetic_password_1234&passphrase=synthetic_passphrase_1234"
                "&pbkeylen=32&latency=2000&payloadsize=1316"
                "&peeridletimeo=10000&conntimeo=3000"
            )
        }
    ]
    helper_idle_seconds = loaded["SOURCE_HELPER_PEER_IDLE_TIMEOUT_MILLISECONDS"] / 1000
    assert loaded["SOURCE_HELPER_PEER_IDLE_TIMEOUT_MILLISECONDS"] == 10_000
    assert (
        loaded["SRT_IDLE_LOWER_BOUND_SECONDS"]
        < helper_idle_seconds
        <= loaded["SRT_IDLE_UPPER_BOUND_SECONDS"]
    )


def test_self_test_primary_live_fixture_uses_backpressured_paced_transport_bridge() -> None:
    loaded = load_self_test()
    live = ROOT / "synthetic-live.mp4"
    ffmpeg = str(loaded["FFMPEG"])
    source_path = loaded["SOURCE_PATH"]
    udp_port = loaded["SOURCE_PRIMARY_FEED_PORT"]
    rtmp_port = loaded["SOURCE_PRIMARY_RTMP_PORT"]

    remux = loaded["local_mpegts_remux_command"](live)
    primary = loaded["local_primary_rtmp_publisher_command"](udp_port, rtmp_port)
    auxiliary = loaded["local_rtmp_publisher_command"](live, rtmp_port)

    assert udp_port == 31937
    assert loaded["LIVE_FEED_FIFO_UNITS"] == 4096
    assert loaded["LIVE_FEED_SOCKET_BUFFER_BYTES"] == 262_144
    assert loaded["SRT_PAYLOAD_SIZE"] == 1316
    assert loaded["LIVE_FEED_BATCH_PAYLOADS"] == 8
    assert loaded["LIVE_FEED_CHUNK_BYTES"] == 8 * loaded["SRT_PAYLOAD_SIZE"]
    assert loaded["LIVE_FEED_CHUNK_BYTES"] % loaded["MPEGTS_PACKET_SIZE_BYTES"] == 0
    assert loaded["LIVE_FEED_CHUNK_BYTES"] <= loaded["MAX_UDP_DATAGRAM_BYTES"]
    assert loaded["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] == 9_000_000
    assert loaded["LIVE_FEED_START_TIMEOUT_SECONDS"] == 5.0
    maximum_buffered_seconds = (
        loaded["LIVE_FEED_FIFO_UNITS"] * loaded["MPEGTS_PACKET_SIZE_BYTES"]
        + 2 * loaded["LIVE_FEED_SOCKET_BUFFER_BYTES"]
    ) / (loaded["LIVE_TRANSPORT_MUX_RATE_BITS_PER_SECOND"] / 8)
    assert maximum_buffered_seconds < loaded["LIVE_TO_SLATE_DEADLINE_SECONDS"] / 2
    assert remux == [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-stream_loop",
        "-1",
        "-i",
        str(live),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-mpegts_flags",
        "+resend_headers",
        "-pat_period",
        "0.1",
        "-muxrate",
        "9000000",
        "-flush_packets",
        "1",
        "-f",
        "mpegts",
        "pipe:1",
    ]
    assert primary == [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-i",
        f"udp://127.0.0.1:{udp_port}?fifo_size=4096&buffer_size=262144",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-f",
        "flv",
        f"rtmp://127.0.0.1:{rtmp_port}/{source_path}",
    ]
    assert "-re" not in primary
    assert "-stream_loop" not in primary
    assert "overrun_nonfatal" not in primary[6]
    assert "+discardcorrupt" not in primary
    assert "-re" not in remux
    assert "-re" in auxiliary
    assert str(live) in auxiliary

    generator = (
        SELF_TEST.read_text(encoding="utf-8")
        .split("def generate_live", 1)[1]
        .split("def video_gop_signature", 1)[0]
    )
    assert "return output" in generator
    assert "-stream_loop" not in generator
    assert "nal-hrd=cbr:force-cfr=1:filler=1:bframes=3:b-pyramid=normal" in generator


def test_self_test_paced_feeder_pauses_without_skipping_or_catching_up() -> None:
    loaded = load_self_test()
    feeder_class = loaded["PacedMPEGTSFeeder"]
    chunk_size = loaded["LIVE_FEED_CHUNK_BYTES"]
    packet_size = loaded["MPEGTS_PACKET_SIZE_BYTES"]
    packets_per_chunk = chunk_size // packet_size
    producer = (
        "import os, time\n"
        "index = 0\n"
        "while True:\n"
        "    packet = bytes((0x47, index % 251)) + bytes((index % 251,)) * 186\n"
        f"    for _ in range({packets_per_chunk}):\n"
        "        os.write(1, packet)\n"
        "        time.sleep(0.002)\n"
        "    index += 1\n"
    )
    command = [sys.executable, "-c", producer]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.5)
        feeder = feeder_class(
            command,
            receiver.getsockname()[1],
            bytes_per_second=chunk_size / 0.2,
        )
        feeder.start()
        try:
            assert feeder.wait_ready(1.0)
            initial_remux_pid = feeder.remux_pid
            assert initial_remux_pid is not None
            observed = [receiver.recvfrom(chunk_size)[0]]
            assert feeder.pause()

            receiver.settimeout(0.05)
            while True:
                try:
                    observed.append(receiver.recvfrom(chunk_size)[0])
                except TimeoutError:
                    break
            observed_indexes = [chunk[1] for chunk in observed]
            assert observed_indexes == list(range(len(observed)))

            time.sleep(0.65)
            with pytest.raises(TimeoutError):
                receiver.recvfrom(chunk_size)

            assert feeder.resume()
            receiver.settimeout(0.25)
            resumed = receiver.recvfrom(chunk_size)[0]
            assert resumed[1] == len(observed)
            receiver.settimeout(0.1)
            with pytest.raises(TimeoutError):
                receiver.recvfrom(chunk_size)
            assert feeder.healthy()
            assert feeder.failure_kind is None
            assert feeder.remux_pid == initial_remux_pid
        finally:
            assert feeder.finish()
        assert not feeder.is_alive()
        assert feeder.remux_pid is None


def test_self_test_paced_feeder_fails_closed_when_remux_exits() -> None:
    loaded = load_self_test()
    feeder_class = loaded["PacedMPEGTSFeeder"]
    chunk_size = loaded["LIVE_FEED_CHUNK_BYTES"]
    packet_size = loaded["MPEGTS_PACKET_SIZE_BYTES"]
    packets_per_chunk = chunk_size // packet_size
    producer = (
        "import os\n"
        "packet = b'\\x47' + b'\\x00' * 187\n"
        f"os.write(1, packet * {packets_per_chunk})\n"
    )
    command = [sys.executable, "-c", producer]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.5)
        feeder = feeder_class(
            command,
            receiver.getsockname()[1],
            bytes_per_second=chunk_size / 0.05,
        )
        feeder.start()
        try:
            assert feeder.wait_ready(1.0)
            assert len(receiver.recvfrom(chunk_size)[0]) == chunk_size
            deadline = time.monotonic() + 1.0
            while feeder.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not feeder.is_alive()
            assert feeder.failure_kind == "remux"
            assert not feeder.healthy()
        finally:
            assert feeder.finish()


def test_self_test_paced_feeder_is_ready_only_after_one_complete_datagram(
    tmp_path: Path,
) -> None:
    loaded = load_self_test()
    feeder_class = loaded["PacedMPEGTSFeeder"]
    chunk_size = loaded["LIVE_FEED_CHUNK_BYTES"]
    packet_size = loaded["MPEGTS_PACKET_SIZE_BYTES"]
    packets_per_chunk = chunk_size // packet_size
    gate = tmp_path / "release-remux"
    producer = (
        "import os, time\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.005)\n"
        "packet = b'\\x47' + b'\\x00' * 187\n"
        f"os.write(1, packet * {packets_per_chunk})\n"
        "time.sleep(30)\n"
    )
    command = [sys.executable, "-c", producer]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1.0)
        feeder = feeder_class(command, receiver.getsockname()[1])
        feeder.start()
        try:
            deadline = time.monotonic() + 1.0
            while feeder.remux_pid is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert feeder.remux_pid is not None
            assert not feeder.wait_ready(0.05)
            gate.touch()
            assert feeder.wait_ready(1.0)
            assert len(receiver.recvfrom(chunk_size)[0]) == chunk_size
            assert feeder.healthy()
        finally:
            assert feeder.finish()


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM behavior only")
def test_self_test_paced_feeder_kills_and_reaps_term_ignoring_remux() -> None:
    loaded = load_self_test()
    feeder_class = loaded["PacedMPEGTSFeeder"]
    packets_per_chunk = loaded["LIVE_FEED_CHUNK_BYTES"] // loaded["MPEGTS_PACKET_SIZE_BYTES"]
    producer = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "packet = b'\\x47' + b'\\x00' * 187\n"
        f"os.write(1, packet * {packets_per_chunk})\n"
        "time.sleep(30)\n"
    )
    command = [sys.executable, "-c", producer]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        feeder = feeder_class(command, receiver.getsockname()[1])
        feeder.start()
        assert feeder.wait_ready(1.0)
        assert feeder.remux_pid is not None
        time.sleep(0.1)
        started = time.monotonic()
        assert feeder.finish(1.5)
        assert time.monotonic() - started < 2.0
        assert not feeder.is_alive()
        assert feeder.remux_pid is None


@pytest.mark.parametrize(
    ("offsets", "expected"),
    [
        ([0.3, 0.1, 0.3], 1),
        ([0.3, 0.1, 0.1, 0.3], 1),
        ([0.3, 0.1, 0.1, 0.1, 0.3], 2),
        ([0.25, 0.250001], 1),
        ([0.3, 0.4, 0.5], 1),
        ([0.3], 1),
    ],
)
def test_self_test_video_offset_cluster_counter_requires_sustained_recovery(
    offsets: list[float],
    expected: int,
) -> None:
    loaded = load_self_test()
    count = loaded["count_video_pts_dts_offset_clusters"]

    assert loaded["VIDEO_REORDER_SETTLE_PACKETS"] == 3
    assert count([(0, offset) for offset in offsets]) == expected


def test_self_test_video_offset_cluster_counter_ignores_audio_packets() -> None:
    loaded = load_self_test()
    count = loaded["count_video_pts_dts_offset_clusters"]

    assert count([(0, 0.3), (0, 0.1), (1, 0.1), (0, 0.1), (0, 0.3)]) == 1


def test_self_test_video_offset_cluster_counter_requires_contiguous_known_packets() -> None:
    loaded = load_self_test()
    count = loaded["count_video_pts_dts_offset_clusters"]

    assert count([(0, 0.3), (0, 0.1), (0, None), (0, 0.1), (0, 0.1), (0, 0.3)]) == 1


def test_self_test_video_offset_cluster_counter_preserves_quick_transition_cap() -> None:
    loaded = load_self_test()
    count = loaded["count_video_pts_dts_offset_clusters"]
    one_incident = [(0, 0.3), (0, 0.1), (0, 0.1), (0, 0.1)]

    assert count(one_incident * 12) == 12
    assert count(one_incident * 12 + [(0, 0.3)]) == 13


def test_self_test_primary_live_feeder_has_strict_lifecycle_guards() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")

    port_guard = source.split("ports = (", 1)[1].split("busy =", 1)[0]
    assert '("udp", SOURCE_PRIMARY_FEED_PORT)' in port_guard

    primary_start = source.split("def start_primary_publisher", 1)[1].split(
        "def start_live_feeder", 1
    )[0]
    assert "local_primary_rtmp_publisher_command(" in primary_start
    assert "assert_clean_process_metadata(command)" in primary_start
    assert "wait_udp_bound(process, SOURCE_PRIMARY_FEED_PORT)" in primary_start

    feeder_start = source.split("def start_live_feeder", 1)[1].split(
        "def stop_primary_srt_source", 1
    )[0]
    assert 'port_is_free("udp", SOURCE_PRIMARY_FEED_PORT)' in feeder_start
    assert "local_mpegts_remux_command(live)" in feeder_start
    assert "assert_clean_process_metadata(command)" in feeder_start
    assert "PacedMPEGTSFeeder(" in feeder_start
    assert "live_feeder.start()" in feeder_start
    assert "live_feeder.wait_ready(LIVE_FEED_START_TIMEOUT_SECONDS)" in feeder_start
    assert "processes.append" not in feeder_start
    assert 'raise TestFailure("local live feeder exited during startup")' in feeder_start

    initial_start = source.split('mark_self_test_stage("auth-source")', 1)[1].split(
        'mark_self_test_stage("live-ingest")', 1
    )[0]
    startup_steps = (
        'mark_self_test_stage("auth-src-help")',
        "primary_helper = start_source_helper(",
        'mark_self_test_stage("auth-src-bind")',
        "publisher = start_primary_publisher()",
        'mark_self_test_stage("auth-src-feed")',
        "feeder = start_live_feeder()",
        'mark_self_test_stage("auth-src-path")',
        "wait_helper_path(",
    )
    startup_positions = [initial_start.index(step) for step in startup_steps]
    assert startup_positions == sorted(startup_positions)
    assert "health_check=require_primary_source_liveness" in initial_start
    initial_ingest = source.split('mark_self_test_stage("live-ingest")', 1)[1].split(
        'mark_self_test_stage("live-normalize")', 1
    )[0]
    assert "health_check=require_primary_source_liveness" in initial_ingest

    liveness = source.split("def require_primary_source_liveness", 1)[1].split(
        "def stop_primary_srt_source", 1
    )[0]
    helper = 'mark_self_test_stage("auth-src-help")'
    publisher = 'mark_self_test_stage("auth-src-bind")'
    feeder = 'mark_self_test_stage("auth-src-feed")'
    assert liveness.index(helper) < liveness.index(publisher) < liveness.index(feeder)

    same_session = source.split('mark_self_test_stage("stall-pre")', 1)[1].split(
        'mark_self_test_stage("stall-ident")', 1
    )[0]
    assert "same_session_upstream_pids = (publisher.pid, primary_helper.pid)" in same_session
    assert "same_session_feeder = feeder" in same_session
    assert "same_session_feeder_ident = feeder.ident" in same_session
    assert "same_session_remux_pid = feeder.remux_pid" in same_session
    assert "if not feeder.pause():" in same_session
    assert "if not feeder.resume():" in same_session
    assert "signal.SIGSTOP" not in same_session
    assert "signal.SIGCONT" not in same_session
    assert "feeder is not same_session_feeder" in same_session
    assert "feeder.ident != same_session_feeder_ident" in same_session
    assert "feeder.remux_pid != same_session_remux_pid" in same_session
    assert "(publisher.pid, primary_helper.pid) != same_session_upstream_pids" in same_session

    outage_loop = source.split("for index, duration in enumerate(durations, start=1):", 1)[1].split(
        'mark_self_test_stage("continuity")', 1
    )[0]
    stop = 'stop_primary_srt_source(f"outage {index}")'
    helper = "primary_helper = start_source_helper("
    publisher = "publisher = start_primary_publisher()"
    feeder = "feeder = start_live_feeder()"
    assert "stopped_feeder = feeder" in outage_loop
    assert "primary source survived outage" in outage_loop
    assert "primary source remained active during outage" in outage_loop
    assert 'not port_is_free("udp", SOURCE_PRIMARY_FEED_PORT)' in outage_loop
    assert outage_loop.index(stop) < outage_loop.index(helper)
    assert outage_loop.index(helper) < outage_loop.index(publisher) < outage_loop.index(feeder)
    assert "feeder is stopped_feeder" in outage_loop
    assert "stopped_feeder.is_alive()" in outage_loop
    assert "stopped_feeder.remux_pid is not None" in outage_loop
    assert "recovered_feeder = feeder" in outage_loop
    assert "recovered_feeder_ident = feeder.ident" in outage_loop
    assert "recovered_remux_pid = feeder.remux_pid" in outage_loop
    assert "feeder is not recovered_feeder" in outage_loop
    assert ".pid" not in "\n".join(line for line in outage_loop.splitlines() if "feeder" in line)

    continuity = source.split('mark_self_test_stage("continuity")', 1)[1].split(
        'mark_self_test_stage("decode")', 1
    )[0]
    assert 'stop_primary_srt_source("final continuity transition")' in continuity
    assert source.index('stop_primary_srt_source("final continuity transition")') < source.index(
        'mark_self_test_stage("secrets")'
    )

    cleanup = source.split("finally:\n        cleanup_errors = []", 1)[1]
    assert (
        cleanup.index("feeder.finish()")
        < cleanup.index("safe_stop(primary_helper, force=True)")
        < cleanup.index("safe_stop(publisher, force=True)")
    )


def test_bundle_contains_only_portable_sources_and_no_instance_manifest() -> None:
    expected = {
        "README.md",
        "initialize-secrets",
        "moblin-relay-normalize",
        "moblin-relay-render-config",
        "moblin-relay.service",
        "node.json.example",
        "relayctl",
        "self-test",
        "slate.txt",
        "test-render-config.py",
    }
    assert {path.name for path in BUNDLE.iterdir()} == expected
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(BUNDLE.iterdir()) if path.is_file()
    )
    assert "176.98.181.225" not in combined
    assert "172.29.172.1" not in combined
    assert "install-manifest.json" not in expected
    assert not any(path.suffix in {".mp4", ".tar", ".gz"} for path in BUNDLE.iterdir())


def test_secret_initializer_is_server_side_atomic_and_refuses_overwrite() -> None:
    source = (BUNDLE / "initialize-secrets").read_text(encoding="utf-8")

    assert "secrets.token_hex" in source
    assert "secrets.token_urlsafe" in source
    assert "os.replace(temporary_path, target)" in source
    assert "os.fsync" in source
    assert "target.lstat()" in source
    assert "refusing to overwrite existing relay secrets" in source


def test_bundle_does_not_mutate_host_network_or_container_runtime() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(BUNDLE.iterdir()) if path.is_file()
    ).lower()

    for forbidden in (
        "iptables ",
        "nft ",
        "ufw ",
        "firewall-cmd",
        "ip route ",
        "ip link ",
        "docker ",
        "sysctl -w",
    ):
        assert forbidden not in combined


def test_bundle_python_sources_parse_as_python_310() -> None:
    for name in (
        "initialize-secrets",
        "moblin-relay-normalize",
        "moblin-relay-render-config",
        "relayctl",
        "self-test",
        "test-render-config.py",
    ):
        path = BUNDLE / name
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def test_self_test_emits_only_root_run_scoped_allowlisted_stages() -> None:
    source = (BUNDLE / "self-test").read_text(encoding="utf-8")
    loaded = load_self_test()

    assert loaded["ASSET_DURATION_SECONDS"] == 12
    assert loaded["LIVE_FIXTURE_DURATION_SECONDS"] >= 5 * loaded["ASSET_DURATION_SECONDS"]
    assert loaded["LIVE_FIXTURE_DURATION_SECONDS"] % loaded["ASSET_DURATION_SECONDS"] == 0
    assert (loaded["LIVE_FIXTURE_DURATION_SECONDS"] * loaded["VIDEO_FPS"]) % loaded[
        "VIDEO_GOP_FRAMES"
    ] == 0
    live_generator = source.split("def generate_live", 1)[1].split("def video_gop_signature", 1)[0]
    assert live_generator.count("LIVE_FIXTURE_DURATION_SECONDS") == 3
    assert "ASSET_DURATION_SECONDS" not in live_generator

    assert 'os.environ.pop("MOBLIN_RELAY_SELF_TEST_STAGE_FILE", "")' in source
    assert 'r"/run/moblin-relay-self-test\\.' in source
    assert "os.O_NOFOLLOW" in source
    assert "os.O_NONBLOCK" in source
    assert "parent_metadata.st_uid != 0" in source
    assert "stat.S_IMODE(parent_metadata.st_mode) & 0o022" in source
    assert "stat.S_ISREG(metadata.st_mode)" in source
    assert "metadata.st_uid != 0" in source
    assert "metadata.st_nlink != 1" in source
    assert "os.fchmod(descriptor, 0o600)" in source
    assert "os.ftruncate(descriptor, 0)" in source
    assert '"dut_metrics_ok": False' in source
    assert 'sample["dut_metrics_ok"] = True' in source
    assert '"sink_metrics_ok": False' in source
    assert 'sample["sink_metrics_ok"] = True' in source
    assert "fresh_after = max(started, not_before)" in source
    assert 'sample.get("t", 0.0) >= fresh_after' in source
    assert "and predicate(sample)" in source
    assert "not_before=transition_started" in source
    assert 'f"&payloadsize={SRT_PAYLOAD_SIZE}"' in source
    assert (
        'f"&peeridletimeo={SOURCE_HELPER_PEER_IDLE_TIMEOUT_MILLISECONDS}&conntimeo=3000"' in source
    )
    assert 'f"&passphrase={passphrase}&pbkeylen=32&latency={SRT_LATENCY_MILLISECONDS}"' in source
    assert (
        loaded["LIVE_TO_SLATE_DEADLINE_SECONDS"] + loaded["SLATE_CAPTURE_GROWTH_TIMEOUT_SECONDS"]
        < loaded["SRT_IDLE_LOWER_BOUND_SECONDS"]
    )
    direct_stages = (
        "startup",
        "assets",
        "topology",
        "auth",
        "auth-source",
        "auth-src-help",
        "auth-src-bind",
        "auth-src-feed",
        "auth-src-path",
        "auth-scan",
        "auth-exclusive",
        "auth-x-core",
        "auth-x-second",
        "auth-x-primary",
        "auth-x-blind",
        "auth-x-attempt",
        "live-ingest",
        "live-normalize",
        "norm-hook",
        "norm-child",
        "norm-publish",
        "norm-flap",
        "stall-slate",
        "stall-pre",
        "stall-pause",
        "stall-switch",
        "stall-capture",
        "stall-resume",
        "stall-live",
        "stall-core",
        "stall-source",
        "stall-id-pre",
        "stall-blind",
        "stall-ident",
        "stall-cont",
        "crash-death",
        "crash-live",
        "crash-cont",
        "outages",
        "outage-slate",
        "outage-normal",
        "outage-hold",
        "outage-live",
        "continuity",
        "decode",
        "streams",
        "format",
        "gop",
        "decoder",
        "frames",
        "timestamps",
        "secrets",
        "cleanup",
    )
    timestamp_diagnostic_stages = (
        "ts-probe-pts",
        "ts-packet-dts",
        "ts-v-offset",
        "ts-v-cluster",
        "ts-v-order",
        "ts-audio-pts",
        "ts-g-vdts",
        "ts-g-adts",
        "ts-g-vpts",
        "ts-g-apts",
        "ts-g-vdec",
        "ts-g-adec",
        "ts-av-sync",
    )
    dynamic_exclusivity_stages = (
        "auth-x-live",
        "auth-x-ingest",
        "auth-x-norm",
        "auth-x-sink",
        "auth-x-bytes",
    )
    dynamic_stall_stages = (
        "stall-i-off",
        "stall-id-rec",
        "stall-i-byte",
        "stall-h-blind",
        "stall-h-path",
        "stall-h-error",
        "stall-h-state",
        "stall-norm",
        "stall-sink",
    )
    for stage in loaded["SELF_TEST_STAGES"]:
        assert len(f"{stage}\n".encode("ascii")) <= 16
    for stage in direct_stages:
        assert f'mark_self_test_stage("{stage}")' in source
    for stage in timestamp_diagnostic_stages:
        assert f'("{stage}",' in source
    for stage in dynamic_exclusivity_stages:
        assert f'return "{stage}"' in source
    for stage in dynamic_stall_stages:
        assert f'return "{stage}"' in source
    assert "stall-ingest" in loaded["SELF_TEST_STAGES"]
    assert "stall-i-id" in loaded["SELF_TEST_STAGES"]
    assert 'return "stall-i-id"' not in source
    assert 'mark_self_test_stage("stall-i-id")' not in source
    assert "mark_self_test_stage(\n                timestamp_failure_stage(" in source

    outage_block = source.split('mark_self_test_stage("outages")', 1)[1].split(
        'mark_self_test_stage("continuity")', 1
    )[0]
    assert outage_block.index('mark_self_test_stage("outage-slate")') < outage_block.index(
        "detached = wait_slate_with_live_srt("
    )
    assert 'stop_primary_srt_source(f"outage {index}")' in outage_block
    assert outage_block.index('stop_primary_srt_source(f"outage {index}")') < outage_block.index(
        "detached = wait_slate_with_live_srt("
    )
    assert outage_block.index("detached = wait_slate_with_live_srt(") < (
        outage_block.index("wait_srt_idle_expiry(")
    )
    assert "wait_slate_capture_growth(" in outage_block
    assert "expected_ingest_ids=initial_ingest_ids" in outage_block
    assert "if not feeder.pause():" in outage_block
    assert "if not feeder.resume():" in outage_block
    assert "signal.SIGSTOP" not in outage_block
    assert "signal.SIGCONT" not in outage_block
    assert "signal.SIGKILL" in outage_block
    same_session_recovery = outage_block.split('"same-session LIVE recovery"', 1)[1].split(
        "expected_ingest_ids=initial_ingest_ids", 1
    )[0]
    assert "SUPERVISOR_RESTART_TIMEOUT_SECONDS" in same_session_recovery
    assert "wait_process_exit(old_child_pid, 1.0)" in outage_block
    assert "maximum_capture_no_growth_seconds(" in outage_block
    assert "primary source helper exited during outage" not in outage_block
    assert outage_block.index('mark_self_test_stage("outage-normal")') < outage_block.index(
        "detached = wait_slate_with_live_srt("
    )
    assert outage_block.index('mark_self_test_stage("outage-hold")') < outage_block.index(
        "while time.monotonic() - outage_started < duration"
    )
    assert outage_block.index('mark_self_test_stage("outage-live")') < outage_block.index(
        "primary_helper = start_source_helper("
    )
    assert outage_block.index("primary_helper = start_source_helper(") < outage_block.index(
        "publisher = start_primary_publisher()"
    )
    assert outage_block.index("publisher = start_primary_publisher()") < outage_block.index(
        "feeder = start_live_feeder()"
    )

    helper_block = source.split("def stop_primary_srt_source", 1)[1].split(
        "def reject_with_helper", 1
    )[0]
    assert (
        helper_block.index("feeder.finish()")
        < helper_block.index("safe_stop(primary_helper, force=True)")
        < helper_block.index("safe_stop(publisher, force=True)")
    )
    assert "feeder = None" in helper_block
    assert "primary_helper = None" in helper_block
    assert "publisher = None" in helper_block
    assert "wait_ports_released(" in helper_block
    assert '("tcp", SOURCE_PRIMARY_RTMP_PORT)' in helper_block
    assert '("udp", SOURCE_PRIMARY_FEED_PORT)' in helper_block
    assert '("tcp", SOURCE_PRIMARY_METRICS_PORT)' in helper_block

    continuity_block = source.split('mark_self_test_stage("continuity")', 1)[1].split(
        'mark_self_test_stage("decode")', 1
    )[0]
    assert 'stop_primary_srt_source("final continuity transition")' in continuity_block
    assert "final_slate = wait_slate_with_live_srt(" in continuity_block
    assert (
        'wait_slate_capture_growth("final SLATE capture growth", final_slate)' in continuity_block
    )
    assert continuity_block.index("final_slate = wait_slate_with_live_srt(") < (
        continuity_block.index("wait_srt_idle_expiry(")
    )
    assert "maximum_capture_no_growth_seconds(final_samples)" in continuity_block

    decode_block = source.split('mark_self_test_stage("decode")', 1)[1].split(
        'mark_self_test_stage("secrets")', 1
    )[0]
    ordered_decode_stages = (
        "streams",
        "format",
        "gop",
        "decoder",
        "frames",
        "timestamps",
    )
    positions = [
        decode_block.index(f'mark_self_test_stage("{stage}")') for stage in ordered_decode_stages
    ]
    assert positions == sorted(positions)
    assert decode_block.index('mark_self_test_stage("streams")') < decode_block.index(
        "normalized_signature = stream_signature(capture, include_gop=False)"
    )
    assert decode_block.index('mark_self_test_stage("format")') < decode_block.index(
        "format_failure = output_format_failure_stage("
    )
    assert decode_block.index('mark_self_test_stage("gop")') < decode_block.index(
        'normalized_signature["video_gop"] = video_gop_signature(capture)'
    )
    assert decode_block.index('mark_self_test_stage("decoder")') < decode_block.index(
        "decode = run(["
    )
    assert decode_block.index('mark_self_test_stage("frames")') < decode_block.index(
        'result["decoded_video_frames"] = analyze_decoded_video_frames(capture)'
    )
    assert decode_block.index('mark_self_test_stage("timestamps")') < decode_block.index(
        'result["decoded_audio_timestamps"] = analyze_decoded_audio_timestamps(capture)'
    )
    assert "capture decode/timestamp validation failed" not in decode_block
    assert '"capture decoder validation failed",' in source
    assert '"capture frame validation failed",' in source
    assert '"capture timestamp validation failed",' in source


def test_self_test_validates_actual_decoded_pts_on_native_flv_capture() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    loaded = load_self_test()
    analyze = loaded["analyze_decoded_video_frames"]
    assert callable(analyze)

    assert 'capture = work / "capture.flv"' in source
    assert 'debug_capture = TEST_ROOT / "debug-capture.flv"' in source
    reader_block = source.split("reader = subprocess.Popen([", 1)[1].split(
        "processes.append(reader)", 1
    )[0]
    assert '"-c", "copy", "-f", "flv", str(capture)' in reader_block
    assert '"-f", "mpegts", str(capture)' not in reader_block

    analyzer_block = source.split("def analyze_decoded_video_frames", 1)[1].split(
        "def analyze_decoded_audio_timestamps", 1
    )[0]
    assert (
        '"frame=width,height,pix_fmt,key_frame,pict_type,decode_error_flags,'
        'pts_time,best_effort_timestamp_time"' in analyzer_block
    )
    assert '("pts_time", presentation_timestamps)' in analyzer_block
    assert '("best_effort_timestamp_time", best_effort_timestamps)' in analyzer_block

    width = loaded["PORTRAIT_WIDTH"]
    height = loaded["PORTRAIT_HEIGHT"]
    frame_prefix = (
        f"width={width}|height={height}|pix_fmt=yuv420p|key_frame=0|"
        "pict_type=P|decode_error_flags=0|"
    )

    class FakeProbe:
        returncode = 0
        stdout = iter(
            [
                frame_prefix + "pts_time=0.000000|best_effort_timestamp_time=0.000000\n",
                frame_prefix + "pts_time=0.033333|best_effort_timestamp_time=0.033333\n",
                frame_prefix + "pts_time=0.020000|best_effort_timestamp_time=0.066667\n",
            ]
        )

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 60
            return "", ""

    with patch("subprocess.Popen", return_value=FakeProbe()) as popen:
        result = analyze(Path("native-downstream.flv"))

    command = popen.call_args.args[0]
    assert any("pts_time,best_effort_timestamp_time" in item for item in command)
    assert result["presentation_timestamp_count"] == result["frame_count"] == 3
    assert not result["strict_presentation_timestamps_monotonic"]
    assert result["maximum_presentation_timestamp_backward_step_seconds"] == pytest.approx(0.013333)
    assert result["strict_best_effort_timestamps_monotonic"]

    timestamp_gate = source.split("timestamp_ok = (", 1)[1].split(
        'result["timestamp_check"] = timestamp_ok', 1
    )[0]
    assert 'decoded_frames["strict_presentation_timestamps_monotonic"]' in timestamp_gate
    assert 'decoded_frames["strict_best_effort_timestamps_monotonic"]' not in timestamp_gate


def test_self_test_format_diagnostics_identify_each_safe_predicate() -> None:
    loaded = load_self_test()
    classify = loaded["output_format_failure_stage"]
    assert callable(classify)

    expected_video = {"profile": "High", "level": 40}
    video = {
        "codec_name": "h264",
        "profile": "High",
        "level": 40,
        "has_b_frames": 2,
        "width": loaded["PORTRAIT_WIDTH"],
        "height": loaded["PORTRAIT_HEIGHT"],
        "pix_fmt": "yuv420p",
        "r_frame_rate": f"{loaded['VIDEO_FPS']}/1",
        "avg_frame_rate": f"{loaded['VIDEO_FPS']}/1",
    }
    audio = {
        "codec_name": "aac",
        "profile": "LC",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    }
    assert classify(video, audio, expected_video) is None

    cases = (
        ("video", "codec_name", "hevc", "fmt-v-codec"),
        ("video", "profile", "Main", "fmt-v-prof"),
        ("video", "level", 41, "fmt-v-level"),
        ("video", "has_b_frames", 0, "fmt-v-bframes"),
        ("video", "width", 720, "fmt-v-size"),
        ("video", "height", 1280, "fmt-v-size"),
        ("video", "pix_fmt", "yuv444p", "fmt-v-pixfmt"),
        ("video", "r_frame_rate", "60/1", "fmt-v-rfps"),
        ("video", "avg_frame_rate", "0/0", "fmt-v-afps"),
        ("audio", "codec_name", "mp3", "fmt-a-codec"),
        ("audio", "profile", "HE-AAC", "fmt-a-prof"),
        ("audio", "sample_rate", "44100", "fmt-a-rate"),
        ("audio", "channels", 1, "fmt-a-chans"),
        ("audio", "channel_layout", "mono", "fmt-a-layout"),
    )
    for target, key, value, expected_stage in cases:
        changed_video = dict(video)
        changed_audio = dict(audio)
        changed = changed_video if target == "video" else changed_audio
        changed[key] = value
        assert classify(changed_video, changed_audio, expected_video) == expected_stage


def test_self_test_publisher_exclusivity_uses_server_proof_and_stable_ids() -> None:
    loaded = load_self_test()
    prove = loaded["publisher_exclusivity_proved"]
    problem = loaded["publisher_exclusivity_sample_problem"]
    outbound_bytes = loaded["helper_forward_outbound_bytes"]
    assert callable(prove)
    assert callable(problem)
    assert callable(outbound_bytes)
    stable_sample = {
        "live": True,
        "ingest_ids": ["primary-ingest"],
        "normalized_ids": ["primary-normalizer"],
        "sink_ids": ["downstream"],
        "ingest_bytes": 120,
    }

    assert prove(
        [stable_sample],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        100,
        4096,
        True,
    )
    assert not prove(
        [stable_sample],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        100,
        0,
        True,
    )
    assert not prove(
        [stable_sample],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        100,
        4096,
        False,
    )

    for field, replacement, expected_problem in (
        ("ingest_ids", ["replacement-ingest"], "auth-x-ingest"),
        ("normalized_ids", ["replacement-normalizer"], "auth-x-norm"),
        ("sink_ids", ["replacement-downstream"], "auth-x-sink"),
        ("live", False, "auth-x-live"),
        ("ingest_bytes", 99, "auth-x-bytes"),
    ):
        changed = dict(stable_sample)
        changed[field] = replacement
        assert (
            problem(
                changed,
                ["primary-ingest"],
                ["primary-normalizer"],
                "downstream",
                100,
            )
            == expected_problem
        )
        assert not prove(
            [changed],
            ["primary-ingest"],
            ["primary-normalizer"],
            "downstream",
            100,
            4096,
            True,
        )

    assert (
        problem(
            dict(stable_sample, live=False, normalized_ids=[]),
            ["primary-ingest"],
            ["primary-normalizer"],
            "downstream",
            100,
        )
        == "auth-x-norm"
    )
    assert (
        problem(
            dict(stable_sample, live=False, ingest_ids=[]),
            ["primary-ingest"],
            ["primary-normalizer"],
            "downstream",
            100,
        )
        == "auth-x-ingest"
    )

    no_progress = dict(stable_sample, ingest_bytes=100)
    assert (
        problem(
            no_progress,
            ["primary-ingest"],
            ["primary-normalizer"],
            "downstream",
            100,
        )
        is None
    )
    assert not prove(
        [no_progress],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        100,
        4096,
        True,
    )

    fresh_baseline = dict(stable_sample, ingest_bytes=500)
    assert not prove(
        [fresh_baseline],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        500,
        4096,
        True,
    )
    assert prove(
        [dict(fresh_baseline, ingest_bytes=501)],
        ["primary-ingest"],
        ["primary-normalizer"],
        "downstream",
        500,
        4096,
        True,
    )
    rebased_attack = dict(
        fresh_baseline,
        normalized_ids=["replacement-before-challenge"],
        ingest_bytes=501,
    )
    assert prove(
        [rebased_attack],
        ["primary-ingest"],
        ["replacement-before-challenge"],
        "downstream",
        500,
        4096,
        True,
    )
    assert (
        problem(
            dict(rebased_attack, normalized_ids=["primary-normalizer"]),
            ["primary-ingest"],
            ["replacement-before-challenge"],
            "downstream",
            500,
        )
        == "auth-x-norm"
    )

    metrics = (
        "# Forward destinations\n"
        'forward_dests{path="source/live",protocol="srt",'
        'state="forwarding"} 1\n'
        'forward_dests_outbound_bytes{path="source/live",protocol="srt",'
        'state="forwarding"} 8192\n'
    )
    with patch.dict(outbound_bytes.__globals__, {"fetch_metrics": lambda _port: metrics}):
        assert outbound_bytes(31998) == 8192
    error_metrics = metrics.replace('state="forwarding"', 'state="error"').replace("8192", "16384")
    with patch.dict(outbound_bytes.__globals__, {"fetch_metrics": lambda _port: error_metrics}):
        assert outbound_bytes(31998) == 16384
    wrong_path_metrics = metrics.replace('path="source/live"', 'path="other"')
    with patch.dict(
        outbound_bytes.__globals__, {"fetch_metrics": lambda _port: wrong_path_metrics}
    ):
        assert outbound_bytes(31998) is None


def test_self_test_same_session_failure_classifier_is_fixed_and_secret_free() -> None:
    loaded = load_self_test()
    parse_helper = loaded["parse_source_helper_status"]
    read_helper = loaded["source_helper_status"]
    classify = loaded["same_session_recovery_failure_stage"]
    assert callable(parse_helper)
    assert callable(read_helper)
    assert callable(classify)

    forwarding_metrics = (
        'paths{name="source/live",state="ready"} 1\n'
        'forward_dests{path="source/live",protocol="srt",state="forwarding"} 1\n'
    )
    healthy_helper = {
        "metrics_ok": True,
        "path_ready": True,
        "forward_state": "forwarding",
    }
    assert parse_helper(forwarding_metrics) == healthy_helper
    assert parse_helper(forwarding_metrics.replace('state="forwarding"', 'state="error"')) == {
        **healthy_helper,
        "forward_state": "error",
    }
    assert parse_helper(forwarding_metrics.replace('state="forwarding"', 'state="ready"')) == {
        **healthy_helper,
        "forward_state": "ready",
    }

    secret = "SRT_SECRET_MUST_NOT_ESCAPE"
    untrusted_metrics = forwarding_metrics.replace('state="forwarding"', f'state="{secret}"')
    sanitized = parse_helper(untrusted_metrics)
    assert sanitized == {**healthy_helper, "forward_state": "unknown"}
    assert secret not in repr(sanitized)
    assert parse_helper(forwarding_metrics + forwarding_metrics) == {
        **healthy_helper,
        "forward_state": "unknown",
    }

    def unavailable_metrics(_port: int) -> str:
        raise OSError("metrics unavailable")

    with patch.dict(read_helper.__globals__, {"fetch_metrics": unavailable_metrics}):
        assert read_helper(31998) == {
            "metrics_ok": False,
            "path_ready": False,
            "forward_state": "unknown",
        }

    def sample(
        timestamp: float,
        ingest_bytes: int | None,
        **updates: object,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "t": timestamp,
            "dut_metrics_ok": True,
            "sink_metrics_ok": True,
            "ingest_live": True,
            "ingest_ids": ["primary-ingest"],
            "ingest_bytes": ingest_bytes,
            "normalized": True,
            "normalized_ids": ["normalizer"],
            "path_ready": True,
            "forward": True,
            "sink_ids": ["downstream"],
        }
        value.update(updates)
        return value

    def stage(
        samples: list[dict[str, object]],
        helper: dict[str, object] | None = None,
    ) -> str:
        return classify(
            samples,
            ["primary-ingest"],
            100,
            1.0,
            "downstream",
            healthy_helper if helper is None else helper,
        )

    assert stage([sample(1.2, 101)], {**healthy_helper, "metrics_ok": False}) == ("stall-h-blind")
    assert stage([sample(1.2, 101)], {**healthy_helper, "path_ready": False}) == ("stall-h-path")
    assert stage([sample(1.2, 101)], {**healthy_helper, "forward_state": "error"}) == (
        "stall-h-error"
    )
    assert stage([sample(1.2, 101)], {**healthy_helper, "forward_state": "ready"}) == (
        "stall-h-state"
    )
    identity_stage = stage([sample(1.2, 101, ingest_ids=[secret])])
    assert identity_stage == "stall-id-rec"
    assert secret not in identity_stage
    assert stage([sample(1.2, 101, ingest_live=False, ingest_ids=[])]) == "stall-i-off"
    assert stage([sample(1.2, 100)]) == "stall-i-byte"
    assert stage([sample(1.2, 101), sample(2.3, 101)]) == "stall-i-byte"
    assert stage([sample(1.2, 101, normalized=False, normalized_ids=[])]) == "stall-norm"
    assert stage([sample(1.2, 101, forward=False)]) == "stall-sink"
    assert stage([sample(1.2, 101)]) == "stall-live"
    assert stage([sample(1.2, None)]) == "stall-blind"
    assert stage([sample(1.2, 101), sample(1.3, 100)]) == "stall-blind"
    assert stage([sample(1.2, 101, ingest_ids=["replacement"]), sample(1.3, 102)]) == (
        "stall-id-rec"
    )


def test_self_test_takes_fresh_same_session_baseline_before_resume() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    block = source.split('mark_self_test_stage("stall-capture")', 1)[1].split(
        'mark_self_test_stage("stall-ident")', 1
    )[0]

    capture_proof = "wait_slate_capture_growth("
    baseline = 'observer.checked_snapshot("same-session resume baseline")'
    byte_baseline = 'resume_ingest_bytes = resume_baseline.get("ingest_bytes")'
    idle_guard = "time.monotonic() - same_session_started >= SRT_IDLE_LOWER_BOUND_SECONDS"
    recovery_start = "recovery_started = time.monotonic()"
    resume = "if not feeder.resume():"
    assert (
        block.index(capture_proof)
        < block.index(baseline)
        < block.index(byte_baseline)
        < block.index(idle_guard)
        < block.index(recovery_start)
        < block.index(resume)
    )
    assert 'resume_baseline.get("ingest_ids") != initial_ingest_ids' in block
    assert 'mark_self_test_stage("stall-id-pre")' in block
    assert "resume_ingest_bytes" in block.split(resume, 1)[1]


def test_self_test_refreshes_exclusivity_baseline_immediately_before_challenge() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    block = source.split('mark_self_test_stage("auth-exclusive")', 1)[1].split(
        'mark_self_test_stage("outages")', 1
    )[0]

    scan = 'result["secret_scan_while_live"] = scan_for_markers('
    helper = 'second_helper = start_source_helper(second_config, "second"'
    baseline = "exclusivity_baseline = wait_healthy_live("
    log_tail = "rejection_log_descriptor, rejection_log_offset = open_validated_log_tail("
    challenge = "second_started = time.monotonic()"
    publisher = "second = start_local_publisher(SOURCE_AUX_RTMP_PORT)"
    assert source.index(scan) < source.index(helper)
    assert (
        block.index(helper)
        < block.index(baseline)
        < block.index(log_tail)
        < block.index(challenge)
        < block.index(publisher)
    )
    assert '"healthy LIVE before second-publisher challenge"' in block
    assert "expected_ingest_ids=initial_ingest_ids" in block

    challenged = block.split(challenge, 1)[1]
    assert "initial_normalized_ids" not in challenged
    assert "initial_ingest_bytes" not in challenged
    assert "exclusivity_normalized_ids" in challenged
    assert "exclusivity_ingest_bytes" in challenged
    assert "exclusivity_transport_bytes" in challenged
    assert 'sample["ingest_bytes"] > exclusivity_ingest_bytes' in challenged
    assert 'sample["ingest_transport_bytes"] > exclusivity_transport_bytes' in challenged
    proof_failure = 'if not result["second_publisher_rejected"]:'
    descriptor_cleanup = "if rejection_log_descriptor is not None:\n                os.close("
    assert challenged.index(proof_failure) < challenged.index(descriptor_cleanup)


def test_self_test_dut_log_marker_is_scoped_to_appended_tail(tmp_path: Path) -> None:
    loaded = load_self_test()
    open_tail = loaded["open_validated_log_tail"]
    contains_marker = loaded["log_tail_contains_marker"]
    classify_restart = loaded["classify_normalizer_restart"]
    wait_restart = loaded["wait_normalizer_restart_stage"]
    failure = loaded["TestFailure"]
    marker = loaded["DUPLICATE_PUBLISHER_LOG_MARKER"]
    maximum_tail = loaded["MAX_REJECTION_LOG_TAIL_BYTES"]
    assert callable(open_tail)
    assert callable(contains_marker)
    assert marker == b"someone is already publishing to path 'iphone-live'"
    log_path = tmp_path / "dut.log"
    other_path = tmp_path / "other.log"
    expected_uid = getattr(os, "geteuid", lambda: 0)()

    with (
        log_path.open("w+b") as writer,
        other_path.open("w+b") as other,
        patch.dict(open_tail.__globals__, {"validate_workdir": lambda path: path.resolve()}),
    ):
        writer.write(marker + b"\n")
        writer.flush()
        descriptor, offset = open_tail(log_path, writer.fileno(), tmp_path, expected_uid)
        try:
            assert not contains_marker(descriptor, offset, marker, expected_uid)
            writer.write(b"prefix " + marker + b" suffix\n")
            writer.flush()
            assert contains_marker(descriptor, offset, marker, expected_uid)
            assert classify_restart(descriptor, offset, expected_uid) == "auth-x-norm"
            writer.write(b"moblin-relay-normalize:restart:output-fallback\n")
            writer.flush()
            assert classify_restart(descriptor, offset, expected_uid) == "auth-n-fallback"
            writer.write(b"moblin-relay-normalize:restart:verified-stall\n")
            writer.flush()
            assert classify_restart(descriptor, offset, expected_uid) == "auth-n-stall"
            assert wait_restart(descriptor, offset, expected_uid, timeout=0) == "auth-n-stall"
        finally:
            os.close(descriptor)

        stages = iter(("auth-x-norm", "auth-n-child"))
        with patch.dict(
            wait_restart.__globals__,
            {"classify_normalizer_restart": lambda *_args: next(stages)},
        ):
            assert wait_restart(-1, 0, expected_uid, timeout=0.1) == "auth-n-child"

        with pytest.raises(failure):
            open_tail(log_path, other.fileno(), tmp_path, expected_uid)

        descriptor, offset = open_tail(log_path, writer.fileno(), tmp_path, expected_uid)
        try:
            writer.truncate(0)
            writer.flush()
            with pytest.raises(failure, match="DUT log changed"):
                contains_marker(descriptor, offset, marker, expected_uid)
        finally:
            os.close(descriptor)

        descriptor, offset = open_tail(log_path, writer.fileno(), tmp_path, expected_uid)
        try:
            writer.truncate(maximum_tail + 1)
            writer.flush()
            with pytest.raises(failure, match="bounded inspection size"):
                contains_marker(descriptor, offset, marker, expected_uid)
        finally:
            os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link semantics only")
def test_self_test_dut_log_reader_rejects_symlink(tmp_path: Path) -> None:
    loaded = load_self_test()
    open_tail = loaded["open_validated_log_tail"]
    failure = loaded["TestFailure"]
    assert callable(open_tail)
    target = tmp_path / "target.log"
    link = tmp_path / "dut.log"
    expected_uid = os.geteuid()
    with target.open("w+b") as writer:
        link.symlink_to(target)
        with (
            patch.dict(open_tail.__globals__, {"validate_workdir": lambda path: path.resolve()}),
            pytest.raises(failure),
        ):
            open_tail(link, writer.fileno(), tmp_path, expected_uid)
        link.unlink()
        hardlink = tmp_path / "hardlink.log"
        os.link(target, hardlink)
        with (
            patch.dict(open_tail.__globals__, {"validate_workdir": lambda path: path.resolve()}),
            pytest.raises(failure),
        ):
            open_tail(target, writer.fileno(), tmp_path, expected_uid)


def test_self_test_distinguishes_unknown_metrics_from_media_outage() -> None:
    loaded = load_self_test()
    complete = loaded["complete_metrics_sample"]
    observer_problem = loaded["observer_health_problem"]
    maximum_blind = loaded["maximum_metrics_blind_seconds"]
    require_coverage = loaded["require_metrics_coverage"]
    maximum_no_growth = loaded["maximum_capture_no_growth_seconds"]
    failure = loaded["TestFailure"]
    blind_limit = loaded["METRICS_BLIND_LIMIT_SECONDS"]

    assert complete({"dut_metrics_ok": True, "sink_metrics_ok": True}) is True
    for dut_ok, sink_ok in ((False, True), (True, False), (False, False)):
        assert complete({"dut_metrics_ok": dut_ok, "sink_metrics_ok": sink_ok}) is False
    assert blind_limit == pytest.approx(3.0)
    assert blind_limit < loaded["LIVE_TO_SLATE_DEADLINE_SECONDS"]

    transient_samples = [
        {"finished": 0.2, "dut_metrics_ok": True, "sink_metrics_ok": True},
        {"finished": 0.4, "dut_metrics_ok": True, "sink_metrics_ok": True},
        {"finished": 1.6, "dut_metrics_ok": False, "sink_metrics_ok": True},
        {"finished": 2.8, "dut_metrics_ok": True, "sink_metrics_ok": True},
        {"finished": 3.0, "dut_metrics_ok": True, "sink_metrics_ok": True},
    ]
    assert maximum_blind(transient_samples, 0.2, 3.0) == pytest.approx(2.4)
    assert require_coverage(transient_samples, 0.2, 3.0, "transient scrape") == (pytest.approx(2.4))

    for sustained_samples, started, finished in (
        ([], 0.0, 3.1),
        ([{"finished": 3.1, "dut_metrics_ok": True, "sink_metrics_ok": True}], 0.0, 3.1),
        (
            [
                {"finished": 0.1, "dut_metrics_ok": True, "sink_metrics_ok": True},
                {"finished": 3.2, "dut_metrics_ok": True, "sink_metrics_ok": True},
            ],
            0.1,
            3.2,
        ),
        ([{"finished": 0.2, "dut_metrics_ok": True, "sink_metrics_ok": True}], 0.2, 3.3),
    ):
        assert maximum_blind(sustained_samples, started, finished) == pytest.approx(3.1)
        with pytest.raises(failure, match="metrics observability was lost"):
            require_coverage(sustained_samples, started, finished, "sustained scrape failure")

    healthy = {"finished": 9.9, "dut_metrics_ok": True, "sink_metrics_ok": True}
    incomplete = {"finished": 10.0, "dut_metrics_ok": False, "sink_metrics_ok": True}
    assert (
        observer_problem([healthy, incomplete], monitoring_started=0.0, now=10.0, alive=True)
        is None
    )
    assert observer_problem([], monitoring_started=0.0, now=3.1, alive=True) == (
        "observer produced no fresh samples"
    )
    assert observer_problem([healthy], monitoring_started=0.0, now=13.0, alive=True) == (
        "observer samples became stale"
    )
    assert observer_problem([healthy], monitoring_started=0.0, now=10.0, alive=False) == (
        "observer stopped"
    )
    sustained_incomplete = {
        "finished": 10.0,
        "dut_metrics_ok": False,
        "sink_metrics_ok": True,
    }
    assert (
        observer_problem(
            [
                {"finished": 6.9, "dut_metrics_ok": True, "sink_metrics_ok": True},
                sustained_incomplete,
            ],
            monitoring_started=0.0,
            now=10.0,
            alive=True,
        )
        == "metrics observability was lost"
    )

    assert maximum_no_growth(
        [
            {"t": 1.0, "capture_size": 100},
            {"t": 1.1, "capture_size": 200},
            {"t": 1.5, "capture_size": 200},
        ]
    ) == pytest.approx(0.4)
    assert maximum_no_growth(
        [
            {"t": 1.0, "capture_size": 100},
            {"t": 3.0, "capture_size": 200},
        ]
    ) == pytest.approx(2.0)

    main_source = SELF_TEST.read_text().split("def main()", 1)[1]
    assert "observer.snapshot()" not in main_source
    assert main_source.count("observer.checked_snapshot(") == 6
    assert 'observer.checked_snapshot("same-session resume baseline")' in main_source
    assert '"same-session LIVE recovery diagnosis"' in main_source
    assert "capture_observer = CaptureObserver(capture)" in main_source
    assert main_source.count("capture_observer.checked_samples_since(") == 5


def test_normalizer_uses_a_secret_free_liveness_supervisor() -> None:
    loaded = load_normalizer()
    self_test = load_self_test()
    build_argv = loaded["build_ffmpeg_argv"]
    parse_ingest_sample = loaded["parse_ingest_sample"]
    parse_output_sample = loaded["parse_output_sample"]
    growth_gate = loaded["GrowthGate"]
    connection_growth_gate = loaded["ConnectionGrowthGate"]
    watchdog_type = loaded["MediaWatchdog"]
    sanitized_environment = loaded["sanitized_environment"]

    assert self_test["SUPERVISOR_RESTART_TIMEOUT_SECONDS"] > (
        loaded["OUTPUT_START_TIMEOUT_SECONDS"] + self_test["GOP_DURATION_SECONDS"] + 1.0
    )

    argv = build_argv(18554, 11936)
    assert "-rw_timeout" not in argv
    assert "-timeout" not in argv
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-copyinkf" not in argv
    assert "rtsp://127.0.0.1:18554/iphone-live" in argv
    assert "rtmp://127.0.0.1:11936/relay-output" in argv
    assert argv.count("-flush_packets") == 1
    assert "-tcp_nodelay" not in argv
    assert argv[-5:] == [
        "-flush_packets",
        "1",
        "-f",
        "flv",
        "rtmp://127.0.0.1:11936/relay-output",
    ]

    source_id = "11111111-2222-3333-4444-555555555555"
    assert loaded["build_ingest_metrics_path"](source_id) == (
        "/metrics?type=srt_conns&srt_conn=" + source_id
    )
    with pytest.raises(ValueError):
        loaded["build_ingest_metrics_path"]("invalid&path=other")
    metric = (
        'srt_conns_bytes_received_unique{remoteAddr="198.51.100.10:54321",'
        f'id="{source_id}",state="publish",path="iphone-live"}} 100\n'
    )
    assert parse_ingest_sample(metric, source_id) == (source_id, 100)
    assert parse_ingest_sample(metric + metric, source_id) is None
    rejected_candidate = (
        'srt_conns_bytes_received_unique{remoteAddr="198.51.100.20:54322",'
        'id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",state="idle",path=""} 999999\n'
    )
    assert parse_ingest_sample(metric + rejected_candidate, source_id) == (source_id, 100)
    assert (
        parse_ingest_sample(metric.replace('path="iphone-live"', 'path="other"'), source_id) is None
    )
    assert parse_ingest_sample(metric.replace('state="publish"', 'state="idle"'), source_id) is None
    assert (
        parse_ingest_sample(
            metric.replace("srt_conns_bytes_received_unique", "srt_conns_bytes_received"),
            source_id,
        )
        is None
    )
    output_metric = (
        'rtmp_conns_inbound_bytes{remoteAddr="127.0.0.1:54321",id="normalizer-a",'
        'state="publish",path="relay-output"} 500\n'
    )
    assert parse_output_sample(output_metric) == ("normalizer-a", 500)
    assert parse_output_sample(output_metric + output_metric) is None
    assert parse_output_sample(output_metric.replace('state="publish"', 'state="read"')) is None
    assert parse_output_sample(output_metric.replace('path="relay-output"', 'path="other"')) is None
    assert (
        parse_output_sample(output_metric.replace("127.0.0.1:54321", "203.0.113.5:54321")) is None
    )

    gate = growth_gate()
    assert gate.observe(100) is False
    assert gate.observe(110) is False
    assert gate.observe(120) is True
    gate.reset()
    assert gate.observe(120) is False
    assert gate.observe(120) is False

    output_gate = connection_growth_gate()
    assert output_gate.observe(("normalizer-a", 100)) is False
    assert output_gate.observe(("normalizer-a", 110)) is False
    assert output_gate.observe(("normalizer-a", 120)) is True
    assert output_gate.observe(("normalizer-b", 130)) is False

    assert loaded["VERIFIED_STALL_TIMEOUT_SECONDS"] == 0.075
    assert loaded["OUTPUT_IDLE_FALLBACK_SECONDS"] == 0.5
    assert loaded["REQUIRED_IDLE_OBSERVATIONS"] == 2
    assert loaded["REQUIRED_VERIFIED_STALL_OBSERVATIONS"] == 3
    assert loaded["METRICS_BLIND_TIMEOUT_SECONDS"] == 0.75
    assert (
        loaded["VERIFIED_STALL_TIMEOUT_SECONDS"]
        + (2 * loaded["MEDIA_POLL_INTERVAL_SECONDS"])
        + loaded["CHILD_STOP_GRACE_SECONDS"]
        + (1024 / 48000)
        < self_test["AUDIO_PRESENTATION_GAP_LIMIT_SECONDS"]
    )
    assert (
        loaded["OUTPUT_IDLE_FALLBACK_SECONDS"]
        + loaded["MEDIA_POLL_INTERVAL_SECONDS"]
        + loaded["CHILD_STOP_GRACE_SECONDS"]
        + (1024 / 48000)
        < self_test["CAPTURE_NO_GROWTH_LIMIT_SECONDS"]
    )

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.10, 1.101) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.15) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.15, 1.152) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.14) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.14, 1.141) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.19) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.19, 1.191) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    for observed_at, ingest_counter in ((1.05, 500), (1.10, 501), (1.15, 502)):
        assert watchdog.observe_output(True, ("normalizer-a", 120), observed_at) == (True, True)
        assert (
            watchdog.observe_ingest(
                True,
                ("ingest-a", ingest_counter),
                observed_at,
                observed_at + 0.001,
            )
            is True
        )
    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.16) == (True, False)

    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.21) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.21, 1.211) is True
    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.26) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.26, 1.261) is True
    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.31) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.31, 1.311) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    for observed_at, ingest_counter in (
        (1.05, 500),
        (1.15, 501),
        (1.25, 502),
        (1.35, 503),
        (1.45, 504),
        (1.499, 505),
    ):
        assert watchdog.observe_output(True, ("normalizer-a", 120), observed_at) == (
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
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.501) == (False, False)

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.10, 1.30) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.31) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.31, 1.311) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(False, None, 1.10, 1.11) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.15) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.15, 1.151) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.20) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.20, 1.201) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, None, 1.05, 1.051) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(False, None, 1.749) == (True, False)
    assert watchdog.observe_output(False, None, 1.751) == (False, False)
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, None, 1.749) == (True, False)
    assert watchdog.observe_output(True, None, 1.751) == (False, False)
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-b", 121), 1.01) == (False, False)
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 119), 1.01) == (False, False)
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 499), 1.10, 1.101) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-b", 501), 1.10, 1.101) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.20, 1.19) is False
    assert "make_parent_death_setup" in loaded

    supervisor_source = (
        NORMALIZER.read_text(encoding="utf-8")
        .split("def run_supervisor", 1)[1]
        .split("def main", 1)[0]
    )
    assert supervisor_source.count("ingest_reader.sample()") == 2
    assert "keep_child, probe_ingest = watchdog.observe_output(" in supervisor_source
    assert "if keep_child and probe_ingest:" in supervisor_source
    assert "ingest_started = time.monotonic()" in supervisor_source
    assert "ingest_finished = time.monotonic()" in supervisor_source
    assert "keep_child = watchdog.observe_ingest(" in supervisor_source
    assert (
        "watchdog = MediaWatchdog(counter, now)\n                    ingest_reader.close()"
        in supervisor_source
    )
    assert "if not probe_ingest:\n                ingest_reader.close()" in supervisor_source
    assert "if not keep_child:\n                emit_restart_reason(" in supervisor_source
    assert "watchdog.failure_reason or RESTART_REASON_WATCHDOG_UNKNOWN" in supervisor_source

    assert sanitized_environment(18554, 11936, 19998, source_id) == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT": "18554",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT": "11936",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT": "19998",
        "MOBLIN_RELAY_INTERNAL_SRT_CONNECTION_ID": source_id,
    }


def test_normalizer_restart_diagnostics_are_fixed_and_secret_free(capsys) -> None:
    loaded = load_normalizer()
    tokens = loaded["RESTART_LOG_TOKENS"]
    emit = loaded["emit_restart_reason"]

    assert len(tokens) == 12
    assert all(
        re.fullmatch(r"moblin-relay-normalize:restart:[a-z-]+", token) for token in tokens.values()
    )
    for reason, token in tokens.items():
        emit(reason)
        assert capsys.readouterr().err == token + "\n"

    with pytest.raises(ValueError, match="invalid normalizer restart reason"):
        emit("untrusted-value")
    assert capsys.readouterr().err == ""


def test_normalizer_watchdog_records_a_fixed_failure_reason() -> None:
    loaded = load_normalizer()
    watchdog_type = loaded["MediaWatchdog"]

    cases: list[tuple[str, object]] = []

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(False, None, 1.751) == (False, False)
    cases.append(("metrics-blind", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-b", 121), 1.01) == (False, False)
    cases.append(("output-identity", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 119), 1.01) == (False, False)
    cases.append(("output-regression", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.501) == (False, False)
    cases.append(("output-fallback", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.20, 1.19) is False
    cases.append(("ingest-timing", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, None, 1.05, 1.051) is False
    cases.append(("ingest-missing", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_ingest(True, ("ingest-b", 501), 1.10, 1.101) is False
    cases.append(("ingest-identity", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_ingest(True, ("ingest-a", 499), 1.10, 1.101) is False
    cases.append(("ingest-regression", watchdog))

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.10, 1.101) is True
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.15, 1.151) is False
    cases.append(("verified-stall", watchdog))

    for expected, watchdog in cases:
        assert watchdog.failure_reason == expected


def test_normalizer_reexec_discards_hook_secrets() -> None:
    loaded = load_normalizer()
    main = loaded["main"]
    captured: dict[str, object] = {}
    marker = "must-not-survive-reexec"
    source_id = "11111111-2222-3333-4444-555555555555"

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, environment=environment)
        raise OSError("stop before exec")

    hook_environment = {
        "MTX_PATH": "iphone-live",
        "MTX_QUERY": f"publisher={marker}",
        "MTX_SOURCE_TYPE": "srtConn",
        "MTX_SOURCE_ID": source_id,
        "RTSP_PORT": "18554",
        "MOBLIN_RELAY_OUTPUT_RTMP_PORT": "11936",
        "MOBLIN_RELAY_METRICS_PORT": "19998",
    }
    with (
        patch.dict(os.environ, hook_environment, clear=True),
        patch.object(os, "execve", fake_execve),
        patch.object(sys, "argv", [str(NORMALIZER)]),
    ):
        assert main() == 1

    assert captured["path"] == "/usr/bin/python3"
    assert marker not in repr(captured)
    assert captured["environment"] == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT": "18554",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT": "11936",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT": "19998",
        "MOBLIN_RELAY_INTERNAL_SRT_CONNECTION_ID": source_id,
    }


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    [
        ("rtmpConn", "11111111-2222-3333-4444-555555555555"),
        ("srtConn", "not-a-source-id"),
        ("srtConn", ""),
    ],
)
def test_normalizer_reexec_rejects_an_unbound_source(
    source_type: str,
    source_id: str,
) -> None:
    loaded = load_normalizer()
    hook_environment = {
        "MTX_PATH": "iphone-live",
        "MTX_SOURCE_TYPE": source_type,
        "MTX_SOURCE_ID": source_id,
        "RTSP_PORT": "18554",
        "MOBLIN_RELAY_OUTPUT_RTMP_PORT": "11936",
        "MOBLIN_RELAY_METRICS_PORT": "19998",
    }
    with (
        patch.dict(os.environ, hook_environment, clear=True),
        patch.object(sys, "argv", [str(NORMALIZER)]),
    ):
        assert loaded["main"]() == 2


@pytest.mark.parametrize(
    ("scope", "field", "value", "expected"),
    [
        ("timestamps", "ffprobe_exit", 1, "ts-probe-pts"),
        ("timestamps", "dts_within_tolerance", False, "ts-packet-dts"),
        ("timestamps", "max_pts_dts_offset_seconds", {0: 3.0, 1: 0.01}, "ts-v-offset"),
        (
            "timestamps",
            "video_pts_dts_offset_clusters_over_normal_reorder",
            7,
            "ts-v-cluster",
        ),
        (
            "decoded_frames",
            "strict_presentation_timestamps_monotonic",
            False,
            "ts-v-order",
        ),
        (
            "decoded_audio",
            "presentation_timestamp_steps_beyond_tolerance",
            1,
            "ts-audio-pts",
        ),
        ("timestamps", "max_dts_gap_seconds", {0: 3.0, 1: 0.02}, "ts-g-vdts"),
        ("timestamps", "max_dts_gap_seconds", {0: 0.04, 1: 0.3}, "ts-g-adts"),
        ("timestamps", "max_sorted_pts_gap_seconds", {0: 3.0, 1: 0.02}, "ts-g-vpts"),
        ("timestamps", "max_sorted_pts_gap_seconds", {0: 0.04, 1: 0.3}, "ts-g-apts"),
        (
            "decoded_frames",
            "maximum_presentation_timestamp_gap_seconds",
            3.0,
            "ts-g-vdec",
        ),
        (
            "decoded_audio",
            "maximum_presentation_timestamp_gap_seconds",
            0.3,
            "ts-g-adec",
        ),
        ("timestamps", "audio_video_end_difference_seconds", 0.3, "ts-av-sync"),
    ],
)
def test_timestamp_failure_stage_is_bounded_and_category_specific(
    scope: str,
    field: str,
    value: object,
    expected: str,
) -> None:
    loaded = load_self_test()
    classify = loaded["timestamp_failure_stage"]
    timestamps = {
        "pts_present_for_every_packet": True,
        "ffprobe_exit": 0,
        "stderr_empty": True,
        "dts_within_tolerance": True,
        "negative_dts_steps": {},
        "dts_backward_events_beyond_tolerance": {},
        "max_pts_dts_offset_seconds": {0: 0.1, 1: 0.01},
        "video_pts_dts_offset_clusters_over_normal_reorder": 0,
        "max_dts_gap_seconds": {0: 0.04, 1: 0.02},
        "max_sorted_pts_gap_seconds": {0: 0.04, 1: 0.02},
        "audio_video_duration_difference_seconds": 0.01,
        "audio_video_end_difference_seconds": 0.01,
    }
    decoded_frames = {
        "frame_count": 10,
        "presentation_timestamp_count": 10,
        "strict_presentation_timestamps_monotonic": True,
        "maximum_presentation_timestamp_gap_seconds": 0.04,
    }
    decoded_audio = {
        "ffprobe_exit": 0,
        "frame_count": 10,
        "presentation_timestamp_count": 10,
        "presentation_timestamp_steps_beyond_tolerance": 0,
        "negative_presentation_timestamp_steps": 0,
        "maximum_presentation_timestamp_gap_seconds": 0.02,
        "stderr_empty": True,
    }
    values = {
        "timestamps": timestamps,
        "decoded_frames": decoded_frames,
        "decoded_audio": decoded_audio,
    }
    values[scope][field] = value

    stage = classify(timestamps, decoded_frames, decoded_audio, 6)

    assert stage == expected
    assert stage.isascii()
    assert len(f"{stage}\n".encode("ascii")) <= 16
