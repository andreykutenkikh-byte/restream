import re
import shutil
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.validate_production_compose import PolicyViolation, validate


def load_compose() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.yml").read_text(encoding="utf-8"))


def load_ci_override() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.ci.yml").read_text(encoding="utf-8"))


def load_production_override() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "compose.production.yml").read_text(encoding="utf-8"))


def resolved_production_model() -> dict[str, Any]:
    model = deepcopy(load_compose())
    override = load_production_override()
    for service_name, service_override in override["services"].items():
        service = model["services"][service_name]
        for key, value in service_override.items():
            if key == "environment":
                service["environment"].update(value)
            else:
                service[key] = value

    environment = model["services"]["backend"]["environment"]
    environment.update(
        {
            "SESSION_SECRET": "s" * 32,
            "WORKER_AUTH_PASSWORD": "w" * 32,
            "BOOTSTRAP_WORKER_SECRET": "b" * 32,
            "NODE_AGENT_IMAGE": ("ghcr.io/andreykutenkikh-byte/restream-node@sha256:" + "a" * 64),
        }
    )
    model["services"]["backend"]["ports"] = [
        {"host_ip": "127.0.0.1", "target": 8000, "published": 8088}
    ]
    model["services"]["mediamtx"]["ports"] = [
        {"host_ip": "127.0.0.1", "target": 1935, "published": 1935}
    ]
    model["services"]["backend"]["volumes"] = [
        {"type": "volume", "source": "database", "target": "/srv/app/data"},
        {"type": "volume", "source": "logs", "target": "/srv/app/logs"},
        {"type": "volume", "source": "backups", "target": "/srv/app/backups"},
        {
            "type": "volume",
            "source": "bootstrap_socket",
            "target": "/run/adojapan-bootstrap",
            "read_only": True,
        },
    ]
    model["services"]["bootstrap"]["volumes"] = [
        {
            "type": "volume",
            "source": "bootstrap_socket",
            "target": "/run/adojapan-bootstrap",
        }
    ]
    model["services"]["bootstrap"]["secrets"] = [
        {"source": "bootstrap_worker_secret", "target": "bootstrap_worker_secret"}
    ]
    return model


def test_compose_project_is_isolated_and_bounded() -> None:
    compose = load_compose()
    assert compose["name"] == "adojapan-restream"
    services = compose["services"]
    assert set(services) == {"backend", "mediamtx", "bootstrap"}
    for service in services.values():
        assert "container_name" not in service
        assert "expose" not in service
        assert "network_mode" not in service
        assert service.get("privileged") is not True
        assert service["read_only"] is True
        assert service["tmpfs"]
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
    assert services["bootstrap"]["networks"] == ["bootstrap-egress"]
    assert compose["networks"]["internal"]["internal"] is True
    assert compose["networks"]["ingest"].get("internal", False) is False
    assert compose["networks"]["bootstrap-egress"] == {"driver": "bridge"}
    assert set(compose["volumes"]) == {"database", "logs", "backups", "bootstrap_socket"}
    assert compose["secrets"] == {
        "bootstrap_worker_secret": {"environment": "BOOTSTRAP_WORKER_SECRET"}
    }


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
    assert set(override["services"]) == {"backend", "mediamtx", "bootstrap"}

    backend = override["services"]["backend"]
    mediamtx = override["services"]["mediamtx"]
    bootstrap = override["services"]["bootstrap"]
    assert set(backend) == {"cpus", "mem_limit", "pids_limit", "environment", "restart"}
    assert set(mediamtx) == {"cpus", "mem_limit", "pids_limit", "restart"}
    assert set(bootstrap) == {"cpus", "mem_limit", "pids_limit", "environment", "restart"}
    assert backend == {
        "cpus": "0.40",
        "mem_limit": "384m",
        "pids_limit": 96,
        "environment": {
            "ENVIRONMENT": "production",
            "COOKIE_SECURE": "true",
            "MAX_DESTINATIONS": "1",
            "PUBLIC_DOMAIN": "restream.adojapan.ru",
            "PUBLIC_RTMP_HOST": "restream.adojapan.ru",
            "PUBLIC_RTMP_PORT": "1935",
            "PUBLIC_CONTROL_URL": "https://restream.adojapan.ru",
            "NODE_AGENT_IMAGE": "${NODE_AGENT_IMAGE:?NODE_AGENT_IMAGE is required in production}",
            "NODE_PROTOCOL_VERSION": "1",
        },
        "restart": "unless-stopped",
    }
    assert mediamtx == {
        "cpus": "0.20",
        "mem_limit": "192m",
        "pids_limit": 64,
        "restart": "unless-stopped",
    }
    assert bootstrap == {
        "cpus": "0.10",
        "mem_limit": "128m",
        "pids_limit": 64,
        "environment": {"ENVIRONMENT": "production"},
        "restart": "unless-stopped",
    }

    services = override["services"].values()
    assert sum(Decimal(service["cpus"]) for service in services) == Decimal("0.70")
    assert sum(int(service["mem_limit"].removesuffix("m")) for service in services) == 704
    assert sum(service["pids_limit"] for service in services) == 224


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


