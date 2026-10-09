"""Idle-only three-second synthetic TCP transfer, authenticated in both directions.

No media credentials or payloads are involved. Tokens arrive through sealed node
plans, never over this socket. Every frame, byte count and connection is bounded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from app.broadcast.network_models import (
    PROBE_CAP_BPS,
    PROBE_MAX_BYTES,
    PROBE_SECONDS,
    ProbeProgress,
)


def proof(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "\0".join(parts).encode(), hashlib.sha256).hexdigest()


def read_exact(peer: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        data = peer.recv(size - len(result))
        if not data:
            raise OSError("peer_unavailable")
        result.extend(data)
    return bytes(result)


def message(peer: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(peer, 4))[0]
    if not 0 < size <= 1024:
        raise ValueError("authentication_failed")
    value = json.loads(read_exact(peer, size))
    if not isinstance(value, dict):
        raise ValueError("authentication_failed")
    return value


def write(peer: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > 1024:
        raise ValueError("authentication_failed")
    peer.sendall(struct.pack("!I", len(data)) + data)


def valid_job(job: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(job["expires_at"]) > datetime.now(UTC)
    except (KeyError, ValueError, TypeError):
        return False


class ProbeServer:
    def __init__(self, port: int, idle: Callable[[], bool], *, host: str = "0.0.0.0") -> None:  # noqa: S104
        self.idle = idle
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.ending = threading.Event()
        self.slots = threading.BoundedSemaphore(4)
        self.bulk = threading.Lock()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((host, port))
            self.listener.listen(4)
            self.listener.settimeout(0.25)
        except OSError:
            self.listener.close()
            raise
        self.port = int(self.listener.getsockname()[1])
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def accept(self, jobs: list[dict[str, Any]]) -> None:
        with self.lock:
            self.jobs = {
                j["id"]: j for j in jobs[:4] if j.get("role") == "receiver" and valid_job(j)
            }

    def _listen(self) -> None:
        while not self.ending.is_set():
            try:
                peer, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                peer.close()
                continue
            threading.Thread(target=self._handle, args=(peer,), daemon=True).start()

    def _handle(self, peer: socket.socket) -> None:
        try:
            peer.settimeout(1.5)
            hello = message(peer)
            client_nonce = hello["nonce"]
            if not isinstance(client_nonce, str) or len(client_nonce) != 64:
                return
            with self.lock:
                job = self.jobs.get(hello["id"])
            if job is None or not valid_job(job) or not self.idle():
                return
            nonce = secrets.token_hex(32)
            write(
                peer,
                {
                    "nonce": nonce,
                    "proof": proof(job["token"], "server", job["id"], client_nonce, nonce),
                },
            )
            response = message(peer)
            if not hmac.compare_digest(
                response.get("proof", ""),
                proof(job["token"], "client", job["id"], client_nonce, nonce),
            ):
                return
            if not self.bulk.acquire(blocking=False):
                return
            try:
                received, start, checked = 0, time.monotonic(), 0.0
                while not self.ending.is_set():
                    now = time.monotonic()
                    if now - start > 10 or not valid_job(job):
                        return
                    if now - checked >= 0.2:
                        if not self.idle():
                            return
                        checked = now
                    size = struct.unpack("!I", read_exact(peer, 4))[0]
                    if size == 0:
                        break
                    if size > 65536 or received + size > PROBE_MAX_BYTES:
                        return
                    read_exact(peer, size)
                    received += size
                elapsed = max(1.0, (time.monotonic() - start) * 1000)
                write(
                    peer,
                    {
                        "bytes": received,
                        "ms": elapsed,
                        "proof": proof(
                            job["token"],
                            "result",
                            job["id"],
                            client_nonce,
                            nonce,
                            str(received),
                            str(elapsed),
                        ),
                    },
                )
            finally:
                self.bulk.release()
        except (OSError, ValueError, KeyError, TypeError, struct.error):
            pass  # Never log attacker frames, tokens or addresses.
        finally:
            peer.close()
            self.slots.release()

    def close(self) -> None:
        self.ending.set()
        self.listener.close()
        self.thread.join(timeout=2)


def run_probe(
    job: dict[str, Any], idle: Callable[[], bool], *, test_loopback: bool = False
) -> ProbeProgress:
    failure = ProbeProgress(
        job_id=job["id"], role="sender", state="FAILED", safe_error="peer_unavailable"
    )
    if not valid_job(job):
        return failure.model_copy(update={"safe_error": "expired"})
    if not idle():
        return failure.model_copy(update={"safe_error": "media_active"})
    try:
        address = ip_address(job["host"])
        if not address.is_global and not (test_loopback and address.is_loopback):
            return failure
        port = int(job["port"])
        if not 1024 <= port <= 65535:
            return failure
        with socket.create_connection((str(address), port), timeout=1.5) as peer:
            nonce = secrets.token_hex(32)
            write(peer, {"id": job["id"], "nonce": nonce})
            response = message(peer)
            server_nonce = response["nonce"]
            if not isinstance(server_nonce, str) or len(server_nonce) != 64:
                return failure.model_copy(update={"safe_error": "authentication_failed"})
            if not hmac.compare_digest(
                response.get("proof", ""),
                proof(job["token"], "server", job["id"], nonce, server_nonce),
            ):
                return failure.model_copy(update={"safe_error": "authentication_failed"})
            write(peer, {"proof": proof(job["token"], "client", job["id"], nonce, server_nonce)})
            block = os.urandom(16384)
            start, sent, checked = time.monotonic(), 0, 0.0
            while sent < PROBE_MAX_BYTES and time.monotonic() - start < PROBE_SECONDS:
                now = time.monotonic()
                if now - checked >= 0.2:
                    if not valid_job(job):
                        return failure.model_copy(update={"safe_error": "expired"})
                    if not idle():
                        return failure.model_copy(update={"safe_error": "media_active"})
                    checked = now
                chunk = block[: min(len(block), PROBE_MAX_BYTES - sent)]
                peer.sendall(struct.pack("!I", len(chunk)) + chunk)
                sent += len(chunk)
                delay = sent * 8 / PROBE_CAP_BPS - (time.monotonic() - start)
                if delay > 0:
                    time.sleep(min(delay, 0.05))
            peer.sendall(struct.pack("!I", 0))
            result = message(peer)
            received, elapsed = result["bytes"], result["ms"]
            if (
                not isinstance(received, int)
                or isinstance(received, bool)
                or not 0 < received <= sent
                or not isinstance(elapsed, int | float)
                or not 0 < elapsed <= 15000
            ):
                return failure
            if not hmac.compare_digest(
                result.get("proof", ""),
                proof(
                    job["token"],
                    "result",
                    job["id"],
                    nonce,
                    server_nonce,
                    str(received),
                    str(elapsed),
                ),
            ):
                return failure.model_copy(update={"safe_error": "authentication_failed"})
            return ProbeProgress(
                job_id=job["id"],
                role="sender",
                state="COMPLETED",
                bytes_received=received,
                elapsed_ms=float(elapsed),
                throughput_bps=int(received * 8000 / elapsed),
            )
    except (OSError, ValueError, TypeError, KeyError, struct.error):
        return failure


def tcp_rtt(host: str, port: int, *, test_loopback: bool = False) -> tuple[bool, float | None]:
    try:
        address = ip_address(host)
        if not address.is_global and not (test_loopback and address.is_loopback):
            return False, None
        start = time.monotonic()
        with socket.create_connection((str(address), port), timeout=1):
            return True, round((time.monotonic() - start) * 1000, 2)
    except (ValueError, OSError):
        return False, None
