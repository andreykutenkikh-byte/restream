from __future__ import annotations

import ast
import json
import os
import re
import runpy
import signal
import socket
import stat
import subprocess
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


def journal_record(timestamp: int, message: str) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(timestamp), "MESSAGE": message})


def test_relayctl_log_redaction_removes_secrets_and_remote_locators() -> None:
    namespace = load_relayctl()
    redact = namespace["redact_log"]
    assert callable(redact)
    raw = (
        "publish rtmps://a.rtmp.youtube.com/live2#top-secret-key "
        "from srt://203.0.113.7:8890?streamid=publish:iphone-live:user:publisher-password"
        "&passphrase=srt-passphrase peer 198.51.100.8:5000 "
        "v6 [2001:db8::5]:443 dns edge.example.net:443 local 127.0.0.1:1935 "
        "orphan exact-secret"
    )

    result = redact(raw, ["exact-secret"])

    for forbidden in (
        "top-secret-key",
        "publisher-password",
        "srt-passphrase",
        "203.0.113.7",
        "198.51.100.8",
        "2001:db8::5",
        "a.rtmp.youtube.com",
        "edge.example.net",
        "exact-secret",
    ):
        assert forbidden not in result
    assert "127.0.0.1:1935" in result
    assert "[REDACTED_URL]" in result
    assert "[REMOTE_ADDRESS]" in result


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "moblin-relay-normalize:restart:verified-stall",
            (
                "normalizer",
                "NORMALIZER_VERIFIED_STALL",
                "SRT input and relay-output both stopped advancing",
            ),
        ),
        (
            "moblin-relay-normalize:restart:ingest-missing",
            (
                "SRT input",
                "SRT_INPUT_DISAPPEARED",
                "The active SRT input disappeared during recovery",
            ),
        ),
        (
            "moblin-relay-normalize:recovery:reset-succeeded",
            (
                "SRT input",
                "SRT_RECOVERY_RESET_SUCCEEDED",
                "The server closed the old SRT transport so Moblin can reconnect automatically",
            ),
        ),
        (
            "moblin-relay-normalize:state:bridge-active",
            (
                "normalizer",
                "NORMALIZER_BRIDGE_ACTIVE",
                "LIVE media is advancing through the normalizer",
            ),
        ),
        (
            "[SRT] path iphone-live closed: secret remote failure",
            ("SRT input", "SRT_INPUT_LOST", "The Moblin SRT input was lost"),
        ),
        (
            "[h264] non-existing PPS 0 referenced; decode_slice_header error; no frame!",
            (
                "SRT input",
                "SRT_INPUT_CORRUPT_MEDIA",
                "The SRT input contained damaged media packets",
            ),
        ),
        (
            "[mpegts] invalid PES packet size",
            (
                "SRT input",
                "SRT_INPUT_CORRUPT_MEDIA",
                "The SRT input contained damaged media packets",
            ),
        ),
        (
            "[path relay-output] [forward rtmps://remote/live2#secret] closed: timeout",
            (
                "YouTube-forward",
                "YOUTUBE_FORWARD_INTERRUPTED",
                "The YouTube forward was interrupted; MediaMTX retries automatically",
            ),
        ),
        (
            "av_interleaved_write_frame(): Broken pipe remote=203.0.113.9",
            (
                "normalizer",
                "NORMALIZER_OUTPUT_BROKEN_PIPE",
                "FFmpeg lost its local relay-output connection",
            ),
        ),
        (
            "[flv @ 0x1234] Packet is missing PTS secret=stream-key remote=203.0.113.9",
            (
                "normalizer",
                "NORMALIZER_TIMESTAMP_MISSING",
                "FFmpeg received media without required presentation timestamps",
            ),
        ),
        (
            "Timestamps are unset in a packet for stream 0 secret=stream-key",
            (
                "normalizer",
                "NORMALIZER_TIMESTAMP_MISSING",
                "FFmpeg received media without required presentation timestamps",
            ),
        ),
        (
            "[path relay-output] [RTMP dest 0 deadbeef] "
            "DTS is not monotonically increasing, was 4167930, now is 4165320",
            (
                "YouTube-forward",
                "DELIVERY_TIMESTAMP_REGRESSION",
                "The server detected a video timestamp discontinuity; "
                "forwarding retries automatically",
            ),
        ),
        (
            "[path relay-output] publisher is publishing and ready",
            (
                "relay-output",
                "RELAY_OUTPUT_ACTIVE",
                "The normalized relay output became available",
            ),
        ),
    ],
)
def test_relayctl_incident_classification_is_fixed_and_secret_free(
    message: str,
    expected: tuple[str, str, str],
) -> None:
    namespace = load_relayctl()
    classify = namespace["classify_incident"]
    assert callable(classify)

    result = classify(message)

    assert result == expected
    assert result is not None
    combined = " ".join(result)
    assert "secret" not in combined
    assert "203.0.113.9" not in combined


def test_relayctl_incident_parser_sorts_and_collapses_ffmpeg_bursts() -> None:
    namespace = load_relayctl()
    parse = namespace["parse_incident_journal"]
    collapse = namespace["collapse_incidents"]
    assert callable(parse)
    assert callable(collapse)
    payload = "\n".join(
        (
            "not-json",
            journal_record(3_000_000, "Broken pipe secret=third"),
            journal_record(1_000_000, "Broken pipe secret=first"),
            journal_record(2_000_000, "Broken pipe secret=second"),
            journal_record(4_000_000, "unrelated harmless message"),
        )
    )

    result = collapse(parse(payload))

    assert result == [
        (
            1_000_000,
            3_000_000,
            "normalizer",
            "NORMALIZER_OUTPUT_BROKEN_PIPE",
            "FFmpeg lost its local relay-output connection",
            3,
        )
    ]
    assert "secret" not in repr(result)


def test_relayctl_incident_parser_collapses_interleaved_errors_and_repeat_summary() -> None:
    namespace = load_relayctl()
    parse = namespace["parse_incident_journal"]
    collapse = namespace["collapse_incidents"]
    payload = "\n".join(
        (
            journal_record(1_000_000, "[h264] non-existing PPS 0 referenced"),
            journal_record(2_000_000, "Too many packets buffered for output stream 0:0"),
            journal_record(3_000_000, "[mpegts] invalid PES packet size"),
            journal_record(4_000_000, "Last message repeated 7 times"),
            journal_record(5_000_000, "Too many packets buffered for output stream 0:0"),
        )
    )

    result = collapse(parse(payload))

    assert [event[3] for event in result] == [
        "SRT_INPUT_CORRUPT_MEDIA",
        "NORMALIZER_MUX_QUEUE_FULL",
    ]
    assert [event[5] for event in result] == [9, 2]


def test_relayctl_incidents_preserve_recovery_chronology_and_episode_boundaries() -> None:
    namespace = load_relayctl()
    parse = namespace["parse_incident_journal"]
    collapse = namespace["collapse_incidents"]
    payload = "\n".join(
        (
            journal_record(1_000_000, "moblin-relay-normalize:state:bridge-active"),
            journal_record(2_000_000, "[h264] non-existing PPS 0 referenced"),
            journal_record(3_000_000, "moblin-relay-normalize:recovery:reset-succeeded"),
            journal_record(4_000_000, "moblin-relay-normalize:state:bridge-active"),
            journal_record(5_000_000, "[mpegts] invalid PES packet size"),
        )
    )

    result = collapse(parse(payload))

    assert [event[3] for event in result] == [
        "NORMALIZER_BRIDGE_ACTIVE",
        "SRT_INPUT_CORRUPT_MEDIA",
        "SRT_RECOVERY_RESET_SUCCEEDED",
        "NORMALIZER_BRIDGE_ACTIVE",
        "SRT_INPUT_CORRUPT_MEDIA",
    ]
    assert [event[0] for event in result] == [
        1_000_000,
        2_000_000,
        3_000_000,
        4_000_000,
        5_000_000,
    ]


def test_relayctl_incident_classifier_drops_unknown_raw_messages() -> None:
    namespace = load_relayctl()
    classify = namespace["classify_incident"]
    parse = namespace["parse_incident_journal"]
    assert callable(classify)
    assert callable(parse)
    raw = (
        "raw-unknown-marker srt://203.0.113.90:8890?"
        "streamid=publish:iphone-live:user:password&passphrase=secret"
    )

    assert classify(raw) is None
    assert parse(journal_record(1_000_000, raw)) == []


