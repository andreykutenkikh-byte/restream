"""Disposable loopback HTTPS beta using the real panel, HUD, and relay protocol.

Run from the repository root: uv run --locked python -m scripts.hud_beta
Only relay telemetry is synthetic; this does not start or validate a video stream.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.core.security import generate_master_key, hash_password
from app.main import create_app
from app.services.mediamtx import MediaMTXClient
from app.services.preview import PreviewService

HOST = "127.0.0.1"
# Public fixture value, deliberately available only with an explicit CLI flag.
SYNTHETIC_PASSWORD = "local-hud-synthetic-only"  # noqa: S105
HEARTBEAT_SECONDS = 5.0
_NODE_READ = re.compile(r"/api/nodes/[A-Za-z0-9-]+(?:/relay(?:/status)?)?")
_DEVICE_REVOKE = re.compile(r"/api/moblin-hud/devices/[A-Za-z0-9-]+/revoke")
_READ_PATHS = {
    "/",
    "/login",
    "/servers",
    "/health/live",
    "/health/ready",
    "/api/auth/session",
    "/api/ingest",
    "/api/ingest/status",
    "/api/destinations",
    "/api/system/diagnostics",
    "/api/nodes",
    "/api/relay-nodes",
    "/api/moblin-hud/devices",
    "/moblin-hud",
    "/moblin-hud/api/status",
}
_POST_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/moblin-hud/pairings",
    "/moblin-hud/api/pair",
    "/moblin-hud/api/logout",
    "/relay-agent/v1/heartbeat",
}


class BetaBoundary:
    """Beta-only route allowlist: media, installation, and control stay inaccessible."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        allowed = (
            method in {"GET", "HEAD"}
            and (path in _READ_PATHS or path.startswith("/static/") or _NODE_READ.fullmatch(path))
        ) or (method == "POST" and (path in _POST_PATHS or _DEVICE_REVOKE.fullmatch(path)))
        if not allowed:
            response = JSONResponse(
                {"error": {"code": "beta_read_only", "message": "Disabled in synthetic HUD beta"}},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class NoWorkers:
    async def spawn(self, _: Sequence[str]) -> Any:
        raise RuntimeError("Media processes are disabled in the synthetic HUD beta")


def build_app(directory: Path, port: int, password: str) -> FastAPI:
    """Use explicit settings, never environment credentials or an existing database."""
    database_path = directory / "hud-beta.sqlite3"
    if database_path.exists():
        raise ValueError("The beta requires a fresh database")
    settings = Settings(
        environment="test",
        public_domain=HOST,
        public_control_url=f"https://{HOST}:{port}",
        public_rtmp_host=HOST,
        public_rtmp_port=1,
        session_secret=secrets.token_urlsafe(48),
        master_encryption_key=generate_master_key(),
        admin_login="beta",
        admin_password_hash=hash_password(password),
        database_path=database_path,
        mediamtx_api_url=f"http://{HOST}:1",
        mediamtx_hls_url=f"http://{HOST}:1",
        mediamtx_internal_rtmp_url=f"rtmp://{HOST}:1",
        max_destinations=2,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=30,
        reconnect_stable_seconds=60,
        reconnect_max_fast_failures=3,
        log_level="WARNING",
        trusted_proxies=(),
        cookie_secure=True,
        session_ttl_seconds=3600,
        ffmpeg_binary="disabled-in-hud-beta",
        ffprobe_binary="disabled-in-hud-beta",
        worker_auth_user="beta-disabled",
        worker_auth_password=secrets.token_urlsafe(48),
        bootstrap_socket_path=directory / "disabled-bootstrap.sock",
        bootstrap_worker_secret="",
    )
    # Both media transports stay in-process even if a future page probes them.
    mediamtx = MediaMTXClient(settings.mediamtx_api_url, transport=lambda _: None)
    preview = PreviewService(
        settings.mediamtx_hls_url,
        username=settings.worker_auth_user,
        password=settings.worker_auth_password,
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    app = create_app(settings, mediamtx=mediamtx, preview=preview, worker_launcher=NoWorkers())
    app.add_middleware(BetaBoundary)
    return app


def tls_files(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HUD synthetic localhost beta")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = directory / "localhost.key", directory / "localhost.crt"
    with key_path.open("xb") as output:
        key_path.chmod(0o600)
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def heartbeat_payload() -> dict[str, Any]:
    """A main-compatible v1 fixture, with no URL/key, recovery, or installer fields."""
    return {
        "agent_version": "1.0.0",
        "protocol_version": 1,
        "hostname": "synthetic-hud-beta",
        "relay": {
            "service_state": "active",
            "enabled": False,
            "main_process": "running",
            "srt_listener": "listening",
            "source": "LIVE",
            "input_bitrate_bps": 4_000_000,
            "youtube_forward": "active",
            "overall": "healthy",
            "youtube_url_configured": True,
            "youtube_key_configured": True,
            "healthy": True,
            "portrait_profile": True,
            "error_code": None,
        },
        "host": {
            "uptime_seconds": 100,
            "load_1m": 0.1,
            "cpu_percent": 10.0,
            "memory_total_bytes": 2_000_000_000,
            "memory_available_bytes": 1_000_000_000,
            "disk_total_bytes": 20_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
        "current_command_id": None,
    }


def run_demo(client: httpx.Client, token: str, *, duration: int, phase_seconds: int) -> None:
    """Advance only the fixture's real-time schedule; retain all application policies."""
    started = time.monotonic()
    next_heartbeat = started
    previous_phase = -1
    while (now := time.monotonic()) - started < duration:
        phase = int((now - started) // phase_seconds) % 3
        if phase != previous_phase:
            description = ("normal LIVE telemetry", "telemetry paused", "LIVE telemetry restored")
            print(f"SYNTHETIC phase: {description[phase]}", flush=True)
            previous_phase = phase
            next_heartbeat = now
        if phase != 1 and now >= next_heartbeat:
            response = client.post(
                "/relay-agent/v1/heartbeat",
                json=heartbeat_payload(),
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                raise RuntimeError("Synthetic heartbeat was rejected")
            next_heartbeat = now + HEARTBEAT_SECONDS
        time.sleep(min(0.25, max(0.0, started + duration - time.monotonic())))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=8443, help="Loopback port; 0 selects a free port"
    )
    parser.add_argument("--duration", type=int, default=600, help="Exit after 15–3600 seconds")
    parser.add_argument(
        "--phase-seconds", type=int, default=45, help="Each phase lasts 45–300 seconds"
    )
    parser.add_argument(
        "--synthetic-password",
        action="store_true",
        help="Use the documented public fixture password instead of a hidden prompt",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if not 15 <= args.duration <= 3600:
        parser.error("--duration must be between 15 and 3600 seconds")
    if not 45 <= args.phase_seconds <= 300:
        parser.error("--phase-seconds must be between 45 and 300 seconds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    password = (
        SYNTHETIC_PASSWORD
        if args.synthetic_password
        else getpass.getpass(
            "Temporary beta password (12+ characters; do not reuse a real password): "
        )
    )
    if not 12 <= len(password) <= 128:
        print("The temporary password must contain 12–128 characters.", flush=True)
        return 2
    try:
        with (
            TemporaryDirectory(prefix="adojapan-hud-beta-") as temporary,
            socket.socket() as listener,
        ):
            listener.bind((HOST, args.port))
            port = listener.getsockname()[1]
            directory = Path(temporary)
            app = build_app(directory, port, password)
            key_path, cert_path = tls_files(directory)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=HOST,
                    port=port,
                    ssl_keyfile=str(key_path),
                    ssl_certfile=str(cert_path),
                    log_config=None,
                    access_log=False,
                    timeout_graceful_shutdown=3,
                )
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        raise RuntimeError("The loopback HTTPS server could not start")
                    time.sleep(0.05)
                grant = app.state.relays.provision_node(
                    display_name="SYNTHETIC beta relay — no video",
                    address=HOST,
                )
                origin = f"https://{HOST}:{port}"
                print(f"SYNTHETIC HUD beta, no video or external services: {origin}/", flush=True)
                print(
                    "Login: beta. Use your prompted password or the documented fixture password.",
                    flush=True,
                )
                print(
                    "Local self-signed HTTPS; pair from the panel. Ctrl+C stops and removes data.",
                    flush=True,
                )
                print(
                    f"Bounded run: {args.duration}s; repeating phases: {args.phase_seconds}s each.",
                    flush=True,
                )
                context = ssl.create_default_context(cafile=str(cert_path))
                with httpx.Client(
                    base_url=origin, verify=context, trust_env=False, timeout=5
                ) as client:
                    run_demo(
                        client,
                        grant.node_token,
                        duration=args.duration,
                        phase_seconds=args.phase_seconds,
                    )
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError("The beta server did not finish shutdown")
    except KeyboardInterrupt:
        pass
    except Exception:
        # Exception text can contain submitted credentials; report no raw values.
        print("HUD beta failed. Check the loopback port and local Python dependencies.", flush=True)
        return 1
    print("HUD beta stopped; disposable database and TLS key removed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
