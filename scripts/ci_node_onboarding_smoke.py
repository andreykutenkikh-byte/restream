"""Secret-safe real CI smoke for SSH bootstrap, Node Agent, and revoke.

The password marker is sent only in the HTTPS-style request body and over the
worker's UDS/SSH channel. Captured API responses, logs, files, process
arguments, and container environments are checked in memory and never printed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from scripts.ci_output_smoke import COMPOSE, APIClient, SmokeFailure

PASSWORD_MARKER = "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"  # noqa: S105
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_RETAINED_LOG_BYTES = 80 * 1024 * 1024
JOB_TIMEOUT_SECONDS = 600
COMMAND_TIMEOUT_SECONDS = 60
READY_TIMEOUT_SECONDS = 60
SELF_TEST_CHECKS = {
    "control_https",
    "dns",
    "ffmpeg",
    "ffprobe",
    "memory",
    "disk",
    "data_writable",
    "no_inbound_ports",
}
SAFE_BOOTSTRAP_DIAGNOSTIC_CODES = frozenset(
    {
        "agent_enrollment_failed",
        "agent_install_failed",
        "bootstrap_failed",
        "bootstrap_rejected",
        "bootstrap_unavailable",
        "bootstrap_worker_restarted",
        "cancelled",
        "credential_rotation_unavailable",
        "insufficient_cpu",
        "insufficient_disk",
        "insufficient_memory",
        "invalid_job_state",
        "invalid_target",
        "job_conflict",
        "job_not_found",
        "outbound_https_unavailable",
        "overall_timeout",
        "remote_command_failed",
        "remote_directory_conflict",
        "remote_upload_failed",
        "ssh_authentication_failed",
        "ssh_connection_failed",
        "ssh_host_key_changed",
        "ssh_host_key_unsupported",
        "sudo_password_invalid",
        "target_resolution_changed",
        "unsupported_docker_installation",
        "unsupported_operating_system",
    }
)
SMOKE_STAGES = frozenset(
    {
        "bootstrap_job",
        "commands",
        "create_job",
        "host_fingerprint",
        "login",
        "node_ready",
        "password_non_persistence",
        "ping",
        "remote_lifecycle",
        "revoke",
        "revoke_quiescence",
        "self_test",
        "startup",
    }
)
_current_stage = "startup"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")


def require_marker_absent(payload: bytes, location: str) -> None:
    if PASSWORD_MARKER.encode("ascii") in payload:
        raise SmokeFailure(f"password persistence marker found in {location}")


def safe_api_payload[T](value: T, location: str) -> T:
    require_marker_absent(encoded(value), location)
    return value


def safe_bootstrap_diagnostic_code(payload: Mapping[str, Any]) -> str:
    error = payload.get("safe_error")
    if not isinstance(error, Mapping):
        return "unknown"
    code = error.get("code")
    if isinstance(code, str) and code in SAFE_BOOTSTRAP_DIAGNOSTIC_CODES:
        return code
    return "unknown"


def set_smoke_stage(stage: str) -> None:
    global _current_stage  # noqa: PLW0603 - single-process diagnostic state
    if stage not in SMOKE_STAGES:
        raise ValueError("unsupported smoke diagnostic stage")
    _current_stage = stage


def ci_host_fingerprint() -> str:
    result = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "ssh-keygen",
        "-E",
        "sha256",
        "-lf",
        "/etc/ssh/ssh_host_ed25519_key.pub",
    )
    output = result.stdout.decode("ascii", errors="strict").strip()
    match = re.fullmatch(r"\d+ (SHA256:[A-Za-z0-9+/]{43}) .+ \(ED25519\)", output)
    if match is None:
        raise SmokeFailure("CI SSH host fingerprint is invalid")
    return match.group(1)


def compose(
    *arguments: str,
    input_bytes: bytes | None = None,
    expected: Sequence[int] = (0,),
    max_capture_bytes: int = MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Compose argv and bounded capture
            (*COMPOSE, *arguments),
            cwd=".",
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure("bounded Compose probe failed") from exc
    if result.returncode not in expected:
        raise SmokeFailure("bounded Compose probe returned an unexpected status")
    if len(result.stdout) + len(result.stderr) > max_capture_bytes:
        raise SmokeFailure("bounded Compose probe exceeded its capture limit")
    return result


def wait_for[T](description: str, timeout: float, probe: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(0.5)
    raise SmokeFailure(f"timed out waiting for {description}")


def poll_job(client: APIClient, job_id: str) -> Mapping[str, Any]:
    def probe() -> Mapping[str, Any] | None:
        raw = safe_api_payload(
            client.request("GET", f"/api/nodes/bootstrap/{job_id}"),
            "bootstrap job API",
        )
        require(isinstance(raw, dict), "bootstrap job response is invalid")
        state = str(raw.get("state", ""))
        if state in {"failed", "cancelled"}:
            print(f"Bootstrap terminal safe code: {safe_bootstrap_diagnostic_code(raw)}")
            raise SmokeFailure("bootstrap job reached a non-success terminal state")
        return raw if state == "completed" else None

    return wait_for("bootstrap completion", JOB_TIMEOUT_SECONDS, probe)


def ready_node(client: APIClient) -> Mapping[str, Any]:
    def probe() -> Mapping[str, Any] | None:
        raw = safe_api_payload(client.request("GET", "/api/nodes"), "node list API")
        require(isinstance(raw, dict), "node list response is invalid")
        items = raw.get("items")
        require(isinstance(items, list) and len(items) == 1, "node list is not singular")
        node = items[0]
        require(isinstance(node, dict), "node response is invalid")
        node = cast(dict[str, Any], node)
        if node.get("status") != "ready":
            return None
        require(node.get("agent_version") == "0.1.0", "node agent version is invalid")
        require(node.get("protocol_version") == 1, "node protocol version is invalid")
        require(
            isinstance(node.get("hostname"), str) and bool(node["hostname"]), "hostname missing"
        )
        require(isinstance(node.get("cpu_count"), int) and node["cpu_count"] > 0, "CPU missing")
        for field in ("uptime_seconds", "load_1m", "cpu_percent"):
            value = node.get(field)
            require(
                not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0,
                f"{field} metric is invalid",
            )
        require(float(node["cpu_percent"]) <= 100, "CPU percentage is invalid")
        for total_field, available_field in (
            ("memory_total_bytes", "memory_available_bytes"),
            ("disk_total_bytes", "disk_free_bytes"),
        ):
            total = node.get(total_field)
            available = node.get(available_field)
            if not isinstance(total, int) or total <= 0:
                raise SmokeFailure(f"{total_field} metric is invalid")
            if not isinstance(available, int) or not 0 <= available <= total:
                raise SmokeFailure(f"{available_field} metric is invalid")
        for field in ("ffmpeg_version", "ffprobe_version", "last_seen_at"):
            require(isinstance(node.get(field), str) and bool(node[field]), f"{field} is missing")
        require(
            node.get("capabilities") == ["ping", "self_test", "ffmpeg", "ffprobe"],
            "node capabilities are invalid",
        )
        return node

    return wait_for("ready node heartbeat", READY_TIMEOUT_SECONDS, probe)


def complete_command(client: APIClient, node_id: str, command_name: str) -> Mapping[str, Any]:
    path_name = "ping" if command_name == "PING" else "self-test"
    created = safe_api_payload(
        client.request(
            "POST",
            f"/api/nodes/{node_id}/{path_name}",
            {},
            csrf=True,
            expected=(202,),
        ),
        f"{command_name} create API",
    )
    require(isinstance(created, dict), f"{command_name} create response is invalid")
    command_id = str(created.get("id", ""))
    require(bool(command_id), f"{command_name} did not return a command id")

    def probe() -> Mapping[str, Any] | None:
        raw = safe_api_payload(
            client.request("GET", f"/api/nodes/{node_id}/commands/{command_id}"),
            f"{command_name} result API",
        )
        require(isinstance(raw, dict), f"{command_name} result response is invalid")
        state = str(raw.get("state", ""))
        if state == "failed":
            result = raw.get("safe_result")
            code = result.get("code") if isinstance(result, Mapping) else None
            safe_code = code if code in {"command_expired", "command_failed"} else "unknown"
            print(f"{command_name} terminal safe code: {safe_code}")
            raise SmokeFailure(f"{command_name} command failed")
        return raw if state == "completed" else None

    completed = wait_for(f"{command_name} completion", COMMAND_TIMEOUT_SECONDS, probe)
    result = completed.get("safe_result")
    if not isinstance(result, dict) or result.get("status") != "ok":
        if command_name == "SELF_TEST" and isinstance(result, Mapping):
            checks = result.get("checks")
            if isinstance(checks, Mapping) and set(checks) == SELF_TEST_CHECKS:
                failed = sorted(name for name in SELF_TEST_CHECKS if checks.get(name) is not True)
                print(f"SELF_TEST failed safe checks: {','.join(failed) or 'unknown'}")
        raise SmokeFailure(f"{command_name} failed")
    if command_name == "SELF_TEST":
        checks = result.get("checks")
        require(
            isinstance(checks, dict)
            and set(checks) == SELF_TEST_CHECKS
            and all(value is True for value in checks.values()),
            "SELF_TEST checks did not all pass",
        )
        require(result.get("received_at") is None, "SELF_TEST received_at must be null")
    else:
        require(
            isinstance(result.get("received_at"), str) and bool(result["received_at"]),
            "PING received timestamp is missing",
        )
        require(result.get("checks") is None, "PING checks must be null")
    require(
        isinstance(result.get("completed_at"), str) and bool(result["completed_at"]),
        f"{command_name} completion timestamp is missing",
    )
    require(result.get("agent_version") == "0.1.0", f"{command_name} agent version is invalid")
    return completed


def agent_container_state() -> tuple[str, str, int, bool]:
    identifier = compose("ps", "-a", "-q", "ci-node-agent").stdout.strip()
    require(bool(identifier), "CI Node Agent container is missing")
    state = subprocess.run(  # noqa: S603,S607 - fixed inspect and Compose-owned id
        (  # noqa: S607 - fixed Docker inspect and a Compose-owned container id
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}",
            identifier.decode("ascii"),
        ),
        capture_output=True,
        check=False,
        timeout=10,
    )
    if state.returncode != 0:
        raise SmokeFailure("could not inspect CI Node Agent state")
    require_marker_absent(state.stdout + state.stderr, "Node Agent state probe")
    fields = state.stdout.decode("ascii").strip().split("|")
    require(len(fields) == 3, "Node Agent state shape is invalid")
    try:
        restart_count = int(fields[1])
    except ValueError as exc:
        raise SmokeFailure("Node Agent restart count is invalid") from exc
    return identifier.decode("ascii"), fields[0], restart_count, fields[2].lower() == "true"


def require_agent_quiescent_after_revoke(
    expected_identifier: str,
    expected_restart_count: int,
) -> None:
    # One heartbeat interval plus command-poll margin ensures the live agent has
    # observed the permanent 401 before its stable process state is inspected.
    time.sleep(7)
    identifier, status, restart_count, oom_killed = agent_container_state()
    require(identifier == expected_identifier, "Node Agent container restarted after revoke")
    require(status == "running", "Node Agent did not remain quiescent after revoke")
    require(
        restart_count == expected_restart_count,
        "Node Agent restart count changed after revoke",
    )
    require(not oom_killed, "Node Agent was OOM-killed")
    logs = compose("logs", "--no-color", "ci-node-agent")
    quiescent_marker = b"Agent entered quiescent state (node_credential_rejected)"
    require(
        (logs.stdout + logs.stderr).count(quiescent_marker) == 1,
        "Node Agent did not enter one stable quiescent state",
    )


def require_revoked_heartbeat_is_rejected() -> None:
    probe = compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "ci-node-agent",
        "python",
        "-c",
        (
            "import httpx; "
            "from node_agent.credentials import CredentialStore; "
            "from node_agent.metrics import MetricsCollector; "
            "from node_agent.settings import AgentSettings; "
            "s=AgentSettings.from_env(); "
            "t=CredentialStore(s.enrollment_token_path,s.node_token_path).load_permanent(); "
            "m=MetricsCollector(s.data_dir).collect(); "
            "r=httpx.post(s.control_url+'/node-api/v1/heartbeat',"
            "headers={'Authorization':'Bearer '+t.reveal()},"
            "json=m.heartbeat_payload(agent_version='0.1.0',current_command_id=None,"
            "control_latency_ms=None),"
            "timeout=10); print(r.status_code)"
        ),
    )
    require(probe.stdout.strip() == b"401", "revoked heartbeat was not rejected with 401")


def scan_password_non_persistence() -> None:
    marker = PASSWORD_MARKER.encode("ascii")

    database_probe = compose(
        "exec",
        "-T",
        "backend",
        "python",
        "-c",
        (
            "import sys; from pathlib import Path; "
            "needle=sys.stdin.buffer.read(); root=Path('/srv/app/data'); "
            "found=any(needle in path.read_bytes() for path in root.iterdir() "
            "if path.is_file() and not path.is_symlink()); "
            "print('FOUND' if found else 'CLEAR')"
        ),
        input_bytes=marker,
    )
    require(database_probe.stdout.strip() == b"CLEAR", "password marker found in SQLite")

    remote_probe = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        (
            "if grep -R -q -F -f - /opt/adojapan-restream-node 2>/dev/null; "
            "then printf 'FOUND\\n'; else printf 'CLEAR\\n'; fi"
        ),
        input_bytes=marker,
    )
    require(remote_probe.stdout.strip() == b"CLEAR", "password marker found in remote files")

    for location, result in (
        (
            "backend/bootstrap logs",
            compose(
                "logs",
                "--no-color",
                "backend",
                "bootstrap",
                max_capture_bytes=MAX_RETAINED_LOG_BYTES,
            ),
        ),
        ("effective Compose model", compose("config")),
        (
            "remote process arguments",
            compose("exec", "-T", "ci-ssh-target", "ps", "-eo", "args="),
        ),
    ):
        require_marker_absent(result.stdout + result.stderr, location)

    identifiers = compose("ps", "-a", "-q").stdout.splitlines()
    require(bool(identifiers), "CI project has no containers to inspect")
    for identifier in identifiers:
        try:
            decoded = identifier.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SmokeFailure("container id was not ASCII") from exc
        inspection = subprocess.run(  # noqa: S603,S607 - fixed inspect and Compose-owned ids
            (  # noqa: S607 - fixed Docker inspect and Compose-owned container ids
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Env}} {{json .Config.Cmd}} {{json .Path}} {{json .Args}}",
                decoded,
            ),
            capture_output=True,
            check=False,
            timeout=10,
        )
        if inspection.returncode != 0:
            raise SmokeFailure("container secret-boundary inspect failed")
        require_marker_absent(
            inspection.stdout + inspection.stderr,
            "container environment or process arguments",
        )


def verify_remote_lifecycle() -> None:
    lifecycle = compose(
        "exec",
        "-T",
        "ci-ssh-target",
        "sh",
        "-c",
        (
            "test -f /opt/adojapan-restream-node/.managed-by-adojapan && "
            'test "$(cat /opt/adojapan-restream-node/.managed-by-adojapan)" = '
            "'adojapan-restream-node:v1' && "
            "grep -q '^    user: \"10001:10001\"$' "
            "/opt/adojapan-restream-node/compose.yml && "
            "grep -q '^      NODE_CONTROL_URL: \"http://backend:8000\"$' "
            "/opt/adojapan-restream-node/compose.yml && "
            "grep -q '^      NODE_AGENT_ENVIRONMENT: test$' "
            "/opt/adojapan-restream-node/compose.yml && "
            "! grep -Eq '(^|[[:space:]])(ports|privileged):' "
            "/opt/adojapan-restream-node/compose.yml && "
            "test ! -e /opt/adojapan-restream-node/data/enrollment.token && "
            "test -f /opt/adojapan-restream-node/data/node.token && "
            "test \"$(stat -c '%a:%u:%g' /opt/adojapan-restream-node/data/node.token)\" = "
            "'600:10001:10001' && "
            "test \"$(grep -c '^compose_up$' "
            '/opt/adojapan-restream-node/fake-docker-calls.log)" = 1 && '
            "printf 'REMOTE_LIFECYCLE_OK\\n'"
        ),
    )
    require(lifecycle.stdout.strip() == b"REMOTE_LIFECYCLE_OK", "remote lifecycle is invalid")


def main() -> None:
    set_smoke_stage("host_fingerprint")
    expected_host_fingerprint = ci_host_fingerprint()
    client = APIClient()
    set_smoke_stage("login")
    client.login()
    request_payload: dict[str, Any] = {
        "address": "ci-ssh-target",
        "port": 22,
        "username": "ci-node",
        "password": PASSWORD_MARKER,
        "expected_host_fingerprint": None,
    }
    set_smoke_stage("create_job")
    try:
        accepted = safe_api_payload(
            client.request(
                "POST",
                "/api/nodes/bootstrap",
                request_payload,
                csrf=True,
                expected=(202,),
            ),
            "bootstrap create API",
        )
    finally:
        request_payload["password"] = ""
    require(isinstance(accepted, dict), "bootstrap acceptance response is invalid")
    job_id = str(accepted.get("job_id", ""))
    require(bool(job_id) and accepted.get("state") == "queued", "bootstrap job was not queued")

    set_smoke_stage("bootstrap_job")
    poll_job(client, job_id)
    set_smoke_stage("node_ready")
    node = ready_node(client)
    node_id = str(node.get("id", ""))
    require(bool(node_id), "ready node did not have an id")
    require(node.get("host_key_fingerprint") == expected_host_fingerprint, "host key changed")
    require(node.get("host_key_trust_mode") == "tofu", "TOFU was not persisted")
    require(bool(node.get("resolved_ip")), "resolved SSH target IP was not persisted")

    set_smoke_stage("ping")
    complete_command(client, node_id, "PING")
    set_smoke_stage("self_test")
    complete_command(client, node_id, "SELF_TEST")
    set_smoke_stage("remote_lifecycle")
    verify_remote_lifecycle()

    identifier, status, restart_count, oom_killed = agent_container_state()
    require(status == "running", "Node Agent was not running before revoke")
    require(not oom_killed, "Node Agent was OOM-killed before revoke")

    set_smoke_stage("revoke")
    revoked = safe_api_payload(
        client.request(
            "POST",
            f"/api/nodes/{node_id}/revoke",
            {},
            csrf=True,
        ),
        "node revoke API",
    )
    require(isinstance(revoked, dict) and revoked.get("status") == "revoked", "revoke failed")
    set_smoke_stage("revoke_quiescence")
    require_agent_quiescent_after_revoke(identifier, restart_count)
    require_revoked_heartbeat_is_rejected()
    set_smoke_stage("password_non_persistence")
    scan_password_non_persistence()

    print("SSH bootstrap E2E verified")
    print("Node enrollment, heartbeat, PING, SELF_TEST, and revoke verified")
    print("Password persistence marker absent from all required CI boundaries")


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure:
        print(f"Node onboarding smoke failed safely at stage: {_current_stage}")
        raise SystemExit(1) from None