def test_relayctl_incidents_uses_bounded_read_and_never_echoes_journal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = load_relayctl()
    command = namespace["cmd_incidents"]
    assert callable(command)
    calls: list[list[str]] = []
    payload = "\n".join(
        (
            journal_record(
                1_700_000_000_000_000,
                "[path relay-output] [forward rtmps://edge.example/live2#secret-key] closed",
            ),
            journal_record(1_700_000_001_000_000, "Broken pipe 203.0.113.50 secret-key"),
            journal_record(1_700_000_002_000_000, "Broken pipe 203.0.113.50 secret-key"),
            journal_record(
                1_700_000_003_000_000,
                "raw-unknown-marker srt://203.0.113.90:8890?"
                "streamid=publish:iphone-live:user:publisher-password"
                "&passphrase=srt-passphrase",
            ),
        )
    )

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, payload, "")

    command.__globals__["run_quiet"] = fake_run  # type: ignore[attr-defined]

    assert command() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "YOUTUBE_FORWARD_INTERRUPTED" in captured.out
    assert "NORMALIZER_OUTPUT_BROKEN_PIPE" in captured.out
    assert "2 occurrences" in captured.out
    for forbidden in (
        "secret-key",
        "203.0.113.50",
        "edge.example",
        "raw-unknown-marker",
        "203.0.113.90",
        "publisher-password",
        "srt-passphrase",
        "streamid=",
    ):
        assert forbidden not in captured.out
    assert calls == [
        [
            "journalctl",
            "--unit",
            "moblin-relay.service",
            "--since",
            "-6h",
            "--lines",
            "4000",
            "--no-pager",
            "--output",
            "json",
            "--utc",
            "--quiet",
        ]
    ]


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
    assert "nal-hrd=cbr:force-cfr=1:filler=1:bframes=0" in generator


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


def test_self_test_video_offset_cluster_counter_reports_diagnostic_runs() -> None:
    loaded = load_self_test()
    count = loaded["count_video_pts_dts_offset_clusters"]
    one_incident = [(0, 0.3), (0, 0.1), (0, 0.1), (0, 0.1)]

    assert "not source-switch validation" in count.__doc__
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


def test_self_test_normalizer_identity_is_exact_and_excludes_production(
    tmp_path: Path,
) -> None:
    loaded = load_self_test()
    matches = loaded["matching_test_normalizer_supervisor_ids"]
    work = tmp_path / ".run-current"
    work.mkdir()
    token_path = work / "control-api.token"
    token_path.write_bytes(b"x" * 43 + b"\n")
    token_path.chmod(0o600)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    source_id = b"11111111-2222-3333-4444-555555555555"
    command = (
        b"\0".join(
            (
                b"/usr/bin/python3",
                b"-I",
                b"/opt/moblin-relay/libexec/moblin-relay-normalize",
                b"--sanitized-supervisor",
            )
        )
        + b"\0"
    )

    exact_environment = {
        b"LANG": b"C",
        b"LC_ALL": b"C",
        b"PATH": b"/usr/bin:/bin",
        b"MOBLIN_RELAY_INTERNAL_RTSP_PORT": b"18554",
        b"MOBLIN_RELAY_INTERNAL_RTMP_PORT": b"11936",
        b"MOBLIN_RELAY_INTERNAL_METRICS_PORT": b"19998",
        b"MOBLIN_RELAY_INTERNAL_CONTROL_API_PORT": b"19997",
        b"MOBLIN_RELAY_INTERNAL_CONTROL_TOKEN_FILE": os.fsencode(str(token_path.resolve())),
        b"MOBLIN_RELAY_INTERNAL_SRT_CONNECTION_ID": source_id,
    }

    def write_process(pid: int, environment: list[tuple[bytes, bytes]]) -> None:
        process = proc_root / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(command)
        (process / "environ").write_bytes(
            b"\0".join(key + b"=" + value for key, value in environment) + b"\0"
        )

    write_process(101, list(exact_environment.items()))
    production_environment = dict(exact_environment)
    production_environment.update(
        {
            b"MOBLIN_RELAY_INTERNAL_RTSP_PORT": b"8554",
            b"MOBLIN_RELAY_INTERNAL_RTMP_PORT": b"1935",
            b"MOBLIN_RELAY_INTERNAL_METRICS_PORT": b"9998",
            b"MOBLIN_RELAY_INTERNAL_CONTROL_API_PORT": b"9997",
            b"MOBLIN_RELAY_INTERNAL_CONTROL_TOKEN_FILE": (b"/run/moblin-relay/control-api.token"),
        }
    )
    write_process(102, list(production_environment.items()))
    escaped_environment = dict(exact_environment)
    escaped_environment[b"MOBLIN_RELAY_INTERNAL_CONTROL_TOKEN_FILE"] = os.fsencode(
        str((tmp_path / "other" / "control-api.token").resolve())
    )
    write_process(103, list(escaped_environment.items()))
    duplicate_environment = list(exact_environment.items()) + [
        (b"MOBLIN_RELAY_INTERNAL_RTMP_PORT", b"1935")
    ]
    write_process(104, duplicate_environment)
    write_process(105, list(exact_environment.items()))

    globals_ = matches.__globals__  # type: ignore[attr-defined]
    with patch.dict(
        globals_,
        {
            "PROC_ROOT": proc_root,
            "validate_workdir": lambda path: Path(path).resolve(),
            "validate_secret_config": lambda *_args: None,
            "proc_process_owner_uid": (
                lambda process_path: 1000 if process_path.name == "105" else 0
            ),
        },
    ):
        assert matches(work) == [101]


def test_self_test_normalizer_signal_revalidates_after_pidfd_open() -> None:
    loaded = load_self_test()
    send = loaded["signal_test_normalizer_supervisor"]
    work = Path("/var/lib/moblin-relay/tests/.run-current")
    identity_checks = iter((True, False))

    with (
        patch.dict(
            send.__globals__,  # type: ignore[attr-defined]
            {"is_test_normalizer_supervisor": lambda *_args: next(identity_checks)},
        ),
        patch.object(signal, "SIGKILL", 9, create=True),
        patch.object(os, "pidfd_open", create=True, return_value=77) as open_pidfd,
        patch.object(signal, "pidfd_send_signal", create=True) as send_pidfd,
        patch.object(os, "close") as close_descriptor,
    ):
        assert send(123, work, 9) is False

    open_pidfd.assert_called_once_with(123, 0)
    send_pidfd.assert_not_called()
    close_descriptor.assert_called_once_with(77)


def test_self_test_normalizer_reap_fails_closed_without_identity_anchor() -> None:
    loaded = load_self_test()
    terminate = loaded["terminate_test_normalizer_supervisors"]
    failure = loaded["TestFailure"]

    with (
        patch.dict(
            terminate.__globals__,  # type: ignore[attr-defined]
            {
                "validated_test_normalizer_token_path": lambda _work: (_ for _ in ()).throw(
                    failure("missing identity anchor")
                ),
            },
        ),
        patch.dict(
            terminate.__globals__,  # type: ignore[attr-defined]
            {"matching_test_normalizer_supervisor_ids": pytest.fail},
        ),
    ):
        assert terminate(Path("/var/lib/moblin-relay/tests/.run-damaged")) is False


def test_self_test_safe_stop_never_reuses_a_dead_process_group() -> None:
    loaded = load_self_test()
    safe_stop = loaded["safe_stop"]

    class ExitedProcess:
        pid = 4242

        @staticmethod
        def poll() -> int:
            return 0

    with patch.object(os, "killpg", create=True) as kill_group:
        assert safe_stop(ExitedProcess(), process_group=True) is True
    kill_group.assert_not_called()