def test_bootstrap_worker_is_uds_only_and_has_no_main_storage_or_networks() -> None:
    compose = load_compose()
    services = compose["services"]
    bootstrap = services["bootstrap"]
    backend = services["backend"]

    assert bootstrap["build"] == {"context": ".", "dockerfile": "Dockerfile.bootstrap"}
    assert bootstrap["init"] is True
    assert "ports" not in bootstrap
    assert "expose" not in bootstrap
    assert bootstrap["networks"] == ["bootstrap-egress"]
    assert bootstrap["volumes"] == ["bootstrap_socket:/run/adojapan-bootstrap"]
    assert bootstrap["secrets"] == ["bootstrap_worker_secret"]
    assert bootstrap["environment"] == {
        "ENVIRONMENT": "${ENVIRONMENT:-development}",
        "BOOTSTRAP_SOCKET_PATH": "/run/adojapan-bootstrap/bootstrap.sock",
        "BOOTSTRAP_SECRET_FILE": "/run/secrets/bootstrap_worker_secret",
        "BOOTSTRAP_MAX_ACTIVE_JOBS": "1",
        "BOOTSTRAP_JOB_TTL_SECONDS": "1200",
    }
    assert bootstrap["stop_grace_period"] == "90s"
    assert "BOOTSTRAP_WORKER_SECRET" not in bootstrap["environment"]
    assert backend["volumes"].count("bootstrap_socket:/run/adojapan-bootstrap:ro") == 1
    for forbidden in ("database", "logs", "backups", "/var/run/" + "docker.sock"):
        assert forbidden not in str(bootstrap)
    assert "bootstrap-egress" not in backend["networks"]
    assert "bootstrap-egress" not in services["mediamtx"]["networks"]

    root = Path(__file__).resolve().parents[2]
    for dockerfile_name in ("Dockerfile", "Dockerfile.bootstrap"):
        dockerfile = (root / dockerfile_name).read_text(encoding="utf-8")
        assert "install -d -o 10001 -g 10001 -m 0700 /run/adojapan-bootstrap" in dockerfile


def test_resolved_production_policy_accepts_only_the_exact_bootstrap_boundary() -> None:
    validate(resolved_production_model())

    mutations = []

    extra_network = resolved_production_model()
    extra_network["services"]["bootstrap"]["networks"].append("internal")
    mutations.append(extra_network)

    blocked_egress = resolved_production_model()
    blocked_egress["networks"]["bootstrap-egress"]["internal"] = True
    mutations.append(blocked_egress)

    writable_backend_socket = resolved_production_model()
    writable_backend_socket["services"]["backend"]["volumes"][-1]["read_only"] = False
    mutations.append(writable_backend_socket)

    mutable_agent_image = resolved_production_model()
    mutable_agent_image["services"]["backend"]["environment"]["NODE_AGENT_IMAGE"] = (
        "ghcr.io/andreykutenkikh-byte/restream-node:latest"
    )
    mutations.append(mutable_agent_image)

    short_bootstrap_grace = resolved_production_model()
    short_bootstrap_grace["services"]["bootstrap"]["stop_grace_period"] = "20s"
    mutations.append(short_bootstrap_grace)

    reused_secret = resolved_production_model()
    reused_secret["services"]["backend"]["environment"]["BOOTSTRAP_WORKER_SECRET"] = reused_secret[
        "services"
    ]["backend"]["environment"]["SESSION_SECRET"]
    mutations.append(reused_secret)

    extra_service = resolved_production_model()
    extra_service["services"]["unexpected"] = deepcopy(extra_service["services"]["bootstrap"])
    mutations.append(extra_service)

    for model in mutations:
        with pytest.raises(PolicyViolation):
            validate(model)


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
    assert backend_environment["BOOTSTRAP_WORKER_SECRET"] == (
        "${BOOTSTRAP_WORKER_SECRET:?BOOTSTRAP_WORKER_SECRET is required}"
    )
    assert backend_environment["SESSION_SECRET"] != backend_environment["WORKER_AUTH_PASSWORD"]
    assert backend_environment["BOOTSTRAP_WORKER_SECRET"] not in {
        backend_environment["SESSION_SECRET"],
        backend_environment["WORKER_AUTH_PASSWORD"],
    }
    assert "WORKER_AUTH_PASSWORD=REQUIRED_INDEPENDENT_RANDOM_VALUE" in template
    assert "BOOTSTRAP_WORKER_SECRET=REQUIRED_THIRD_RANDOM_VALUE" in template
    assert "NODE_AGENT_IMAGE=" in template
    assert "@sha256:" in template
    assert "PUBLIC_CONTROL_URL=" in template


