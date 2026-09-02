from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_nginx_media_body_override_is_exact_and_does_not_raise_global_limit() -> None:
    nginx = (ROOT / "deploy" / "nginx-restream.conf.example").read_text(encoding="utf-8")

    assert nginx.count("client_max_body_size 1m;") == 1
    assert nginx.count("client_max_body_size 3m;") == 1
    media_location = nginx.split('location ~ "^/relay-media/v1/preview/segments/', 1)[1]
    media_location = media_location.split("\n    }", 1)[0]
    assert "proxy_request_buffering off;" in media_location
    assert "proxy_buffering off;" in media_location
    assert "/relay-agent/" not in media_location


def test_agent_preview_credential_is_optional_and_never_an_argument_or_environment() -> None:
    unit = (ROOT / "deploy" / "hk-relay-agent" / "adojapan-relay-agent.service").read_text(
        encoding="utf-8"
    )
    entry = (ROOT / "relay_agent" / "__main__.py").read_text(encoding="utf-8")
    installer = (ROOT / "deploy" / "hk-relay-agent" / "install-preview-token.py").read_text(
        encoding="utf-8"
    )

    assert "-/etc/adojapan-relay-agent/preview-reader.token" in unit
    assert "preview-reader.token" not in unit.split("ExecStart=", 1)[1].splitlines()[0]
    assert "Environment=" not in unit
    assert 'PREVIEW_TOKEN_PATH = Path("/etc/adojapan-relay-agent/preview-reader.token")' in entry
    assert 'arguments not in ([], ["--generate"])' in installer
    assert "secrets.token_urlsafe(48)" in installer
    assert "print(value)" not in installer
    assert 'print("Preview reader credential saved successfully")' in installer
    assert "getpass.getpass" in installer
    assert 'SERVICE_UNITS = ("moblin-relay.service", "adojapan-relay-agent.service")' in installer
    assert 'active_state not in {"inactive", "failed"}' in installer
    assert "int(main_pid) != 0" in installer
    assert "os.fchmod(fd, 0o600)" in installer
    assert "os.fchown(fd, account.pw_uid, account.pw_gid)" in installer
    assert "os.replace(temporary, TARGET)" in installer
    assert "adojapan-relay-install-preview-token" in (
        ROOT / "deploy" / "hk-relay-agent" / "install.sh"
    ).read_text(encoding="utf-8")


def test_media_endpoint_is_separate_from_small_control_plane_limit() -> None:
    api = (ROOT / "app" / "relay_preview_api.py").read_text(encoding="utf-8")
    middleware = (ROOT / "app" / "node_api.py").read_text(encoding="utf-8")

    assert '"/relay-media/v1/preview/segments/{generation}/{sequence}"' in api
    assert 'startswith(("/node-api/", "/relay-agent/"))' in middleware
    assert "MAX_NODE_BODY_BYTES = 16 * 1024" in middleware