def test_self_test_safe_stop_does_not_reuse_group_after_wait_reaps_leader() -> None:
    loaded = load_self_test()
    safe_stop = loaded["safe_stop"]

    class ReapedByWait:
        pid = 4343
        exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

        def wait(self, timeout: float) -> int:
            assert timeout == 8
            self.exited = True
            return 0

    process = ReapedByWait()
    with patch.object(os, "killpg", create=True) as kill_group:
        assert safe_stop(process, process_group=True) is True
    kill_group.assert_called_once_with(process.pid, signal.SIGINT)


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX result permissions required")
def test_self_test_failure_checkpoint_is_atomic_bounded_and_redacted(tmp_path: Path) -> None:
    loaded = load_self_test()
    collect = loaded["collect_sanitized_failure_diagnostics"]
    persist = loaded["persist_sanitized_result"]
    work = tmp_path / ".run-current"
    work.mkdir()
    marker = b"private-test-marker"
    log = work / "dut.log"
    log.write_text(
        "\n".join([f"old-{index}" for index in range(80)] + [marker.decode(), "last"]),
        encoding="utf-8",
    )
    log.chmod(0o600)
    result_file = tmp_path / "last-result.json"
    globals_ = collect.__globals__  # type: ignore[attr-defined]
    real_lstat = os.lstat
    real_fstat = os.fstat
    log_identity = (log.stat().st_dev, log.stat().st_ino)

    def fixture_root_owner(metadata: os.stat_result) -> os.stat_result:
        # CI runs as an unprivileged user; production diagnostic files are root-owned.
        # Preserve the real mode, inode and link count so all other checks still run.
        if (metadata.st_dev, metadata.st_ino) != log_identity:
            return metadata
        fields = list(metadata)
        fields[4] = 0
        return os.stat_result(fields)

    with (
        patch.dict(globals_, {"validate_workdir": lambda path: Path(path).resolve()}),
        patch.object(
            os, "lstat", lambda *args, **kwargs: fixture_root_owner(real_lstat(*args, **kwargs))
        ),
        patch.object(os, "fstat", lambda fd: fixture_root_owner(real_fstat(fd))),
    ):
        diagnostics = collect(work, [marker])
        assert len(diagnostics["dut.log"]) <= loaded["FAILURE_DIAGNOSTIC_LINE_LIMIT"]
        persist(
            result_file,
            {
                "status": "FAIL",
                "result_phase": "pre-cleanup",
                "failure": f"failed: {marker.decode()}",
                "sanitized_diagnostics": diagnostics,
            },
            [marker],
        )

    raw = result_file.read_bytes()
    checkpoint = json.loads(raw)
    assert marker not in raw
    assert checkpoint["status"] == "FAIL"
    assert checkpoint["result_phase"] == "pre-cleanup"
    assert "[REDACTED]" in checkpoint["failure"]
    assert "[REDACTED]" in checkpoint["sanitized_diagnostics"]["dut.log"]
    assert stat.S_IMODE(result_file.stat().st_mode) == 0o600

    persist(result_file, {"status": "FAIL", "result_phase": "final"}, [marker])
    assert json.loads(result_file.read_text(encoding="utf-8"))["result_phase"] == "final"


@pytest.mark.parametrize("path_uid,opened_uid", [(0, 0), (1001, 0), (0, 1001), (1001, 1001)])
def test_self_test_diagnostic_reader_requires_root_before_and_after_open(
    tmp_path: Path, path_uid: int, opened_uid: int
) -> None:
    loaded = load_self_test()
    read = loaded["read_sanitized_failure_log_tail"]
    failure = loaded["TestFailure"]
    work = tmp_path / ".run-owner"
    work.mkdir()
    log = work / "dut.log"
    log.write_text("private-test-marker\nlast", encoding="utf-8")
    real_lstat = os.lstat
    real_fstat = os.fstat
    log_identity = (log.stat().st_dev, log.stat().st_ino)

    def fixture_metadata(metadata: os.stat_result, uid: int) -> os.stat_result:
        if (metadata.st_dev, metadata.st_ino) != log_identity:
            return metadata
        fields = list(metadata)
        fields[0] = stat.S_IFREG | 0o600
        fields[4] = uid
        return os.stat_result(fields)

    with (
        patch.dict(read.__globals__, {"validate_workdir": lambda path: Path(path).resolve()}),  # type: ignore[attr-defined]
        patch.object(os, "O_CLOEXEC", getattr(os, "O_CLOEXEC", 0), create=True),
        patch.object(
            os,
            "lstat",
            lambda *args, **kwargs: fixture_metadata(real_lstat(*args, **kwargs), path_uid),
        ),
        patch.object(os, "fstat", lambda fd: fixture_metadata(real_fstat(fd), opened_uid)),
    ):
        if path_uid == opened_uid == 0:
            assert read(log, work, [b"private-test-marker"]) == ["[REDACTED]", "last"]
        else:
            with pytest.raises(failure, match="(?:unsafe|changed while opening)"):
                read(log, work, [b"private-test-marker"])


def test_self_test_quick_checkpoint_updates_canonical_and_mode_specific_results(
    tmp_path: Path,
) -> None:
    loaded = load_self_test()
    persist = loaded["persist_sanitized_results"]
    canonical = tmp_path / "last-result.json"
    quick = tmp_path / "last-quick-result.json"

    def portable_atomic(path: Path, value: dict, mode: int = 0o600) -> None:
        assert mode == 0o600
        path.write_text(json.dumps(value), encoding="utf-8")

    with patch.dict(persist.__globals__, {"atomic_json": portable_atomic}):  # type: ignore[attr-defined]
        persist(
            (quick, canonical),
            {"status": "FAIL", "mode": "quick", "result_phase": "pre-cleanup"},
            [],
        )

    for path in (canonical, quick):
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint == {
            "status": "FAIL",
            "mode": "quick",
            "result_phase": "pre-cleanup",
        }


def test_self_test_partial_result_write_cannot_leave_a_new_pass() -> None:
    loaded = load_self_test()
    persist = loaded["persist_sanitized_results"]
    failure = loaded["TestFailure"]
    quick = Path("/var/lib/moblin-relay/tests/last-quick-result.json")
    canonical = Path("/var/lib/moblin-relay/tests/last-result.json")
    written: dict[Path, dict] = {}
    failed_once = False

    def write(path: Path, value: dict, _markers: list[bytes]) -> None:
        nonlocal failed_once
        if path == canonical and value.get("status") == "PASS" and not failed_once:
            failed_once = True
            raise OSError("injected result write failure")
        written[path] = dict(value)

    with (
        patch.dict(
            persist.__globals__,  # type: ignore[attr-defined]
            {"persist_sanitized_result": write},
        ),
        pytest.raises(failure, match="unable to persist every self-test result target"),
    ):
        persist(
            (quick, canonical),
            {"status": "PASS", "mode": "quick", "result_phase": "final"},
            [],
        )

    assert written.keys() == {quick, canonical}
    assert all(value["status"] == "FAIL" for value in written.values())
    assert all(
        value["failure"] == "self-test result persistence incomplete" for value in written.values()
    )


def test_self_test_preserves_identity_anchor_when_scoped_reap_fails(tmp_path: Path) -> None:
    loaded = load_self_test()
    cleanup = loaded["cleanup_test_workdir_after_supervisors"]
    work = tmp_path / ".run-current"
    work.mkdir()
    token = work / "control-api.token"
    token.write_bytes(b"identity-anchor\n")

    with (
        patch.dict(
            cleanup.__globals__,  # type: ignore[attr-defined]
            {
                "discover_secret_configs": pytest.fail,
                "wipe_secret_config": pytest.fail,
                "remove_workdir": pytest.fail,
            },
        ),
    ):
        wiped, removed, errors = cleanup(work, {token}, supervisors_stopped=False)

    assert (wiped, removed, errors) == (0, False, [])
    assert token.read_bytes() == b"identity-anchor\n"
    assert work.is_dir()


def test_self_test_failure_checkpoint_precedes_cleanup_and_scoped_reaping() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    main = source.split("def main() -> int:", 1)[1]
    failure = main.split("    except Exception as exc:", 1)[1].split(
        "    finally:\n        cleanup_errors = []", 1
    )[0]
    cleanup = main.split("    finally:\n        cleanup_errors = []", 1)[1]
    stale_cleanup = source.split("def cleanup_stale_workdirs()", 1)[1].split(
        "def validate_secret_config", 1
    )[0]

    assert 'result["result_phase"] = "pre-cleanup"' in failure
    assert "collect_sanitized_failure_diagnostics(" in failure
    assert "persist_sanitized_results(result_files, result, markers)" in failure
    assert cleanup.index("terminate_test_normalizer_supervisors(work)") < cleanup.index(
        "cleanup_test_workdir_after_supervisors("
    )
    assert "result_files = (result_file, RESULT_FILE) if args.quick else (RESULT_FILE,)" in main
    assert 'result["cleanup_identity_anchor_preserved"] = True' in cleanup
    assert "supervisors_stopped=test_normalizers_stopped" in cleanup
    assert stale_cleanup.index("terminate_test_normalizer_supervisors(path)") < (
        stale_cleanup.index("wipe_secret_config(config, path)")
    )
    assert "matching_process_ids(" not in source
    assert "os.kill(old_supervisor_pid" not in source
    assert 'result["result_phase"] = "final"' in cleanup


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
    assert {path.name for path in BUNDLE.iterdir() if path.is_file()} == expected
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(BUNDLE.iterdir()) if path.is_file()
    )
    assert "176.98.181.225" not in combined
    assert "172.29.172.1" not in combined
    assert "install-manifest.json" not in expected
    assert not any(path.suffix in {".mp4", ".tar", ".gz"} for path in BUNDLE.iterdir())