def test_mediamtx_control_api_stays_internal_and_auth_is_delegated() -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8"))
    assert config["logLevel"] == "error"
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
    assert backend["environment"] == {
        "ENVIRONMENT": "test",
        "COOKIE_SECURE": "false",
        "NODE_AGENT_IMAGE": ("ghcr.io/adojapan/ci-node-agent@sha256:" + "1" * 64),
        "PUBLIC_CONTROL_URL": "http://backend:8000",
        "TEST_DESTINATION_ALLOWLIST": "rtmp://ci-rtmp-receiver:1935/ci-output",
    }
    assert backend["depends_on"]["ci-rtmp-receiver"] == {"condition": "service_healthy"}
    assert backend["restart"] == "no"
    assert override["services"]["mediamtx"] == {"restart": "no"}
    assert override["services"]["bootstrap"] == {
        "environment": {
            "ENVIRONMENT": "test",
            "TEST_SSH_TARGET_ALLOWLIST": "ci-ssh-target:22",
        },
        "restart": "no",
    }


def test_ci_ssh_and_real_agent_fixtures_are_internal_and_non_production() -> None:
    base_services = load_compose()["services"]
    production_services = load_production_override()["services"]
    for service_name in ("ci-ssh-target", "ci-node-agent"):
        assert service_name not in base_services
        assert service_name not in production_services

    override = load_ci_override()
    services = override["services"]
    target = services["ci-ssh-target"]
    agent = services["ci-node-agent"]

    assert "ports" not in target
    assert target["networks"] == ["bootstrap-egress"]
    assert target.get("privileged") is not True
    assert target["read_only"] is True
    assert "/var/run/" + "docker.sock" not in str(target)
    assert target["restart"] == "no"
    # The worker accepts this exact empty mountpoint only in its explicit test
    # mode; production still rejects every unmarked pre-existing directory.
    assert target["volumes"] == ["ci_node_data:/opt/adojapan-restream-node"]
    assert target["logging"]["options"] == {"max-size": "10m", "max-file": "3"}

    assert "ports" not in agent
    assert agent["networks"] == ["internal"]
    assert agent["read_only"] is True
    assert agent["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in agent["security_opt"]
    assert agent["restart"] == "no"
    assert agent["cpus"] == "0.25"
    assert agent["mem_limit"] == "256m"
    assert agent["pids_limit"] == 128
    assert agent["volumes"] == ["ci_node_data:/mnt/node"]
    assert agent["environment"] == {
        "NODE_AGENT_ENVIRONMENT": "test",
        "NODE_CONTROL_URL": "http://backend:8000",
        "NODE_DATA_DIR": "/mnt/node/data",
        "NODE_COMMAND_WAIT_SECONDS": "2",
    }
    assert set(override["volumes"]) == {"ci_node_data"}


def test_ci_ssh_target_emulates_only_exact_docker_package_queries() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "ci" / "ssh-target" / "Dockerfile").read_text(encoding="utf-8")
    shim = root / "ci" / "ssh-target" / "fake-dpkg-query"
    source = shim.read_text(encoding="utf-8")

    assert "COPY ci/ssh-target/fake-dpkg-query /usr/local/bin/dpkg-query" in dockerfile
    assert 'ENV PATH="/usr/local/bin:${PATH}"' in dockerfile
    assert "/usr/local/bin/dpkg-query" in dockerfile
    assert "docker-ce docker-ce-cli containerd.io docker-compose-plugin" in source
    assert "docker.io containerd runc podman-docker" in source
    assert "exit 64" in source

    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX shell is unavailable on this unit-test host")
    format_argument = r"-f=${db:Status-Abbrev}\n"
    official = subprocess.run(  # noqa: S603 - fixed local test fixture
        [
            shell,
            str(shim),
            "-W",
            format_argument,
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-compose-plugin",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    forbidden = subprocess.run(  # noqa: S603 - fixed local test fixture
        [
            shell,
            str(shim),
            "-W",
            format_argument,
            "docker.io",
            "containerd",
            "runc",
            "podman-docker",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    unexpected = subprocess.run(  # noqa: S603 - fixed local test fixture
        [shell, str(shim), "-W", format_argument, "docker-ce"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert official.returncode == 0
    assert official.stdout.splitlines() == ["ii "] * 4
    assert forbidden.returncode == 1
    assert forbidden.stdout == ""
    assert unexpected.returncode == 64


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
    assert "node --check app/static/servers.js" in workflow
    assert "tests/frontend/preview-player.test.js tests/frontend/servers.test.js" in workflow
    assert "Real RTMP output end-to-end smoke" in workflow
    assert "python scripts/ci_output_smoke.py" in workflow
    assert "SSH bootstrap and Node Agent end-to-end smoke" in workflow
    assert "python scripts/ci_node_onboarding_smoke.py" in workflow
    assert "Post-onboarding runtime limits" in workflow
    assert workflow.count("sh scripts/check_runtime_limits.sh") >= 3
    assert workflow.index("python scripts/ci_node_onboarding_smoke.py") < workflow.index(
        "Post-onboarding runtime limits"
    )
    assert "generate-bootstrap-worker-secret" in workflow
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
    assert "100000000 134217728 64" in runtime_check
    assert "250000000 268435456 128" in runtime_check
    assert "check_limits bootstrap" in runtime_check
    assert "check_limits ci-node-agent" in runtime_check

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
