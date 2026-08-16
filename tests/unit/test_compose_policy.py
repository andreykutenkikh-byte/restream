import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.runtime import MEDIAMTX_AUTH_TIMEOUT_SECONDS
from scripts.validate_production_compose import PolicyViolation, validate_service_security


def load_compose() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.yml").read_text(encoding="utf-8"))


def load_ci_override() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.ci.yml").read_text(encoding="utf-8"))


def load_production_override() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.production.yml").read_text(encoding="utf-8"))


def test_compose_project_is_isolated_and_bounded() -> None:
    compose = load_compose()
    assert compose["name"] == "adojapan-restream"
    services = compose["services"]
    assert set(services) == {"backend", "mediamtx"}
    for service in services.values():
        assert "container_name" not in service
        assert "expose" not in service
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
    assert services["backend"]["ports"] == ["127.0.0.1:${HTTP_PORT:-8088}:8000/tcp"]
    assert services["mediamtx"]["ports"] == [
        "${RTMP_BIND_ADDRESS:-127.0.0.1}:${PUBLIC_RTMP_PORT:-1935}:1935/tcp"
    ]
    assert "expose" not in services["mediamtx"]
    assert "8888" not in str(services["mediamtx"]["ports"])


def test_shared_host_production_override_is_minimal_and_bounded() -> None:
    override = load_production_override()
    assert set(override) == {"services"}
    assert set(override["services"]) == {"backend", "mediamtx"}

    backend = override["services"]["backend"]
    mediamtx = override["services"]["mediamtx"]
    assert set(backend) == {"cpus", "mem_limit", "pids_limit", "restart", "environment"}
    assert set(mediamtx) == {"cpus", "mem_limit", "pids_limit", "restart"}
    assert backend == {
        "cpus": "0.40",
        "mem_limit": "384m",
        "pids_limit": 96,
        "restart": "unless-stopped",
        "environment": {
            "ENVIRONMENT": "production",
            "COOKIE_SECURE": "true",
            "MAX_DESTINATIONS": "1",
            "PUBLIC_DOMAIN": "restream.adojapan.ru",
            "PUBLIC_RTMP_HOST": "restream.adojapan.ru",
            "PUBLIC_RTMP_PORT": "1935",
        },
    }
    assert mediamtx == {
        "cpus": "0.20",
        "mem_limit": "192m",
        "pids_limit": 64,
        "restart": "unless-stopped",
    }

    services = override["services"].values()
    assert sum(Decimal(service["cpus"]) for service in services) <= Decimal("0.60")
    assert sum(int(service["mem_limit"].removesuffix("m")) for service in services) <= 576
    assert sum(service["pids_limit"] for service in services) == 160


def test_production_validator_rejects_restart_policy_drift() -> None:
    service = {
        "read_only": True,
        "restart": "unless-stopped",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "healthcheck": {},
        "logging": {},
        "volumes": [],
    }

    validate_service_security(service, "backend")
    for invalid in (None, "no", "always", "on-failure:5"):
        with pytest.raises(PolicyViolation, match="backend_restart_policy"):
            validate_service_security({**service, "restart": invalid}, "backend")


def test_production_override_cannot_weaken_isolation_or_publish_extra_ports() -> None:
    override = load_production_override()
    forbidden_keys = {
        "ports",
        "expose",
        "networks",
        "network_mode",
        "volumes",
        "privileged",
        "security_opt",
        "cap_drop",
        "read_only",
    }
    for service in override["services"].values():
        assert forbidden_keys.isdisjoint(service)
        assert "/var/run/" + "docker.sock" not in str(service)

    base_services = load_compose()["services"]
    assert base_services["backend"]["ports"] == ["127.0.0.1:${HTTP_PORT:-8088}:8000/tcp"]
    assert base_services["mediamtx"]["ports"] == [
        "${RTMP_BIND_ADDRESS:-127.0.0.1}:${PUBLIC_RTMP_PORT:-1935}:1935/tcp"
    ]
    assert "ci-rtmp-receiver" not in override["services"]


def test_worker_auth_password_is_a_separate_required_compose_secret() -> None:
    root = Path(__file__).resolve().parents[2]
    backend_environment = load_compose()["services"]["backend"]["environment"]
    template = (root / ".env.example").read_text(encoding="utf-8")

    assert backend_environment["SESSION_SECRET"] == (
        "${SESSION_SECRET:?SESSION_SECRET is required}"
    )
    assert backend_environment["WORKER_AUTH_PASSWORD"] == (
        "${WORKER_AUTH_PASSWORD:?WORKER_AUTH_PASSWORD is required}"
    )
    assert backend_environment["SESSION_SECRET"] != backend_environment["WORKER_AUTH_PASSWORD"]
    assert "WORKER_AUTH_PASSWORD=REQUIRED_INDEPENDENT_RANDOM_VALUE" in template


