from pathlib import Path

import yaml


def load_compose() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.yml").read_text(encoding="utf-8"))


def test_compose_project_is_isolated_and_bounded() -> None:
    compose = load_compose()
    assert compose["name"] == "adojapan-restream"
    services = compose["services"]
    assert set(services) == {"backend", "mediamtx"}
    for service in services.values():
        assert "container_name" not in service
        assert "network_mode" not in service
        assert service.get("privileged") is not True
        assert service["cpus"]
        assert service["mem_limit"]
        assert service["pids_limit"]
        assert service["healthcheck"]
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
        assert service["restart"] == "on-failure:5"
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["cap_drop"] == ["ALL"]
        docker_socket = "/var/run/" + "docker.sock"
        assert docker_socket not in str(service)

    assert set(services["mediamtx"]["networks"]) == {"internal", "ingest"}
    assert set(services["backend"]["networks"]) == {"internal", "egress"}
    assert compose["networks"]["internal"]["internal"] is True
    assert compose["networks"]["ingest"].get("internal", False) is False
    assert set(compose["volumes"]) == {"database", "logs", "backups"}


def test_only_web_loopback_and_rtmp_are_published() -> None:
    services = load_compose()["services"]
    assert services["backend"]["ports"] == [
        "${HTTP_BIND_ADDRESS:-127.0.0.1}:${HTTP_PORT:-8088}:8000/tcp"
    ]
    assert services["mediamtx"]["ports"] == [
        "${RTMP_BIND_ADDRESS:-127.0.0.1}:${PUBLIC_RTMP_PORT:-1935}:1935/tcp"
    ]


def test_mediamtx_control_api_stays_internal_and_auth_is_delegated() -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8"))
    assert config["logLevel"] == "error"
    assert config["authMethod"] == "http"
    assert config["authHTTPAddress"] == "http://backend:8000/internal/mediamtx/auth"
    assert config["api"] is True
    assert config["rtmp"] is True
    assert config["rtsp"] is False
    assert config["hls"] is False
    assert config["webrtc"] is False
    assert config["srt"] is False
