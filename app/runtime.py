"""Application service composition and secret-safe view models."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.core.config import Settings
from app.core.redaction import redact_text
from app.core.security import (
    decrypt_destination_key,
    encrypt_destination_key,
    generate_ingest_key,
)
from app.core.validation import ValidatedDestinationURL, validate_destination_url
from app.db import Database
from app.services.bitrate import IngestBitrateSampler
from app.services.mediamtx import IngestStatus, MediaMTXClient
from app.services.workers import (
    DestinationSpec,
    ReconnectPolicy,
    WorkerManager,
    WorkerRuntimeConfig,
    WorkerStatus,
)

URLValidator = Callable[[str], ValidatedDestinationURL]


class ApplicationRuntime:
    """Wires persistence, MediaMTX status, and independent FFmpeg workers."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        mediamtx: MediaMTXClient | None = None,
        bitrate_sampler: IngestBitrateSampler | None = None,
        url_validator: URLValidator = validate_destination_url,
        worker_launcher: Any | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.mediamtx = mediamtx or MediaMTXClient(settings.mediamtx_api_url)
        self._bitrate_sampler = bitrate_sampler or IngestBitrateSampler()
        self.url_validator = url_validator
        self.workers = WorkerManager(
            self._destination_spec,
            self.ingest_status,
            launcher=worker_launcher,
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=settings.reconnect_initial_seconds,
                max_delay_seconds=settings.reconnect_max_seconds,
                max_fast_failures=settings.reconnect_max_fast_failures,
                stable_after_seconds=settings.reconnect_stable_seconds,
            ),
            runtime=WorkerRuntimeConfig(ffmpeg_executable=settings.ffmpeg_binary),
            status_callback=self._persist_worker_status,
        )
        self._rotation_lock = asyncio.Lock()

    def initialize_ingest(self) -> None:
        encrypted = self.database.get_ingest_encrypted()
        if encrypted is None:
            key = generate_ingest_key()
            self.database.set_ingest_encrypted(
                encrypt_destination_key(key, self.settings.master_encryption_key)
            )
            self.database.add_audit_event("ingest.initialized")
            return
        # Fail at startup if the configured master key cannot decrypt existing data.
        decrypt_destination_key(encrypted, self.settings.master_encryption_key)

    async def startup(self) -> None:
        self.initialize_ingest()
        enabled = [
            int(destination["id"])
            for destination in self.database.list_destinations()
            if bool(destination["enabled"])
        ]
        await self.workers.reconcile(enabled)

    async def shutdown(self) -> None:
        await self.workers.shutdown()

    def ingest_key(self) -> str:
        encrypted = self.database.get_ingest_encrypted()
        if encrypted is None:
            raise RuntimeError("Ingest configuration is not initialized")
        return decrypt_destination_key(encrypted, self.settings.master_encryption_key)

    def ingest_path(self, key: str | None = None) -> str:
        return f"live/{key or self.ingest_key()}"

    async def ingest_status(self) -> IngestStatus:
        return await self.mediamtx.get_ingest_status(self.ingest_path())

    async def rotate_ingest_key(self) -> str:
        async with self._rotation_lock:
            old_key = self.ingest_key()
            old_path = self.ingest_path(old_key)
            new_key = generate_ingest_key()
            old_encrypted = encrypt_destination_key(old_key, self.settings.master_encryption_key)
            new_encrypted = encrypt_destination_key(new_key, self.settings.master_encryption_key)
            self.database.set_ingest_encrypted(new_encrypted)
            try:
                await self.mediamtx.kick_publishers(old_path)
            except (Exception, asyncio.CancelledError):
                # A reported rotation must also revoke the active publisher. If
                # MediaMTX cannot be reached, restore the still-working key.
                self.database.set_ingest_encrypted(old_encrypted)
                raise
            self._bitrate_sampler.reset()
            self.workers.notify_ingest_changed()
            self.database.add_audit_event("ingest.rotated")
            return new_key

    def authorize_mediamtx(
        self, *, action: str, protocol: str, path: str, user: str, password: str
    ) -> bool:
        expected_path = self.ingest_path()
        if action == "publish":
            return protocol == "rtmp" and hmac.compare_digest(path, expected_path)
        if action == "read":
            return (
                protocol in {"rtmp", "hls"}
                and hmac.compare_digest(path, expected_path)
                and hmac.compare_digest(user, self.settings.worker_auth_user)
                and hmac.compare_digest(password, self.settings.worker_auth_password)
            )
        return False

    async def _destination_spec(self, destination_id: int | str) -> DestinationSpec:
        destination = self.database.get_destination(int(destination_id))
        if destination is None:
            raise LookupError("Destination does not exist")
        # Resolve again immediately before process launch to mitigate DNS rebinding.
        self.url_validator(str(destination["server_url"]))
        destination_key = decrypt_destination_key(
            str(destination["stream_key_encrypted"]), self.settings.master_encryption_key
        )
        ingest_key = self.ingest_key()
        auth_query = urlencode(
            {
                "user": self.settings.worker_auth_user,
                "pass": self.settings.worker_auth_password,
            }
        )
        input_path = self.ingest_path(ingest_key)
        input_url = f"{self.settings.mediamtx_internal_rtmp_url}/{input_path}?{auth_query}"
        return DestinationSpec.from_parts(
            destination_id,
            input_url=input_url,
            server_url=str(destination["server_url"]),
            stream_key=destination_key,
            secret_values=(
                destination_key,
                ingest_key,
                self.settings.worker_auth_password,
            ),
        )

    async def _persist_worker_status(self, destination_id: int | str, status: WorkerStatus) -> None:
        safe_error = redact_text(status.last_error) if status.last_error else None
        started_at = status.started_at.isoformat() if status.started_at else None
        self.database.set_destination_state(
            int(destination_id),
            status.state.value,
            error=safe_error,
            worker_pid=status.pid,
            started_at=started_at,
        )

    def validate_destination_server(self, server_url: str) -> str:
        return self.url_validator(server_url).value

    def destination_view(self, destination: dict[str, Any]) -> dict[str, Any]:
        status = self.workers.status(int(destination["id"]))
        state = status.state.value
        if not status.desired_running and state == "stopped":
            state = str(destination.get("state") or state)
            if state not in {
                "stopped",
                "waiting_for_input",
                "connecting",
                "live",
                "reconnecting",
                "failed",
            }:
                state = "stopped"
        reference = status.live_since or status.started_at
        uptime_seconds = None
        if reference:
            uptime_seconds = max(0, int((datetime.now(UTC) - reference).total_seconds()))
        return {
            "id": int(destination["id"]),
            "name": str(destination["name"]),
            "server_url": str(destination["server_url"]),
            "has_stream_key": True,
            "stream_key_masked": "••••••••••••",
            "enabled": bool(destination["enabled"]),
            "state": state,
            "last_error": status.last_error or destination.get("last_error"),
            "started_at": reference.isoformat() if reference else destination.get("started_at"),
            "uptime_seconds": uptime_seconds,
            "restart_count": status.restart_count,
            "created_at": destination["created_at"],
            "updated_at": destination["updated_at"],
        }

    def list_destination_views(self) -> list[dict[str, Any]]:
        return [self.destination_view(item) for item in self.database.list_destinations()]

    async def ingest_view(self) -> dict[str, Any]:
        ingest_path = self.ingest_path()
        status = await self.mediamtx.get_ingest_status(ingest_path)
        metadata = status.metadata.to_dict()
        bitrate_bps = self._bitrate_sampler.sample(stream_id=ingest_path, status=status)
        metadata["bitrate_bps"] = bitrate_bps
        uptime_seconds = None
        if status.since:
            uptime_seconds = max(0, int((datetime.now(UTC) - status.since).total_seconds()))
        return {
            "status": status.state.value,
            "state": status.state.value,
            **metadata,
            "metadata": metadata,
            "uptime_seconds": uptime_seconds,
            "bytes_received": status.bytes_received,
            "message": status.message,
        }
