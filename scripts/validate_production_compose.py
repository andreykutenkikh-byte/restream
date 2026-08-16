"""Validate the resolved production Compose model without printing its environment."""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, cast


class PolicyViolation(RuntimeError):
    """A fixed, secret-safe production policy failure."""


EXPECTED_RESOURCES = {
    "backend": (Decimal("0.40"), 384 * 1024 * 1024, 96),
    "mediamtx": (Decimal("0.20"), 192 * 1024 * 1024, 64),
}


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
    require(
        "/var/run/" + "docker.sock" not in json.dumps(service.get("volumes", [])), f"{name}_socket"
    )


def validate(model: dict[str, Any]) -> None:
    services = as_mapping(model.get("services"), "services_shape")
    require(set(services) == {"backend", "mediamtx"}, "production_service_set")
    backend = as_mapping(services.get("backend"), "backend_shape")
    mediamtx = as_mapping(services.get("mediamtx"), "mediamtx_shape")
    environment = as_mapping(backend.get("environment"), "backend_environment_shape")

    expected_environment = {
        "ENVIRONMENT": "production",
        "COOKIE_SECURE": "true",
        "MAX_DESTINATIONS": "1",
        "PUBLIC_DOMAIN": "restream.adojapan.ru",
        "PUBLIC_RTMP_HOST": "restream.adojapan.ru",
        "PUBLIC_RTMP_PORT": "1935",
    }
    for key, expected_value in expected_environment.items():
        require(str(environment.get(key, "")) == expected_value, f"safe_value_{key.lower()}")
    require("TEST_DESTINATION_ALLOWLIST" not in environment, "production_test_allowlist")

    session_secret = str(environment.get("SESSION_SECRET", ""))
    worker_password = str(environment.get("WORKER_AUTH_PASSWORD", ""))
    require(bool(session_secret and worker_password), "required_secret_presence")
    require(session_secret != worker_password, "required_secret_independence")

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
        total_cpu += cpu
        total_memory += memory
        total_pids += pids

    require(total_cpu <= Decimal("0.60"), "aggregate_cpu")
    require(total_memory <= 576 * 1024 * 1024, "aggregate_memory")
    require(total_pids == 160, "aggregate_pids")

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