def test_relayctl_health_requires_the_loopback_recovery_api() -> None:
    source = RELAYCTL.read_text(encoding="utf-8")
    health = source.split("def cmd_health()", 1)[1].split("def log_redaction_values", 1)[0]

    assert "CONTROL_API_PORT = 9997" in source
    assert "recovery_api_loopback = loopback_tcp_listener(CONTROL_API_PORT)" in health
    assert "and recovery_api_loopback" in health
    assert "Recovery API TCP/{CONTROL_API_PORT}" in health


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
        "stuck-start",
        "stuck-slate",
        "stuck-open",
        "stuck-kicked",
        "stuck-live",
        "stuck-source",
        "stuck-cont",
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
        "ts-v-order",
        "ts-v-fps",
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
    assert "ts-v-cluster" not in loaded["SELF_TEST_STAGES"]
    assert not any(stage.startswith("ts-vc-") for stage in loaded["SELF_TEST_STAGES"])
    assert 'return "stall-i-id"' not in source
    assert 'mark_self_test_stage("stall-i-id")' not in source
    assert "def timestamp_failure_stage(" in source

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
    assert outage_block.index("feeder = start_live_feeder()") < outage_block.index(
        "outage_ingest = wait_new_authenticated_ingest("
    )
    assert outage_block.index("outage_ingest = wait_new_authenticated_ingest(") < (
        outage_block.index("recovered = wait_healthy_live(")
    )
    assert 'sink_transition_started=float(outage_ingest["finished"])' in outage_block

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
    record_segment_helper = source.split("def record_strict_sink_segment(", 1)[1].split(
        "def start_source_helper(", 1
    )[0]
    assert '"initial healthy SLATE"' in record_segment_helper
    assert 'result_key="initial_slate_rtmp_sink_media"' in record_segment_helper
    assert record_segment_helper.index("before_sample = observer.checked_snapshot(") < (
        record_segment_helper.index("capture_final_sink_media_segment(")
    )
    assert record_segment_helper.index("capture_final_sink_media_segment(") < (
        record_segment_helper.index("after_sample = observer.checked_snapshot(")
    )
    assert "validate_final_sink_media_segment(" not in record_segment_helper
    assert "pending_strict_sink_segments.append(" in record_segment_helper
    transition_helper = source.split("def wait_downstream_transition(", 1)[1].split(
        "def wait_new_authenticated_ingest(", 1
    )[0]
    assert transition_helper.index("sample = wait_sink_delivery_growth(") < transition_helper.index(
        "record_strict_sink_segment("
    )
    assert 'if "LIVE" in description or description == INTENTIONAL_RTMP_RECONNECT_EVENT:' in (
        transition_helper
    )
    assert '"initial_live_rtmp_sink_media"' in transition_helper
    assert (
        "SLATE"
        not in transition_helper.split(
            'if "LIVE" in description or description == INTENTIONAL_RTMP_RECONNECT_EVENT:', 1
        )[0].split("recovered_sink_id =", 1)[1]
    )
    assert 'stop_primary_srt_source("final continuity transition")' in continuity_block
    assert "final_slate = wait_slate_with_live_srt(" in continuity_block
    assert (
        'wait_slate_capture_growth("final SLATE capture growth", final_slate)' in continuity_block
    )
    assert continuity_block.index("final_slate = wait_slate_with_live_srt(") < (
        continuity_block.index("wait_srt_idle_expiry(")
    )
    assert continuity_block.index(
        'wait_slate_capture_growth("final SLATE capture growth", final_slate)'
    ) < continuity_block.index(
        'wait_slate_downstream_recovery("final SLATE transition", final_started)'
    )
    assert "maximum_capture_no_growth_seconds(final_samples)" in continuity_block
    assert "kick_test_sink_rtmp_connection(forced_old_sink_id)" in continuity_block
    assert '"forced RTMP sink reconnect"' in continuity_block
    assert "record_strict_sink_segment(" in continuity_block
    final_stable = "final_stable = wait_sink_delivery_growth("
    final_capture = "record_strict_sink_segment("
    final_recent = "final_recent = wait_sink_delivery_growth("
    assert (
        continuity_block.index(final_stable)
        < continuity_block.index(final_capture)
        < continuity_block.index(final_recent)
    )
    final_capture_block = continuity_block.split(final_capture, 1)[1].split(final_recent, 1)[0]
    assert '"stable final SLATE"' in final_capture_block
    assert 'result_key="final_rtmp_sink_media"' in final_capture_block
    assert 'result["rtsp_capture_session_preserved"]' in continuity_block
    assert 'result["automatic_rtmp_forward_recovery"]' in continuity_block
    assert 'result["event_to_delivery_recovery"]' in continuity_block
    assert '"event": description' in source
    assert "downstream_unique_rtmp_ids" not in continuity_block
    assert "downstream_connection_preserved" not in source
    assert "reader_survived_all_switches" not in source

    local_slate_wait = source.split("def wait_slate_with_live_srt(", 1)[1].split(
        "def wait_slate_downstream_recovery(", 1
    )[0]
    downstream_slate_wait = source.split("def wait_slate_downstream_recovery(", 1)[1].split(
        "def wait_slate_capture_growth(", 1
    )[0]
    assert "LIVE_TO_SLATE_DEADLINE_SECONDS" in local_slate_wait
    assert "observer.wait_sample(" in local_slate_wait
    assert "wait_downstream_transition(" not in local_slate_wait
    assert "RTMP_SINK_RECOVERY_TIMEOUT_SECONDS" in downstream_slate_wait
    assert "wait_downstream_transition(" in downstream_slate_wait

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
    mixed_format_check = decode_block.split("format_failure = output_format_failure_stage(", 1)[
        1
    ].split("if format_failure", 1)[0]
    assert "check_b_frames=False" in mixed_format_check
    sink_capture_helper = source.split("def capture_final_sink_media_segment(", 1)[1].split(
        "def validate_final_sink_media_segment(", 1
    )[0]
    sink_validation_helper = source.split("def validate_final_sink_media_segment(", 1)[1].split(
        "def timestamp_failure_stage(", 1
    )[0]
    assert "output_format_failure_stage(streams[0], streams[1], expected_video)" in (
        sink_validation_helper
    )
    assert "check_b_frames=False" not in sink_validation_helper
    assert 'f"sink-proof-{segment_index:03d}.flv"' in sink_capture_helper
    assert "str(VIDEO_GOP_FRAMES + VIDEO_FPS)" in sink_capture_helper
    assert '"-c",\n            "copy"' in sink_capture_helper
    assert "stream_signature(" not in sink_capture_helper
    assert "video_gop_signature(" not in sink_capture_helper
    assert "analyze_decoded_video_frames(" not in sink_capture_helper
    assert '"-err_detect"' not in sink_capture_helper
    assert '"-xerror"' in sink_validation_helper
    assert '"-err_detect"' in sink_validation_helper
    assert '"explode"' in sink_validation_helper
    assert 'decoded_frames["decode_error_flags"]' in sink_validation_helper
    assert "decoded_audio = analyze_decoded_audio_timestamps(sink_capture)" in (
        sink_validation_helper
    )
    assert "timestamps = analyze_timestamps(sink_capture)" in sink_validation_helper
    assert "timestamp_stage = timestamp_failure_stage(" in sink_validation_helper
    assert 'timestamp_stage == "ts-probe-pts"' in sink_validation_helper
    assert '"video-pts-count"' in sink_validation_helper
    assert '"audio-pts-count"' in sink_validation_helper
    assert 'timestamp_stage != "timestamps"' in sink_validation_helper
    assert 'f"({timestamp_stage})"' in sink_validation_helper
    assert '"audio_frames": int(decoded_audio["frame_count"])' in sink_validation_helper
    assert "sink_capture.unlink(missing_ok=True)" in sink_validation_helper
    assert '"-xerror"' in sink_capture_helper
    deferred_validation = continuity_block.split("safe_stop(reader)", 1)[1].split(
        'mark_self_test_stage("decode")', 1
    )[0]
    assert "validate_final_sink_media_segment(" in deferred_validation
    assert "pending_strict_sink_segments" in deferred_validation
    assert 'result["strict_sink_segment_validation_progress"]' in deferred_validation
    assert '"current_segment": segment_number' in deferred_validation
    assert 'result["strict_sink_segment_validation"]' in deferred_validation
    assert 'item["audio_frames"] for item in strict_sink_segment_counts' in deferred_validation
    assert continuity_block.index("capture_observer.finish()") < continuity_block.index(
        "safe_stop(reader)"
    )
    assert continuity_block.index("observer.finish()") < continuity_block.index("safe_stop(reader)")
    main_after_continuity = source.split('mark_self_test_stage("continuity")', 1)[1]
    assert main_after_continuity.index("validate_final_sink_media_segment(") < (
        main_after_continuity.index('mark_self_test_stage("secrets")')
    )
    assert main_after_continuity.index("validate_final_sink_media_segment(") < (
        main_after_continuity.index("safe_stop(dut, process_group=True)")
    )
    assert decode_block.index('mark_self_test_stage("gop")') < decode_block.index(
        'normalized_signature["video_gop"] = video_gop_signature(capture)'
    )
    assert decode_block.index('mark_self_test_stage("decoder")') < decode_block.index(
        "decode = run("
    )
    aggregate_decode_command = decode_block.split("decode = run(", 1)[1].split("decode_text =", 1)[
        0
    ]
    assert '"-err_detect"' in aggregate_decode_command
    assert '"explode"' in aggregate_decode_command
    assert '"-xerror"' not in aggregate_decode_command
    assert "timestamp_warning = re.compile" in decode_block
    assert "media_corruption = re.compile" in decode_block
    assert 'result["decode_timestamp_warning_count"]' in decode_block
    assert 'result["decode_error_pattern_found"] = bool(media_corruption.search' in decode_block
    assert 'result["aggregate_rtsp_capture_diagnostic_only"] = True' in decode_block
    assert 'result["aggregate_rtsp_capture_corruption_oracle"] = False' in decode_block
    assert 'raise TestFailure("capture decoder validation failed")' not in decode_block
    assert 'raise TestFailure("capture frame validation failed")' not in decode_block
    assert 'raise TestFailure("capture timestamp validation failed")' not in decode_block
    assert decode_block.index('mark_self_test_stage("frames")') < decode_block.index(
        'result["decoded_video_frames"] = analyze_decoded_video_frames(capture)'
    )
    assert decode_block.index('mark_self_test_stage("timestamps")') < decode_block.index(
        'result["decoded_audio_timestamps"] = analyze_decoded_audio_timestamps(capture)'
    )
    assert "capture decode/timestamp validation failed" not in decode_block


