import re
from pathlib import Path

import yaml


def load_compose() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.yml").read_text(encoding="utf-8"))


def load_ci_override() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.ci.yml").read_text(encoding="utf-8"))


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


def test_worker_auth_password_is_a_separate_required_compose_secret() -> None:
    root = Path(__file__).resolve().parents[2]
    backend_environment = load_compose()["services"]["backend"]["environment"]
    template = (root / ".env.example").read_text(encoding="utf-8")

    assert backend_environment["WORKER_AUTH_PASSWORD"] == (
        "${WORKER_AUTH_PASSWORD:?WORKER_AUTH_PASSWORD is required}"
    )
    assert "WORKER_AUTH_PASSWORD=REQUIRED_INDEPENDENT_RANDOM_VALUE" in template


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


def test_docker_build_context_excludes_generated_environment_files() -> None:
    root = Path(__file__).resolve().parents[2]
    patterns = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns


def test_ci_receiver_is_absent_from_production_compose_and_strictly_isolated() -> None:
    assert "ci-rtmp-receiver" not in load_compose()["services"]

    override = load_ci_override()
    receiver = override["services"]["ci-rtmp-receiver"]
    assert "ports" not in receiver
    assert receiver["networks"] == ["internal"]
    assert receiver["read_only"] is True
    assert receiver["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in receiver["security_opt"]
    assert receiver["cpus"] and receiver["mem_limit"] and receiver["pids_limit"]
    assert receiver["logging"]["options"] == {"max-size": "10m", "max-file": "3"}

    backend = override["services"]["backend"]
    assert backend["environment"]["TEST_DESTINATION_ALLOWLIST"] == (
        "rtmp://ci-rtmp-receiver:1935/ci-output"
    )
    assert backend["depends_on"]["ci-rtmp-receiver"] == {"condition": "service_healthy"}


def test_ci_receiver_accepts_only_the_exact_smoke_path() -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "mediamtx" / "ci-receiver.yml").read_text(encoding="utf-8"))

    assert config["logLevel"] == "error"
    assert config["paths"] == {"all_others": {}}
    assert config["api"] is True
    assert config["rtmp"] is True
    assert config["rtsp"] is False
    assert config["hls"] is False
    assert config["webrtc"] is False
    assert config["srt"] is False
    permissions = config["authInternalUsers"][0]["permissions"]
    assert {item["action"] for item in permissions} == {"publish", "read", "api"}
    assert all(
        item.get("path") == "ci-output/ci-e2e"
        for item in permissions
        if item["action"] in {"publish", "read"}
    )


def test_ci_runtime_always_uses_test_override_and_cleans_up() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runtime_commands = [
        line.strip()
        for line in workflow.splitlines()
        if "docker compose -p adojapan-restream" in line
    ]

    assert runtime_commands
    production_validation = (
        "docker compose -p adojapan-restream --env-file .env.ci -f compose.yml config --quiet"
    )
    assert production_validation in runtime_commands
    assert all(
        line == production_validation or "-f compose.yml -f compose.ci.yml" in line
        for line in runtime_commands
    )
    assert "Real RTMP output end-to-end smoke" in workflow
    assert "python scripts/ci_output_smoke.py" in workflow
    assert "if: always()" in workflow
    assert "down --remove-orphans --volumes" in workflow
    assert "cleanup_status=$?" in workflow
    assert 'exit "$cleanup_status"' in workflow
    assert "rm -f .env.ci" in workflow


def test_ci_targets_main_and_third_party_actions_are_commit_pinned() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "branches: [main]" in workflow
    action_lines = [line.strip() for line in workflow.splitlines() if "uses: actions/" in line]
    assert action_lines
    assert all(re.search(r"@[0-9a-f]{40}(?:\s+#\s+v\d[^\s]*)?$", line) for line in action_lines)
