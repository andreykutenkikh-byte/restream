"""Validate the resolved production Compose model without printing its environment."""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, cast


class PolicyViolation(RuntimeError):
    """A fixed, secret-safe production policy failure."""


EXPECTED_RESOURCES = {
    "backend": (Decimal("0.40"), 384 * 1024 * 1024, 96),
    "mediamtx": (Decimal("0.20"), 192 * 1024 * 1024, 64),
    "bootstrap": (Decimal("0.10"), 128 * 1024 * 1024, 64),
}
EXPECTED_SOCKET_TARGET = "/run/adojapan-bootstrap"
NODE_AGENT_IMAGE_PATTERN = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
    r"@sha256:[0-9a-f]{64}"
)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PolicyViolation(code)


def as_mapping(value: Any, code: str) -> dict[str, Any]:
    require(isinstance(value, dict), code)
    return cast(dict[str, Any], value)


def memory_bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    units = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024 * 1024,
        "mb": 1024 * 1024,
        "mib": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
        "gib": 1024 * 1024 * 1024,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if suffix and text.endswith(suffix):
            return int(Decimal(text[: -len(suffix)]) * units[suffix])
    return int(Decimal(text))


def duration_seconds(value: Any) -> Decimal:
    if isinstance(value, int):
        # compose-go JSON has represented durations as nanoseconds in older releases.
        return Decimal(value) / Decimal(1_000_000_000)
    text = str(value).strip().lower()
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if match is None or not any(match.groups()):
        raise ValueError("invalid duration")
    minutes = Decimal(match.group(1) or "0")
    seconds = Decimal(match.group(2) or "0")
    return minutes * 60 + seconds


def normalized_port(port: Any) -> tuple[str, int, int]:
    mapping = as_mapping(port, "port_shape")
    host_ip = str(mapping.get("host_ip", ""))
    target = int(mapping.get("target", 0))
    published = int(mapping.get("published", 0))
    return host_ip, target, published


def validate_service_security(service: dict[str, Any], name: str) -> None:
    require(service.get("network_mode") != "host", f"{name}_host_network")
    require(service.get("privileged") is not True, f"{name}_privileged")
    require(service.get("read_only") is True, f"{name}_read_only")
    require(service.get("restart") == "unless-stopped", f"{name}_restart_policy")
    require(service.get("cap_drop") == ["ALL"], f"{name}_cap_drop")
    security_opt = service.get("security_opt", [])
    require("no-new-privileges:true" in security_opt, f"{name}_no_new_privileges")
    require(service.get("healthcheck") is not None, f"{name}_healthcheck")
    require(service.get("logging") is not None, f"{name}_logging")
    require(service.get("restart") == "unless-stopped", f"{name}_restart")
    require(
        "/var/run/" + "docker.sock" not in json.dumps(service.get("volumes", [])), f"{name}_socket"
    )


def network_names(service: dict[str, Any], name: str) -> set[str]:
    networks = service.get("networks", {})
    if isinstance(networks, list):
        return {str(item) for item in networks}
    return set(as_mapping(networks, f"{name}_networks_shape"))


def volume_mounts(service: dict[str, Any], name: str) -> list[dict[str, Any]]:
    raw_volumes = service.get("volumes", [])
    require(isinstance(raw_volumes, list), f"{name}_volumes_shape")
    mounts: list[dict[str, Any]] = []
    for raw_mount in raw_volumes:
        mounts.append(as_mapping(raw_mount, f"{name}_volume_shape"))
    return mounts


def find_mount(service: dict[str, Any], name: str, target: str) -> dict[str, Any]:
    matches = [mount for mount in volume_mounts(service, name) if mount.get("target") == target]
    require(len(matches) == 1, f"{name}_socket_mount")
    return matches[0]