def test_self_test_validates_actual_decoded_pts_on_native_flv_capture() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    loaded = load_self_test()
    analyze = loaded["analyze_decoded_video_frames"]
    assert callable(analyze)

    assert 'capture = work / "capture.flv"' in source
    assert 'debug_capture = TEST_ROOT / "debug-capture.flv"' in source
    reader_block = source.split("reader = subprocess.Popen(", 1)[1].split(
        "processes.append(reader)", 1
    )[0]
    rtsp_input = 'f"rtsp://127.0.0.1:{DUT_RTSP_PORT}/{OUTPUT_PATH}"'
    assert rtsp_input in reader_block
    assert reader_block.index('"-rtsp_transport"') < reader_block.index('"-i"')
    assert '"-loglevel",\n                "error"' in reader_block
    assert 'f"rtmp://127.0.0.1:{SINK_RTMP_PORT}/live/sink"' not in reader_block
    assert '"-c",\n                "copy"' in reader_block
    assert '"-f",\n                "flv"' in reader_block
    assert '"mpegts"' not in reader_block
    write_configs_block = source.split("def write_configs(", 1)[1].split(
        "def write_source_config(", 1
    )[0]
    assert '"writeQueueSize": 1024' in write_configs_block

    analyzer_block = source.split("def analyze_decoded_video_frames", 1)[1].split(
        "def analyze_decoded_audio_timestamps", 1
    )[0]
    assert (
        '"frame=width,height,pix_fmt,key_frame,pict_type,decode_error_flags,'
        '"\n        "pts_time,pkt_pts_time,best_effort_timestamp_time"' in analyzer_block
    )
    assert '(("pts_time", "pkt_pts_time"), presentation_timestamps)' in analyzer_block
    assert '(("best_effort_timestamp_time",), best_effort_timestamps)' in analyzer_block

    packet_analyzer_block = source.split("def analyze_timestamps", 1)[1].split(
        "def analyze_decoded_video_frames", 1
    )[0]
    assert '"video_pts_dts_offset_clusters_over_normal_reorder": (' in packet_analyzer_block

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
                frame_prefix + "pkt_pts_time=0.033333|best_effort_timestamp_time=0.033333\n",
                frame_prefix + "pts_time=0.020000|best_effort_timestamp_time=0.066667\n",
            ]
        )

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 60
            return "", ""

    with patch("subprocess.Popen", return_value=FakeProbe()) as popen:
        result = analyze(Path("native-downstream.flv"))

    command = popen.call_args.args[0]
    assert any("pts_time,pkt_pts_time,best_effort_timestamp_time" in item for item in command)
    assert result["presentation_timestamp_count"] == result["frame_count"] == 3
    assert not result["strict_presentation_timestamps_monotonic"]
    assert result["maximum_presentation_timestamp_backward_step_seconds"] == pytest.approx(0.013333)
    assert result["presentation_frame_rate_matches"]
    assert result["strict_best_effort_timestamps_monotonic"]

    timestamp_gate = source.split("timestamp_ok = (", 1)[1].split(
        'result["timestamp_thresholds_passed"] = timestamp_ok', 1
    )[0]
    assert 'decoded_frames["strict_presentation_timestamps_monotonic"]' in timestamp_gate
    assert 'decoded_frames["strict_best_effort_timestamps_monotonic"]' not in timestamp_gate
    assert "video_pts_dts_offset_clusters_over_normal_reorder" not in timestamp_gate
    for required_check in (
        'result["timestamps"]["max_pts_dts_offset_seconds"].get(0, 999)',
        'result["timestamps"]["max_pts_dts_offset_seconds"].get(1, 999)',
        'result["timestamps"]["max_dts_gap_seconds"].get(0, 999)',
        'result["timestamps"]["max_sorted_pts_gap_seconds"].get(0, 999)',
        'decoded_frames["presentation_frame_rate_matches"]',
        'decoded_frames["maximum_presentation_timestamp_gap_seconds"]',
        'decoded_audio["maximum_presentation_timestamp_gap_seconds"]',
        'result["timestamps"]["audio_video_duration_difference_seconds"]',
        'result["timestamps"]["audio_video_end_difference_seconds"]',
    ):
        assert required_check in timestamp_gate

    timestamp_validation = source.split(
        'result["decoded_audio_timestamps"] = analyze_decoded_audio_timestamps(capture)',
        1,
    )[1].split('mark_self_test_stage("secrets")', 1)[0]
    assert 'result["timestamps"]["dts_within_tolerance"]' in timestamp_validation
    assert 'result["timestamps"]["negative_dts_steps"].get(0, 0) == 0' in timestamp_validation
    assert (
        'decoded_audio["presentation_timestamp_steps_beyond_tolerance"] == 0'
        in timestamp_validation
    )
    assert "analysis.ts" not in timestamp_validation
    assert "genpts" not in timestamp_validation
    event_limits = source.split('result["timestamp_event_limits"] = {', 1)[1].split(
        'result["timestamp_thresholds_passed"]', 1
    )[0]
    assert "video_pts_dts_offset_clusters_over_normal_reorder" not in event_limits


@pytest.mark.parametrize(
    "width,height,failure_text",
    [
        (1080, 1920, None),
        (1280, 720, "landscape 1280x720"),
        (720, 1280, "legacy portrait 720x1280"),
        (1920, 1080, "outside the 1080x1920 portrait profile"),
    ],
)
def test_self_test_rejects_observed_resolution_changes_despite_recorder_decode_artifacts(
    width: int, height: int, failure_text: str | None
) -> None:
    loaded = load_self_test()
    analyze = loaded["analyze_decoded_video_frames"]
    require_dimensions = loaded["require_aggregate_portrait_dimensions"]
    failure = loaded["TestFailure"]

    class FakeProbe:
        returncode = 1

        def __init__(self) -> None:
            self.stdout = iter(
                [
                    "width=1080|height=1920|pix_fmt=yuv420p|decode_error_flags=0\n",
                    f"width={width}|height={height}|pix_fmt=yuv420p|decode_error_flags=1\n",
                    "width=1080|height=1920|pix_fmt=yuv420p|decode_error_flags=0\n",
                ]
            )

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 60
            return "", "diagnostic reader decode artifact"

    with patch("subprocess.Popen", return_value=FakeProbe()):
        decoded = analyze(Path("aggregate-downstream.flv"))
    assert decoded["frame_count"] == 3
    assert decoded["ffprobe_exit"] == 1
    assert decoded["decode_error_flags"]
    assert not decoded["stderr_empty"]
    if failure_text is None:
        require_dimensions(decoded)
    else:
        with pytest.raises(failure, match=failure_text):
            require_dimensions(decoded)

    source = SELF_TEST.read_text(encoding="utf-8")
    aggregate = source.split('result["decoded_video_frames"] = ', 1)[1].split(
        'mark_self_test_stage("timestamps")', 1
    )[0]
    assert "require_aggregate_portrait_dimensions(decoded_frames)" in aggregate