def test_mediamtx_control_api_stays_internal_and_auth_is_delegated() -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8"))
    assert config["logLevel"] == "error"
    assert config["readTimeout"] == f"{int(MEDIAMTX_AUTH_TIMEOUT_SECONDS)}s"
    assert config["authMethod"] == "http"
    assert config["authHTTPAddress"] == "http://backend:8000/internal/mediamtx/auth"
    assert config["api"] is True
    assert config["rtmp"] is True
    assert config["rtsp"] is False
    assert config["hls"] is True
    assert config["hlsAddress"] == ":8888"
    assert config["webrtc"] is False
    assert config["srt"] is False

    services = load_compose()["services"]
    assert services["backend"]["environment"]["MEDIAMTX_HLS_URL"] == ("http://mediamtx:8888")
    assert "internal" in services["backend"]["networks"]
    assert "internal" in services["mediamtx"]["networks"]


def test_docker_build_context_excludes_generated_environment_files() -> None:
    root = Path(__file__).resolve().parents[2]
    patterns = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns


def test_ci_media_helpers_are_absent_from_production_and_strictly_isolated() -> None:
    for service_name in ("ci-rtmp-receiver", "ci-rtmp-publisher"):
        assert service_name not in load_compose()["services"]
        assert service_name not in load_production_override()["services"]

    override = load_ci_override()
    receiver = override["services"]["ci-rtmp-receiver"]
    assert "ports" not in receiver
    assert receiver["networks"] == ["internal"]
    assert receiver["read_only"] is True
    assert receiver["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in receiver["security_opt"]
    assert receiver["cpus"] and receiver["mem_limit"] and receiver["pids_limit"]
    assert receiver["restart"] == "no"
    assert receiver["logging"]["options"] == {"max-size": "10m", "max-file": "3"}

    publisher = override["services"]["ci-rtmp-publisher"]
    assert "ports" not in publisher
    assert "environment" not in publisher
    assert publisher["networks"] == ["internal"]
    assert publisher["read_only"] is True
    assert publisher["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in publisher["security_opt"]
    assert publisher["cpus"] and publisher["mem_limit"] and publisher["pids_limit"]
    assert publisher["restart"] == "no"
    assert publisher["logging"]["options"] == {"max-size": "10m", "max-file": "3"}

    backend = override["services"]["backend"]
    mediamtx = override["services"]["mediamtx"]
    assert backend["restart"] == "no"
    assert mediamtx == {"restart": "no"}
    assert backend["environment"] == {
        "ENVIRONMENT": "test",
        "COOKIE_SECURE": "false",
        "TEST_DESTINATION_ALLOWLIST": "rtmp://ci-rtmp-receiver:1935/ci-output",
    }
    assert backend["depends_on"]["ci-rtmp-receiver"] == {"condition": "service_healthy"}


def test_ci_override_is_the_only_compose_test_mode_and_allowlist_source() -> None:
    root = Path(__file__).resolve().parents[2]
    compose_files = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in root.glob("compose*.yml")
    }
    test_mode_files = {
        name
        for name, document in compose_files.items()
        if document["services"].get("backend", {}).get("environment", {}).get("ENVIRONMENT")
        == "test"
    }
    allowlist_files = {
        name
        for name, document in compose_files.items()
        if "TEST_DESTINATION_ALLOWLIST"
        in document["services"].get("backend", {}).get("environment", {})
    }

    assert test_mode_files == {"compose.ci.yml"}
    assert allowlist_files == {"compose.ci.yml"}


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
    base_validation = (
        "docker compose -p adojapan-restream --env-file .env.ci -f compose.yml config --quiet"
    )
    production_validation = (
        "docker compose -p adojapan-restream --env-file .env.ci -f compose.yml "
        "-f compose.production.yml config --quiet"
    )
    production_effective_validation = (
        "docker compose -p adojapan-restream --env-file .env.ci -f compose.yml "
        "-f compose.production.yml config --format json"
    )
    runtime_files = "-f compose.yml -f compose.production.yml -f compose.ci.yml"
    assert base_validation in runtime_commands
    assert production_validation in runtime_commands
    assert all(
        line in {base_validation, production_validation}
        or line.startswith(production_effective_validation)
        or runtime_files in line
        for line in runtime_commands
    )
    assert any(runtime_files in line and " config --quiet" in line for line in runtime_commands)
    assert any(runtime_files in line and line.endswith(" build") for line in runtime_commands)
    assert any(runtime_files in line and " up -d --wait" in line for line in runtime_commands)
    assert any(runtime_files in line and " logs --tail=100" in line for line in runtime_commands)
    assert any(
        runtime_files in line and " down --remove-orphans --volumes" in line
        for line in runtime_commands
    )
    assert "scripts/validate_production_compose.py" in workflow
    assert "sh scripts/check_runtime_limits.sh" in workflow
    assert "node --check app/static/app.js" in workflow
    assert "node --check app/static/preview-player.js" in workflow
    assert "node --test tests/frontend/preview-player.test.js" in workflow
    assert "Real RTMP rotation and output end-to-end smoke" in workflow
    assert "python scripts/ci_output_smoke.py" in workflow
    assert "if: always()" in workflow
    assert "down --remove-orphans --volumes" in workflow
    assert "cleanup_status=$?" in workflow
    assert 'exit "$cleanup_status"' in workflow
    assert "rm -f .env.ci" in workflow


def test_ci_runtime_limits_and_destination_limit_are_exercised() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_check = (root / "scripts" / "check_runtime_limits.sh").read_text(encoding="utf-8")
    smoke = (root / "scripts" / "ci_output_smoke.py").read_text(encoding="utf-8")
    runtime_files = "-f compose.yml -f compose.production.yml -f compose.ci.yml"

    assert runtime_files in runtime_check
    assert "docker inspect --format" in runtime_check
    for field in (
        ".HostConfig.NanoCpus",
        ".HostConfig.Memory",
        ".HostConfig.PidsLimit",
        ".HostConfig.RestartPolicy.Name",
        ".State.Status",
        ".State.Health.Status",
        ".RestartCount",
        ".State.OOMKilled",
    ):
        assert field in runtime_check
    assert ".Config.Env" not in runtime_check
    assert "400000000 402653184 96" in runtime_check
    assert "200000000 201326592 64" in runtime_check
    assert 'expected="$2 $3 $4 no running healthy 0 false"' in runtime_check

    compose_definition = smoke.split("COMPOSE = (", maxsplit=1)[1].split(")", maxsplit=1)[0]
    assert compose_definition.count('"compose.yml"') == 1
    assert compose_definition.count('"compose.production.yml"') == 1
    assert compose_definition.count('"compose.ci.yml"') == 1
    assert (
        compose_definition.index('"compose.yml"')
        < compose_definition.index('"compose.production.yml"')
        < compose_definition.index('"compose.ci.yml"')
    )
    assert "expected=(409,)" in smoke
    assert "destination_limit_reached" in smoke
    assert 'first_after_limit.get("state") != "live"' in smoke
    assert "assert_hls_port_is_internal()" in smoke
    assert "fetch_preview_segment(client, ingest_key)" in smoke
    assert "sample_active_preview_usage(client, preview.media_playlist_path)" in smoke
    assert "service=PUBLISHER_SERVICE" in smoke
    assert "expected=(401,)" in smoke
    assert "expected=(404, 409)" in smoke
    assert "{{.CPUPerc}}|{{.MemUsage}}" in smoke
    assert 'item.get("bitrate_bps") is None' in smoke
    assert smoke.count("rotate_and_confirm_ingest_key(") == 3
    assert '"/api/ingest/rotate"' in smoke
    assert "active publisher termination after key rotation" in smoke
    assert "publisher rejection with the rotated ingest key" in smoke
    assert "publisher process with the replacement ingest key" in smoke
    assert "publisher_is_absent" in smoke


def test_production_scripts_always_use_shared_host_override() -> None:
    root = Path(__file__).resolve().parents[2]
    required_files = "-f compose.yml -f compose.production.yml"
    compose_command = "docker " + "compose"
    for name in ("start.sh", "stop.sh", "rollback.sh"):
        script = (root / "scripts" / name).read_text(encoding="utf-8")
        compose_lines = [line for line in script.splitlines() if compose_command in line]
        assert compose_lines
        assert all(required_files in line for line in compose_lines)
        assert "compose.ci.yml" not in script


def test_ci_targets_main_and_third_party_actions_are_commit_pinned() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "branches: [main]" in workflow
    action_lines = [line.strip() for line in workflow.splitlines() if "uses: actions/" in line]
    assert action_lines
    assert all(re.search(r"@[0-9a-f]{40}(?:\s+#\s+v\d[^\s]*)?$", line) for line in action_lines)