def validate(model: dict[str, Any]) -> None:
    services = as_mapping(model.get("services"), "services_shape")
    networks = as_mapping(model.get("networks"), "networks_shape")
    secrets = as_mapping(model.get("secrets"), "secrets_shape")
    require(set(services) == {"backend", "mediamtx", "bootstrap"}, "production_service_set")
    backend = as_mapping(services.get("backend"), "backend_shape")
    mediamtx = as_mapping(services.get("mediamtx"), "mediamtx_shape")
    bootstrap = as_mapping(services.get("bootstrap"), "bootstrap_shape")
    environment = as_mapping(backend.get("environment"), "backend_environment_shape")
    bootstrap_environment = as_mapping(bootstrap.get("environment"), "bootstrap_environment_shape")

    expected_environment = {
        "ENVIRONMENT": "production",
        "COOKIE_SECURE": "true",
        "MAX_DESTINATIONS": "1",
        "PUBLIC_DOMAIN": "restream.adojapan.ru",
        "PUBLIC_RTMP_HOST": "restream.adojapan.ru",
        "PUBLIC_RTMP_PORT": "1935",
        "BOOTSTRAP_SOCKET_PATH": "/run/adojapan-bootstrap/bootstrap.sock",
        "NODE_PROTOCOL_VERSION": "1",
        "PUBLIC_CONTROL_URL": "https://restream.adojapan.ru",
    }
    for key, expected_value in expected_environment.items():
        require(str(environment.get(key, "")) == expected_value, f"safe_value_{key.lower()}")
    require("TEST_DESTINATION_ALLOWLIST" not in environment, "production_test_allowlist")

    session_secret = str(environment.get("SESSION_SECRET", ""))
    worker_password = str(environment.get("WORKER_AUTH_PASSWORD", ""))
    bootstrap_secret = str(environment.get("BOOTSTRAP_WORKER_SECRET", ""))
    require(
        bool(session_secret and worker_password and bootstrap_secret), "required_secret_presence"
    )
    require(session_secret != worker_password, "required_secret_independence")
    require(
        len({session_secret, worker_password, bootstrap_secret}) == 3,
        "bootstrap_secret_independence",
    )

    node_agent_image = str(environment.get("NODE_AGENT_IMAGE", ""))
    require(
        NODE_AGENT_IMAGE_PATTERN.fullmatch(node_agent_image) is not None,
        "node_agent_image_digest",
    )
    require(
        str(bootstrap_environment.get("ENVIRONMENT", "")) == "production",
        "bootstrap_environment",
    )
    require(
        str(bootstrap_environment.get("BOOTSTRAP_SOCKET_PATH", ""))
        == "/run/adojapan-bootstrap/bootstrap.sock",
        "bootstrap_socket_path",
    )
    require(
        str(bootstrap_environment.get("BOOTSTRAP_SECRET_FILE", ""))
        == "/run/secrets/bootstrap_worker_secret",
        "bootstrap_secret_file",
    )
    require("BOOTSTRAP_WORKER_SECRET" not in bootstrap_environment, "bootstrap_raw_secret")
    bootstrap_secrets = bootstrap.get("secrets", [])
    require(isinstance(bootstrap_secrets, list), "bootstrap_secrets_shape")
    require(len(bootstrap_secrets) == 1, "bootstrap_secret_count")
    bootstrap_secret_mount = as_mapping(bootstrap_secrets[0], "bootstrap_secret_shape")
    require(
        str(bootstrap_secret_mount.get("source", "")) == "bootstrap_worker_secret",
        "bootstrap_secret_source",
    )
    require(set(secrets) == {"bootstrap_worker_secret"}, "production_secret_set")
    bootstrap_secret_definition = as_mapping(
        secrets.get("bootstrap_worker_secret"), "bootstrap_secret_definition"
    )
    require(bool(str(bootstrap_secret_definition.get("file", ""))), "bootstrap_secret_file_source")
    require(
        "environment" not in bootstrap_secret_definition,
        "bootstrap_secret_environment_source",
    )

    total_cpu = Decimal(0)
    total_memory = 0
    total_pids = 0
    for name, expected_resources in EXPECTED_RESOURCES.items():
        service = as_mapping(services.get(name), f"{name}_shape")
        validate_service_security(service, name)
        cpu = Decimal(str(service.get("cpus", "0")))
        memory = memory_bytes(service.get("mem_limit", 0))
        pids = int(service.get("pids_limit", 0))
        require((cpu, memory, pids) == expected_resources, f"{name}_resources")
        if name == "bootstrap":
            require(
                duration_seconds(service.get("stop_grace_period")) == Decimal(90),
                "bootstrap_stop_grace",
            )
        total_cpu += cpu
        total_memory += memory
        total_pids += pids

    require(total_cpu == Decimal("0.70"), "aggregate_cpu")
    require(total_memory == 704 * 1024 * 1024, "aggregate_memory")
    require(total_pids == 224, "aggregate_pids")

    require(bootstrap.get("ports") in (None, []), "bootstrap_ports")
    require(bootstrap.get("expose") in (None, []), "bootstrap_expose")
    require(network_names(bootstrap, "bootstrap") == {"bootstrap-egress"}, "bootstrap_networks")
    bootstrap_network = as_mapping(
        networks.get("bootstrap-egress"), "bootstrap_egress_network_shape"
    )
    require(bootstrap_network.get("internal") is not True, "bootstrap_egress_internal")
    require(
        "bootstrap-egress" not in network_names(backend, "backend"), "backend_bootstrap_network"
    )
    require(
        "bootstrap-egress" not in network_names(mediamtx, "mediamtx"), "media_bootstrap_network"
    )

    backend_socket = find_mount(backend, "backend", EXPECTED_SOCKET_TARGET)
    require(backend_socket.get("type") == "volume", "backend_socket_type")
    require(str(backend_socket.get("source", "")) == "bootstrap_socket", "backend_socket_source")
    require(backend_socket.get("read_only") is True, "backend_socket_read_only")

    bootstrap_mounts = volume_mounts(bootstrap, "bootstrap")
    require(len(bootstrap_mounts) == 1, "bootstrap_volume_count")
    bootstrap_socket = find_mount(bootstrap, "bootstrap", EXPECTED_SOCKET_TARGET)
    require(bootstrap_socket.get("type") == "volume", "bootstrap_socket_type")
    require(
        str(bootstrap_socket.get("source", "")) == "bootstrap_socket",
        "bootstrap_socket_source",
    )
    require(bootstrap_socket.get("read_only") is not True, "bootstrap_socket_writable")
    forbidden_storage = {"database", "logs", "backups"}
    require(
        not forbidden_storage.intersection(
            str(mount.get("source", "")) for mount in bootstrap_mounts
        ),
        "bootstrap_main_storage",
    )

    build = as_mapping(bootstrap.get("build"), "bootstrap_build_shape")
    require(str(build.get("dockerfile", "")) == "Dockerfile.bootstrap", "bootstrap_dockerfile")

    backend_ports = backend.get("ports")
    if not isinstance(backend_ports, list) or len(backend_ports) != 1:
        raise PolicyViolation("backend_ports")
    backend_host, backend_target, backend_published = normalized_port(backend_ports[0])
    require(backend_host == "127.0.0.1", "backend_host_ip")
    require(backend_target == 8000 and backend_published > 0, "backend_port_mapping")

    media_ports = mediamtx.get("ports")
    if not isinstance(media_ports, list) or len(media_ports) != 1:
        raise PolicyViolation("mediamtx_ports")
    media_host, media_target, media_published = normalized_port(media_ports[0])
    require(bool(media_host), "mediamtx_host_ip")
    require(media_target == 1935 and media_published == 1935, "mediamtx_port_mapping")


def main() -> None:
    try:
        model = as_mapping(json.load(sys.stdin), "document_shape")
        validate(model)
    except PolicyViolation as exc:
        print(f"Production Compose policy failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
        print("Production Compose policy failed: invalid_document", file=sys.stderr)
        raise SystemExit(1) from None
    print("Production Compose policy verified")


if __name__ == "__main__":
    main()