def test_self_test_format_diagnostics_identify_each_safe_predicate() -> None:
    loaded = load_self_test()
    classify = loaded["output_format_failure_stage"]
    assert callable(classify)

    expected_video = {"profile": "Main", "level": 40, "has_b_frames": 0}
    video = {
        "codec_name": "h264",
        "profile": "Main",
        "level": 40,
        "has_b_frames": 0,
        "width": loaded["PORTRAIT_WIDTH"],
        "height": loaded["PORTRAIT_HEIGHT"],
        "pix_fmt": "yuv420p",
        "r_frame_rate": f"{loaded['VIDEO_FPS']}/1",
        "avg_frame_rate": "0/0",
    }
    audio = {
        "codec_name": "aac",
        "profile": "LC",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    }
    assert classify(video, audio, expected_video) is None
    mixed_video = {**video, "has_b_frames": 2}
    assert classify(mixed_video, audio, expected_video, check_b_frames=False) is None
    assert (
        classify(
            {**mixed_video, "profile": "High"},
            audio,
            expected_video,
            check_b_frames=False,
        )
        == "fmt-v-prof"
    )

    cases = (
        ("video", "codec_name", "hevc", "fmt-v-codec"),
        ("video", "profile", "High", "fmt-v-prof"),
        ("video", "level", 41, "fmt-v-level"),
        ("video", "has_b_frames", 2, "fmt-v-bframes"),
        ("video", "width", 720, "fmt-v-size"),
        ("video", "height", 1280, "fmt-v-size"),
        ("video", "pix_fmt", "yuv444p", "fmt-v-pixfmt"),
        ("video", "r_frame_rate", "60/1", "fmt-v-rfps"),
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
            "sink_bytes": 1000,
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
    assert (
        stage(
            [
                sample(1.2, 101),
                sample(1.2 + loaded["CAPTURE_NO_GROWTH_LIMIT_SECONDS"] + 0.1, 101),
            ]
        )
        == "stall-i-byte"
    )
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


def test_self_test_initial_live_log_gates_are_fixed_scoped_and_secret_free(
    tmp_path: Path,
) -> None:
    loaded = load_self_test()
    open_tail = loaded["open_validated_log_tail"]
    wait_bridge = loaded["wait_initial_live_bridge_active"]
    require_clean = loaded["require_initial_live_log_clean"]
    failure = loaded["TestFailure"]
    bridge_marker = loaded["NORMALIZER_BRIDGE_ACTIVE_MARKER"]
    forbidden_markers = loaded["INITIAL_LIVE_FORBIDDEN_LOG_MARKERS"]
    expected_uid = getattr(os, "geteuid", lambda: 0)()
    log_path = tmp_path / "dut.log"
    assert bridge_marker == b"moblin-relay-normalize:state:bridge-active"
    assert forbidden_markers == (
        (b"Packet is missing PTS", "missing-pts"),
        (b"Timestamps are unset in a packet", "timestamps-unset"),
        (b"av_interleaved_write_frame(): Invalid argument", "mux-invalid-argument"),
        (b"moblin-relay-normalize:restart:child-exit", "child-exit"),
        (
            b"moblin-relay-normalize:restart:output-start-timeout",
            "output-start-timeout",
        ),
    )
    assert loaded["TRANSITION_FORBIDDEN_LOG_MARKERS"] == (
        (b"Packet is missing PTS", "missing-pts"),
        (b"Timestamps are unset in a packet", "timestamps-unset"),
        (b"av_interleaved_write_frame(): Invalid argument", "mux-invalid-argument"),
    )
    assert loaded["KNOWN_DTS_REGRESSION_MARKER"] not in {
        marker for marker, _reason in forbidden_markers
    }
    assert loaded["KNOWN_DTS_REGRESSION_MARKER"] not in {
        marker for marker, _reason in loaded["TRANSITION_FORBIDDEN_LOG_MARKERS"]
    }
    assert loaded["RTMP_SINK_RECOVERY_TIMEOUT_SECONDS"] == 15.0
    main_source = SELF_TEST.read_text(encoding="utf-8").split("def main()", 1)[1]
    global_log_open = "transition_log_descriptor, transition_log_offset = open_validated_log_tail("
    global_log_check = "transition_failure = transition_log_failure("
    assert (
        main_source.index('mark_self_test_stage("auth-source")')
        < main_source.index(global_log_open)
        < main_source.index("primary_helper = start_source_helper(")
    )
    assert (
        main_source.index('mark_self_test_stage("continuity")')
        < main_source.index(global_log_check)
        < main_source.index('mark_self_test_stage("decode")')
    )
    global_check_block = main_source.split(global_log_check, 1)[1].split(
        'mark_self_test_stage("decode")', 1
    )[0]
    assert 'mark_self_test_stage("dts-regression")' in global_check_block
    delivery_gate = (
        "require_accounted_downstream_recovery(delivery_summary, event_delivery_summary)"
    )
    dts_diagnostic = "dts_reconnects = transition_log_tail.count(KNOWN_DTS_REGRESSION_MARKER)"
    assert main_source.index(delivery_gate) < main_source.index(dts_diagnostic)
    assert (
        'delivery["max_delivery_outage_seconds"] > RTMP_SINK_RECOVERY_TIMEOUT_SECONDS'
        in SELF_TEST.read_text(encoding="utf-8")
        .split("def require_accounted_downstream_recovery(", 1)[1]
        .split("def observer_health_problem(", 1)[0]
    )

    with (
        log_path.open("w+b") as writer,
        patch.dict(open_tail.__globals__, {"validate_workdir": lambda path: path.resolve()}),
    ):
        writer.write(bridge_marker + b"\n")
        for marker, _reason in forbidden_markers:
            writer.write(marker + b"\n")
        writer.flush()

        descriptor, offset = open_tail(log_path, writer.fileno(), tmp_path, expected_uid)
        try:
            with pytest.raises(failure, match="bridge-active-missing"):
                wait_bridge(descriptor, offset, expected_uid, timeout=0)
            writer.write(bridge_marker + b"\n")
            writer.flush()
            wait_bridge(descriptor, offset, expected_uid, timeout=0)
            require_clean(descriptor, offset, expected_uid)
        finally:
            os.close(descriptor)

        raw_secret = b"stream-key-should-never-escape"
        for marker, reason in forbidden_markers:
            descriptor, offset = open_tail(log_path, writer.fileno(), tmp_path, expected_uid)
            try:
                writer.write(marker + b" " + raw_secret + b"\n")
                writer.flush()
                with pytest.raises(failure) as captured:
                    require_clean(descriptor, offset, expected_uid)
                assert str(captured.value) == f"initial LIVE log gate failed: {reason}"
                assert raw_secret.decode() not in str(captured.value)
            finally:
                os.close(descriptor)


def test_self_test_initial_live_log_interval_brackets_source_and_closes_fd() -> None:
    source = SELF_TEST.read_text(encoding="utf-8")
    block = source.split('mark_self_test_stage("auth-source")', 1)[1].split(
        'mark_self_test_stage("auth-scan")', 1
    )[0]

    opened = "initial_live_log_descriptor, initial_live_log_offset = open_validated_log_tail("
    source_start = "primary_helper = start_source_helper("
    media_clock = "initial_live_started = time.monotonic()"
    feeder_start = "feeder = start_live_feeder()"
    helper_ready = "wait_helper_path("
    accepted_ingest = "accepted_ingest = observer.wait_sample("
    healthy_live = "initial_live = wait_healthy_live("
    bridge_gate = "wait_initial_live_bridge_active("
    clean_gate = "require_initial_live_log_clean("
    close = "finally:\n            os.close(initial_live_log_descriptor)"
    assert (
        block.index(opened)
        < block.index(source_start)
        < block.index(media_clock)
        < block.index(feeder_start)
        < block.index(helper_ready)
        < block.index(accepted_ingest)
        < block.index(healthy_live)
        < block.index(bridge_gate)
        < block.index(clean_gate)
        < block.index(close)
    )
    assert '"bridge_active": True' in block
    assert '"forbidden_markers_absent": True' in block
    accepted_block = block.split(accepted_ingest, 1)[1].split(
        'mark_self_test_stage("live-normalize")', 1
    )[0]
    assert "sink_delivery_active" not in accepted_block
    assert 'sink_transition_started=float(accepted_ingest["finished"])' in block


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
            writer.write(b"moblin-relay-normalize:restart:ingest-confirmed-stall\n")
            writer.flush()
            assert classify_restart(descriptor, offset, expected_uid) == "auth-n-confirm"
            assert wait_restart(descriptor, offset, expected_uid, timeout=0) == "auth-n-confirm"
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


def test_self_test_tracks_bounded_rtmp_sink_delivery_without_exposing_ids() -> None:
    loaded = load_self_test()
    summarize = loaded["summarize_downstream_delivery"]
    summarize_ledger = loaded["summarize_event_delivery_ledger"]
    growth_gate_type = loaded["SinkDeliveryGrowthGate"]
    failure = loaded["TestFailure"]

    def sample(
        observed: float,
        publisher_ids: list[str],
        inbound_bytes: int | None,
        *,
        sink_metrics_ok: bool = True,
        dut_metrics_ok: bool = True,
        forward: bool = True,
    ) -> dict[str, object]:
        return {
            "t": observed,
            "sink_metrics_ok": sink_metrics_ok,
            "dut_metrics_ok": dut_metrics_ok,
            "forward": forward,
            "sink_ids": publisher_ids,
            "sink_bytes": inbound_bytes,
        }

    gate = growth_gate_type()
    assert gate.observe(sample(0.0, ["publisher-a"], 100)) is False
    assert gate.observe(sample(0.1, ["publisher-a"], 120)) is True
    assert gate.observe(sample(0.2, ["publisher-b"], 1)) is False
    gate.reset()
    assert gate.observe(sample(0.3, ["publisher-b"], 2)) is False

    stable = summarize(
        [sample(0.0, ["publisher-a"], 100), sample(0.2, ["publisher-a"], 120)],
        0.0,
        0.25,
    )
    assert stable == {
        "unique_publisher_count": 1,
        "delivery_gap_count": 0,
        "id_rotation_count": 0,
        "inactive_samples": 0,
        "invalid_samples": 0,
        "duplicate_publishers": False,
        "counter_regression": False,
        "max_delivery_outage_seconds": pytest.approx(0.2),
        "final_active": True,
    }

    same_id = summarize(
        [
            sample(0.0, ["publisher-a"], 100),
            sample(0.2, ["publisher-a"], 120),
            sample(0.4, [], None),
            sample(0.6, ["publisher-a"], 120),
            sample(0.8, ["publisher-a"], 140),
        ],
        0.0,
        0.9,
    )
    assert same_id["delivery_gap_count"] == 1
    assert same_id["id_rotation_count"] == 0
    assert same_id["max_delivery_outage_seconds"] == pytest.approx(0.6)

    rotated = summarize(
        [
            sample(0.0, ["publisher-a"], 100),
            sample(0.2, ["publisher-a"], 120),
            sample(0.4, [], None),
            sample(0.6, ["publisher-b"], 1),
            sample(0.8, ["publisher-b"], 21),
        ],
        0.0,
        0.9,
    )
    assert rotated["delivery_gap_count"] == 1
    assert rotated["id_rotation_count"] == 1
    assert rotated["unique_publisher_count"] == 2

    direct_rotation = summarize(
        [
            sample(0.0, ["publisher-a"], 100),
            sample(0.2, ["publisher-a"], 120),
            sample(0.4, ["publisher-b"], 1),
            sample(0.6, ["publisher-b"], 21),
        ],
        0.0,
        0.7,
    )
    assert direct_rotation["delivery_gap_count"] == 1
    assert direct_rotation["id_rotation_count"] == 1

    blind = summarize(
        [
            sample(0.0, ["publisher-a"], 100),
            sample(0.2, ["publisher-a"], 120),
            sample(0.4, [], None, sink_metrics_ok=False),
            sample(0.8, ["publisher-a"], 140),
        ],
        0.0,
        0.9,
    )
    assert blind["delivery_gap_count"] == 0
    assert blind["max_delivery_outage_seconds"] == pytest.approx(0.6)

    duplicate = summarize(
        [sample(0.0, ["publisher-a", "publisher-b"], 100)],
        0.0,
        0.1,
    )
    assert duplicate["duplicate_publishers"] is True
    assert duplicate["final_active"] is False

    invalid = summarize(
        [
            sample(0.0, ["publisher-a"], 100),
            sample(0.2, ["publisher-a"], None),
        ],
        0.0,
        0.3,
    )
    assert invalid["invalid_samples"] == 1
    assert invalid["delivery_gap_count"] == 0
    assert invalid["final_active"] is False

    over_bound = summarize(
        [sample(0.0, ["publisher-a"], 100), sample(15.1, ["publisher-a"], 120)],
        0.0,
        15.2,
    )
    assert over_bound["max_delivery_outage_seconds"] == pytest.approx(15.1)

    forced_event = loaded["INTENTIONAL_RTMP_RECONNECT_EVENT"]
    expected = ("SLATE", forced_event)
    ledger = [
        {"event": "SLATE", "recovery_seconds": 1.2, "publisher_rotated": False},
        {"event": forced_event, "recovery_seconds": 5.4, "publisher_rotated": True},
    ]
    assert summarize_ledger(ledger, expected) == {
        "event_count": 2,
        "recovery_limit_seconds": 15.0,
        "maximum_event_to_delivery_seconds": 5.4,
        "events_with_publisher_rotation": 1,
    }
    with pytest.raises(failure, match="ledger is incomplete"):
        summarize_ledger(ledger, ("LIVE", "SLATE"))
    with pytest.raises(failure, match="ledger is invalid"):
        summarize_ledger(
            [{"event": "SLATE", "recovery_seconds": 15.1, "publisher_rotated": False}],
            ("SLATE",),
        )
    with pytest.raises(failure, match="unexpected downstream RTMP publisher rotation"):
        summarize_ledger(
            [{"event": "LIVE", "recovery_seconds": 1.0, "publisher_rotated": True}],
            ("LIVE",),
        )
    with pytest.raises(failure, match="intentional downstream RTMP reconnect"):
        summarize_ledger(
            [{"event": forced_event, "recovery_seconds": 1.0, "publisher_rotated": False}],
            (forced_event,),
        )


def test_self_test_forward_rotation_requires_fresh_scoped_timestamp_evidence() -> None:
    loaded = load_self_test()
    summarize = loaded["summarize_event_delivery_ledger"]
    count_markers = loaded["known_forward_dts_marker_count"]
    failure = loaded["TestFailure"]
    # Pinned MediaMTX v1.20.1 DestHandler.Log emits [RTMP dest <pos> <8hex>].
    known = (
        b"2026/09/05 12:00:00 ERR [path relay-output] [RTMP dest 1 a12bc345] "
        b"DTS is not monotonically increasing, was 200, now is 100\n"
    )
    unrelated = (
        known.replace(b"relay-output", b"iphone-live")
        + known.replace(b"RTMP dest 1", b"RTMP dest 2")
        + known.replace(b"RTMP dest 1", b"SRT dest 1")
        + known.replace(b"a12bc345", b"a12bc34")
        + known.replace(b"a12bc345", b"a12bc3456")
        + known.replace(b"a12bc345", b"not-hex!")
        + known.replace(b" ERR ", b" WAR ")
        + known.replace(b"[RTMP dest 1 a12bc345]", b"[forward rtmp://127.0.0.1/live]")
        + known.replace(b"DTS is not monotonically increasing", b"connection closed")
        + b"untrusted prefix "
        + known
        + b"FFmpeg: DTS is not monotonically increasing\n"
    )
    assert count_markers(unrelated) == 0
    assert count_markers(known + unrelated) == 1
    recovered = {
        "event": "LIVE",
        "recovery_seconds": 5.4,
        "publisher_rotated": True,
        "known_dts_marker_count": count_markers(known + unrelated),
    }
    assert summarize([recovered], ("LIVE",))["events_with_publisher_rotation"] == 1
    for overrides in (
        {"recovery_seconds": 15.001},
        {"recovery_seconds": float("nan")},
        {"known_dts_marker_count": -1},
        {"known_dts_marker_count": True},
        {"known_dts_marker_count": "1"},
    ):
        with pytest.raises(failure, match="ledger is invalid"):
            summarize([{**recovered, **overrides}], ("LIVE",))
    for previous_rotated in (False, True):
        # The previous checkpoint consumes evidence even when it did not rotate.
        stale_ledger = [
            {**recovered, "event": "previous", "publisher_rotated": previous_rotated},
            recovered,
        ]
        with pytest.raises(failure, match="unexpected downstream RTMP publisher rotation"):
            summarize(stale_ledger, ("previous", "LIVE"))
    fresh_ledger = [
        {**recovered, "event": "previous"},
        {**recovered, "known_dts_marker_count": count_markers(known + known)},
    ]
    assert summarize(fresh_ledger, ("previous", "LIVE"))["events_with_publisher_rotation"] == 2

    source = SELF_TEST.read_text(encoding="utf-8")
    transition = source.split("def wait_downstream_transition(", 1)[1].split(
        "def wait_new_authenticated_ingest(", 1
    )[0]
    assert "known_forward_dts_marker_count(\n                read_validated_log_tail(" in transition
    assert '"known_dts_marker_count": known_dts_marker_count' in transition


def test_self_test_rejects_unaccounted_rotations_and_delivery_failures() -> None:
    loaded = load_self_test()
    require_recovery = loaded["require_accounted_downstream_recovery"]
    failure = loaded["TestFailure"]
    delivery = {
        "duplicate_publishers": False,
        "counter_regression": False,
        "invalid_samples": 0,
        "max_delivery_outage_seconds": 5.4,
        "final_active": True,
        "id_rotation_count": 1,
    }
    events = {"events_with_publisher_rotation": 1}
    require_recovery(delivery, events)
    for overrides in (
        {"duplicate_publishers": True},
        {"counter_regression": True},
        {"invalid_samples": 1},
        {"max_delivery_outage_seconds": 15.001},
        {"final_active": False},
        {"id_rotation_count": 2},
        {"id_rotation_count": 0},
    ):
        with pytest.raises(failure, match="automatic downstream RTMP recovery validation failed"):
            require_recovery({**delivery, **overrides}, events)
    source = SELF_TEST.read_text(encoding="utf-8")
    assert (
        "require_accounted_downstream_recovery(delivery_summary, event_delivery_summary)" in source
    )


def test_self_test_forces_exact_loopback_rtmp_disconnect_with_fixed_errors() -> None:
    loaded = load_self_test()
    kick = loaded["kick_test_sink_rtmp_connection"]
    failure = loaded["TestFailure"]
    connection_id = "12345678-1234-1234-1234-123456789abc"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return b""

    requests = []

    def urlopen(request, timeout: float):
        requests.append((request, timeout))
        return Response()

    with patch.object(loaded["urllib"].request, "urlopen", urlopen):
        kick(connection_id)
    assert len(requests) == 1
    assert requests[0][0].get_method() == "POST"
    assert requests[0][0].full_url.endswith(f"/v3/rtmpconns/kick/{connection_id}")
    assert requests[0][1] == 2

    with (
        patch.object(loaded["urllib"].request, "urlopen", side_effect=OSError(connection_id)),
        pytest.raises(failure) as captured,
    ):
        kick(connection_id)
    assert str(captured.value) == "test sink RTMP disconnect request failed"
    assert connection_id not in str(captured.value)
    with pytest.raises(failure, match="invalid test sink RTMP connection identity"):
        kick("../../untrusted")


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
    assert main_source.count("observer.checked_snapshot(") == 9
    assert 'observer.checked_snapshot("same-session resume baseline")' in main_source
    assert '"same-session LIVE recovery diagnosis"' in main_source
    assert "capture_observer = CaptureObserver(capture)" in main_source
    assert main_source.count("capture_observer.checked_samples_since(") == 8


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
    assert argv.count("-fflags") == 1
    assert argv[argv.index("-fflags") + 1] == "+genpts"
    assert argv.index("-fflags") < argv.index("-i")
    assert "-bsf:v" not in argv
    assert "-output_ts_offset" not in argv
    assert "-use_wallclock_as_timestamps" not in argv
    assert "-copyts" not in argv
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-copyinkf" not in argv
    assert "rtsp://127.0.0.1:18554/iphone-live" in argv
    assert "rtmp://127.0.0.1:11936/relay-output" in argv
    assert argv.count("-flush_packets") == 1
    assert "-tcp_nodelay" not in argv
    assert argv[-3:] == [
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

    assert loaded["VERIFIED_STALL_TIMEOUT_SECONDS"] == 0.50
    assert loaded["OUTPUT_IDLE_FALLBACK_SECONDS"] == 0.90
    assert loaded["REQUIRED_IDLE_OBSERVATIONS"] == 2
    assert loaded["REQUIRED_VERIFIED_STALL_OBSERVATIONS"] == 3
    assert loaded["METRICS_BLIND_TIMEOUT_SECONDS"] == 0.75
    assert (
        loaded["VERIFIED_STALL_TIMEOUT_SECONDS"]
        + (2 * loaded["MEDIA_POLL_INTERVAL_SECONDS"])
        + loaded["CHILD_STOP_GRACE_SECONDS"]
        + (1024 / 48000)
        < self_test["CAPTURE_NO_GROWTH_LIMIT_SECONDS"]
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
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.49) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.49, 1.491) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.552) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.552, 1.553) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.60) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.60, 1.601) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.61) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.61, 1.611) is False

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
    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.60) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.60, 1.601) is True
    assert watchdog.observe_output(True, ("normalizer-a", 121), 1.72) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 502), 1.72, 1.721) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    for observed_at, ingest_counter in (
        (1.05, 500),
        (1.15, 501),
        (1.25, 502),
        (1.35, 503),
        (1.45, 504),
        (1.55, 505),
        (1.899, 506),
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
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.901) == (False, False)

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.10, 1.30) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.56) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.56, 1.561) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.10) == (True, True)
    assert watchdog.observe_ingest(False, None, 1.10, 1.11) is True
    assert watchdog.ingest_counter is None
    assert watchdog.joint_idle_since is None
    assert watchdog.joint_unchanged_observations == 0
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.11) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.11, 1.111) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.55) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.55, 1.551) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.62) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.62, 1.621) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.05) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.05, 1.051) is True
    assert watchdog.observe_output(False, None, 1.10) == (True, False)
    assert watchdog.ingest_counter is None
    assert watchdog.joint_idle_since is None
    assert watchdog.joint_unchanged_observations == 0
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.11) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.11, 1.111) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.55) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.55, 1.551) is True
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.62) == (True, True)
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.62, 1.621) is False
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
    assert loaded["RESTART_REASON_VERIFIED_STALL"] not in loaded["SOURCE_RESET_ELIGIBLE_REASONS"]
    assert "make_parent_death_setup" in loaded

    supervisor_source = (
        NORMALIZER.read_text(encoding="utf-8")
        .split("def run_supervisor", 1)[1]
        .split("def main", 1)[0]
    )
    assert supervisor_source.count("ingest_reader.sample()") == 3
    assert "keep_child, probe_ingest = watchdog.observe_output(" in supervisor_source
    assert "if keep_child and probe_ingest:" in supervisor_source
    assert "ingest_started = time.monotonic()" in supervisor_source
    assert "ingest_finished = time.monotonic()" in supervisor_source
    assert "keep_child = watchdog.observe_ingest(" in supervisor_source
    watchdog_ready_at = supervisor_source.index("watchdog = MediaWatchdog(counter, now)")
    ingest_close_at = supervisor_source.index("ingest_reader.close()", watchdog_ready_at)
    assert watchdog_ready_at < ingest_close_at
    assert (
        "emit_state_event(STATE_EVENT_BRIDGE_ACTIVE)"
        in supervisor_source[watchdog_ready_at:ingest_close_at]
    )
    assert "if not probe_ingest:\n                ingest_reader.close()" in supervisor_source
    assert "if not keep_child:" in supervisor_source
    assert (
        "failure_reason = watchdog.failure_reason or RESTART_REASON_WATCHDOG_UNKNOWN"
        in supervisor_source
    )
    assert "recovery.record_failure(failure_reason, now)" in supervisor_source
    assert "emit_restart_reason(failure_reason)" in supervisor_source
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

    assert len(tokens) == 13
    assert tokens["ingest-confirmed-stall"] == (
        "moblin-relay-normalize:restart:ingest-confirmed-stall"
    )
    assert all(
        re.fullmatch(r"moblin-relay-normalize:restart:[a-z-]+", token) for token in tokens.values()
    )
    for reason, token in tokens.items():
        emit(reason)
        assert capsys.readouterr().err == token + "\n"

    with pytest.raises(ValueError, match="invalid normalizer restart reason"):
        emit("untrusted-value")
    assert capsys.readouterr().err == ""


