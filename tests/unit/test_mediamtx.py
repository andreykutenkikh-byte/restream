from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.services.mediamtx import (
    IngestState,
    MediaMTXClient,
    MediaMTXNotFound,
    map_ingest_status,
    normalize_stream_metadata,
)


class FakeTransport:
    def __init__(self, payload: Mapping[str, Any] | None) -> None:
        self.payload = payload
        self.get_paths: list[str] = []
        self.post_paths: list[str] = []

    async def get_json(self, path: str) -> Mapping[str, Any] | None:
        self.get_paths.append(path)
        return self.payload

    async def post_json(self, path: str) -> Mapping[str, Any] | None:
        self.post_paths.append(path)
        return {}


def test_normalizes_ffprobe_and_mediamtx_metadata() -> None:
    metadata = normalize_stream_metadata(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "H.264",
                    "width": 1920,
                    "height": "1080",
                    "avg_frame_rate": "60000/1001",
                    "bit_rate": "8000000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "MPEG-4 Audio",
                    "bit_rate": 128000,
                },
            ]
        }
    )

    assert metadata.video_codec == "h264"
    assert metadata.audio_codec == "aac"
    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.fps == 59.94
    assert metadata.bitrate_bps == 8_128_000
    assert metadata.tracks == ("h264", "aac")


def test_maps_all_ingest_states_without_exposing_raw_errors() -> None:
    assert map_ingest_status(None).state is IngestState.OFFLINE
    assert map_ingest_status({"source": {"type": "rtmpConn"}}).state is IngestState.CONNECTING
    assert map_ingest_status({"ready": True}).state is IngestState.LIVE
    assert map_ingest_status({"ready": True, "degraded": True}).state is IngestState.UNSTABLE

    status = map_ingest_status({"error": "publisher secret failed"})
    assert status.state is IngestState.ERROR
    assert status.message == "Incoming stream status is unavailable"
    assert "secret" not in status.message


def test_async_client_encodes_path_and_returns_normalized_status() -> None:
    transport = FakeTransport(
        {
            "item": {
                "ready": True,
                "readyTime": "2026-07-12T10:00:00Z",
                "bytesReceived": 42,
                "tracks": ["H264", "MPEG-4 Audio"],
            }
        }
    )
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    status = asyncio.run(client.get_ingest_status("live/main key"))

    assert transport.get_paths == ["/v3/paths/get/live%2Fmain%20key"]
    assert status.state is IngestState.LIVE
    assert status.is_available
    assert status.metadata.video_codec == "h264"
    assert status.metadata.audio_codec == "aac"
    assert status.bytes_received == 42
    assert status.since is not None


def test_client_maps_not_found_and_arbitrary_transport_failure_safely() -> None:
    async def not_found(_: str) -> Mapping[str, Any] | None:
        raise MediaMTXNotFound("offline")

    async def failed(_: str) -> Mapping[str, Any] | None:
        raise RuntimeError("rtmp://example/live/real-secret")

    offline = asyncio.run(
        MediaMTXClient("http://unused", transport=not_found).status("secret-path")
    )
    error = asyncio.run(MediaMTXClient("http://unused", transport=failed).status("secret-path"))

    assert offline.state is IngestState.OFFLINE
    assert error.state is IngestState.ERROR
    assert "real-secret" not in (error.message or "")


def test_kick_publishers_disconnects_only_active_rtmp_source() -> None:
    transport = FakeTransport(
        {
            "name": "live/key",
            "source": {"type": "rtmpConn", "id": "publisher/id"},
            "ready": True,
        }
    )
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    kicked = asyncio.run(client.kick_publishers("live/key"))

    assert kicked == 1
    assert transport.post_paths == ["/v3/rtmpconns/kick/publisher%2Fid"]


def test_kick_publishers_is_noop_for_offline_path() -> None:
    class OfflineTransport(FakeTransport):
        async def get_json(self, path: str) -> Mapping[str, Any] | None:
            raise MediaMTXNotFound("offline")

    transport = OfflineTransport(None)
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    assert asyncio.run(client.kick_publishers("live/key")) == 0
    assert transport.post_paths == []
