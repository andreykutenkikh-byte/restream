from __future__ import annotations

import ast
import runpy
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "moblin-relay"
RELAYCTL = BUNDLE / "relayctl"


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
    for stage in (
        "startup",
        "assets",
        "topology",
        "auth",
        "outages",
        "continuity",
        "decode",
        "secrets",
        "cleanup",
    ):
        assert f'mark_self_test_stage("{stage}")' in source