def test_normalizer_bridge_active_diagnostic_is_fixed_and_secret_free(capsys) -> None:
    loaded = load_normalizer()
    emit = loaded["emit_state_event"]

    assert loaded["STATE_EVENT_TOKENS"] == {
        "bridge-active": "moblin-relay-normalize:state:bridge-active",
        "source-attached": "moblin-relay-normalize:state:source-attached",
        "source-detached": "moblin-relay-normalize:state:source-detached",
    }
    emit("bridge-active")
    assert capsys.readouterr().err == "moblin-relay-normalize:state:bridge-active\n"
    with pytest.raises(ValueError, match="invalid normalizer state event"):
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
    assert watchdog.observe_output(True, ("normalizer-a", 120), 1.901) == (False, False)
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
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.49, 1.491) is True
    assert watchdog.observe_ingest(True, ("ingest-a", 500), 1.552, 1.553) is False
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
            999,
            "timestamps",
        ),
        (
            "decoded_frames",
            "strict_presentation_timestamps_monotonic",
            False,
            "ts-v-order",
        ),
        (
            "decoded_frames",
            "presentation_frame_rate_matches",
            False,
            "ts-v-fps",
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
        "max_dts_gap_seconds": {0: 0.04, 1: 0.02},
        "max_sorted_pts_gap_seconds": {0: 0.04, 1: 0.02},
        "audio_video_duration_difference_seconds": 0.01,
        "audio_video_end_difference_seconds": 0.01,
    }
    decoded_frames = {
        "frame_count": 10,
        "presentation_timestamp_count": 10,
        "strict_presentation_timestamps_monotonic": True,
        "presentation_frame_rate_matches": True,
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
