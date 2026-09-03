from __future__ import annotations

import ast
import os
import runpy
import sys
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
    assert capsys.readouterr() == ("", "")


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
    assert 'f"&payloadsize={SRT_PAYLOAD_SIZE}&conntimeo=3000"' in source
    assert 'f"&passphrase={passphrase}&pbkeylen=32&latency={SRT_LATENCY_MILLISECONDS}"' in source
    direct_stages = (
        "startup",
        "assets",
        "topology",
        "auth",
        "auth-source",
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
        "stall-live",
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
        "ts-video-pts",
        "ts-audio-pts",
        "ts-gaps",
        "ts-av-sync",
    )
    dynamic_exclusivity_stages = (
        "auth-x-live",
        "auth-x-ingest",
        "auth-x-norm",
        "auth-x-sink",
        "auth-x-bytes",
    )
    for stage in direct_stages:
        assert len(f"{stage}\n".encode("ascii")) <= 16
        assert f'mark_self_test_stage("{stage}")' in source
    for stage in timestamp_diagnostic_stages:
        assert len(f"{stage}\n".encode("ascii")) <= 16
        assert f'("{stage}",' in source
    for stage in dynamic_exclusivity_stages:
        assert len(f"{stage}\n".encode("ascii")) <= 16
        assert f'return "{stage}"' in source
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
    assert "signal.SIGSTOP" in outage_block
    assert "signal.SIGCONT" in outage_block
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
        "publisher = start_local_publisher(SOURCE_PRIMARY_RTMP_PORT)"
    )

    helper_block = source.split("def stop_primary_srt_source", 1)[1].split(
        "def reject_with_helper", 1
    )[0]
    assert helper_block.index("safe_stop(primary_helper, force=True)") < helper_block.index(
        "safe_stop(publisher, force=True)"
    )
    assert "primary_helper = None" in helper_block
    assert "publisher = None" in helper_block
    assert "wait_ports_released((" in helper_block
    assert '("tcp", SOURCE_PRIMARY_RTMP_PORT)' in helper_block
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
        'normalized_video.get("codec_name")'
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


def test_self_test_dut_log_marker_is_scoped_to_appended_tail(tmp_path: Path) -> None:
    loaded = load_self_test()
    open_tail = loaded["open_validated_log_tail"]
    contains_marker = loaded["log_tail_contains_marker"]
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
        finally:
            os.close(descriptor)

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
    assert main_source.count("observer.checked_snapshot(") == 4
    assert "capture_observer = CaptureObserver(capture)" in main_source
    assert main_source.count("capture_observer.checked_samples_since(") == 5


def test_normalizer_uses_a_secret_free_liveness_supervisor() -> None:
    loaded = load_normalizer()
    self_test = load_self_test()
    build_argv = loaded["build_ffmpeg_argv"]
    parse_inbound_bytes = loaded["parse_inbound_bytes"]
    parse_output_sample = loaded["parse_output_sample"]
    growth_gate = loaded["GrowthGate"]
    output_growth_gate = loaded["OutputGrowthGate"]
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

    metric = 'paths_inbound_bytes{state="ready",name="iphone-live"} 100\n'
    assert parse_inbound_bytes(metric) == 100
    assert parse_inbound_bytes(metric + metric) is None
    assert parse_inbound_bytes('paths_inbound_bytes{name="other",state="ready"} 100\n') is None
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

    output_gate = output_growth_gate()
    assert output_gate.observe(("normalizer-a", 100)) is False
    assert output_gate.observe(("normalizer-a", 110)) is False
    assert output_gate.observe(("normalizer-a", 120)) is True
    assert output_gate.observe(("normalizer-b", 130)) is False

    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe(True, ("normalizer-a", 120), 1.499) is True
    assert watchdog.observe(True, ("normalizer-a", 120), 1.501) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe(False, None, 1.749) is True
    assert watchdog.observe(False, None, 1.751) is False
    watchdog = watchdog_type(("normalizer-a", 120), 1.0)
    assert watchdog.observe(True, ("normalizer-b", 121), 1.01) is False
    assert "make_parent_death_setup" in loaded

    assert sanitized_environment(18554, 11936, 19998) == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "MOBLIN_RELAY_INTERNAL_RTSP_PORT": "18554",
        "MOBLIN_RELAY_INTERNAL_RTMP_PORT": "11936",
        "MOBLIN_RELAY_INTERNAL_METRICS_PORT": "19998",
    }


def test_normalizer_reexec_discards_hook_secrets() -> None:
    loaded = load_normalizer()
    main = loaded["main"]
    captured: dict[str, object] = {}
    marker = "must-not-survive-reexec"

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, environment=environment)
        raise OSError("stop before exec")

    hook_environment = {
        "MTX_PATH": "iphone-live",
        "MTX_QUERY": f"publisher={marker}",
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
    }


@pytest.mark.parametrize(
    ("scope", "field", "value", "expected"),
    [
        ("timestamps", "ffprobe_exit", 1, "ts-probe-pts"),
        ("timestamps", "dts_within_tolerance", False, "ts-packet-dts"),
        (
            "decoded_frames",
            "strict_presentation_timestamps_monotonic",
            False,
            "ts-video-pts",
        ),
        (
            "decoded_audio",
            "presentation_timestamp_steps_beyond_tolerance",
            1,
            "ts-audio-pts",
        ),
        ("timestamps", "max_dts_gap_seconds", {0: 3.0, 1: 0.02}, "ts-gaps"),
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
