from __future__ import annotations

import asyncio
import traceback
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app.services.mediamtx import (
    HttpxJsonTransport,
    IngestState,
    MediaMTXClient,
    MediaMTXNotFound,
    MediaMTXTransportError,
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


def test_http_transport_tracebacks_never_expose_ingest_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_key = "TRACEBACK_STREAM_KEY_MUST_NOT_LEAK"
    request = httpx.Request(
        "GET",
        f"http://mediamtx:9997/v3/paths/get/live%2F{ingest_key}",
    )
    outcomes: tuple[httpx.Response | httpx.HTTPError, ...] = (
        httpx.ConnectError("connection failed", request=request),
        httpx.Response(500, request=request, json={"error": ingest_key}),
    )

    class FakeAsyncClient:
        def __init__(self, outcome: httpx.Response | httpx.HTTPError) -> None:
            self.outcome = outcome

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def request(self, *_: Any) -> httpx.Response:
            if isinstance(self.outcome, httpx.HTTPError):
                raise self.outcome
            return self.outcome

    for outcome in outcomes:

        def client_factory(*_: Any, _outcome: Any = outcome, **__: Any) -> FakeAsyncClient:
            return FakeAsyncClient(_outcome)

        monkeypatch.setattr(httpx, "AsyncClient", client_factory)
        transport = HttpxJsonTransport("http://mediamtx:9997")

        with pytest.raises(MediaMTXTransportError) as captured:
            asyncio.run(transport.get_json(f"/v3/paths/get/live%2F{ingest_key}"))

        rendered = "".join(traceback.format_exception(captured.value))
        assert ingest_key not in rendered
        assert captured.value.__cause__ is None


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
                "available": True,
                "availableTime": "2026-07-12T10:00:00Z",
                "readyTime": "2026-07-12T09:00:00Z",
                "inboundBytes": 42,
                "bytesReceived": 7,
                "tracks": ["VP9", "Opus"],
                "tracks2": [
                    {
                        "codec": "H264",
                        "codecProps": {
                            "width": 1920,
                            "height": 1080,
                            "profile": "High",
                            "level": "4.2",
                        },
                    },
                    {
                        "codec": "MPEG4Audio",
                        "codecProps": {"sampleRate": 48000, "channelCount": 2},
                    },
                ],
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
    assert status.metadata.width == 1920
    assert status.metadata.height == 1080
    assert status.metadata.fps is None
    assert status.metadata.tracks == ("h264", "aac")
    assert status.bytes_received == 42
    assert status.since is not None
    assert status.since.hour == 10


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


def test_kick_publishers_is_idempotent_when_publisher_disappears() -> None:
    class DisconnectedTransport(FakeTransport):
        async def get_json(self, path: str) -> Mapping[str, Any] | None:
            self.get_paths.append(path)
            if len(self.get_paths) == 1:
                return self.payload
            raise MediaMTXNotFound("publisher already disconnected")

        async def post_json(self, path: str) -> Mapping[str, Any] | None:
            self.post_paths.append(path)
            raise MediaMTXNotFound("publisher already disconnected")

    transport = DisconnectedTransport(
        {
            "name": "live/key",
            "source": {"type": "rtmpConn", "id": "publisher-id"},
            "ready": True,
        }
    )
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    assert asyncio.run(client.kick_publishers("live/key")) == 0
    assert transport.post_paths == ["/v3/rtmpconns/kick/publisher-id"]
    assert transport.get_paths == ["/v3/paths/get/live%2Fkey"] * 2


def test_kick_publishers_fails_closed_when_same_publisher_survives_kick_404() -> None:
    class UnsupportedKickTransport(FakeTransport):
        async def post_json(self, path: str) -> Mapping[str, Any] | None:
            self.post_paths.append(path)
            raise MediaMTXNotFound("kick route unavailable")

    transport = UnsupportedKickTransport(
        {
            "name": "live/key",
            "source": {"type": "rtmpConn", "id": "publisher-id"},
            "ready": True,
        }
    )
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    with pytest.raises(MediaMTXNotFound, match="kick route unavailable"):
        asyncio.run(client.kick_publishers("live/key"))
    assert transport.post_paths == ["/v3/rtmpconns/kick/publisher-id"]
    assert transport.get_paths == ["/v3/paths/get/live%2Fkey"] * 2


def test_kick_publishers_fails_closed_when_publisher_is_replaced_after_kick_404() -> None:
    class ReplacedPublisherTransport(FakeTransport):
        async def get_json(self, path: str) -> Mapping[str, Any] | None:
            self.get_paths.append(path)
            if len(self.get_paths) == 1:
                return self.payload
            return {
                "name": "live/key",
                "source": {"type": "rtmpConn", "id": "replacement-publisher-id"},
                "ready": True,
            }

        async def post_json(self, path: str) -> Mapping[str, Any] | None:
            self.post_paths.append(path)
            raise MediaMTXNotFound("original publisher disappeared before kick")

    transport = ReplacedPublisherTransport(
        {
            "name": "live/key",
            "source": {"type": "rtmpConn", "id": "original-publisher-id"},
            "ready": True,
        }
    )
    client = MediaMTXClient("http://mediamtx:9997", transport=transport)

    with pytest.raises(MediaMTXNotFound, match="original publisher disappeared before kick"):
        asyncio.run(client.kick_publishers("live/key"))
    assert transport.post_paths == ["/v3/rtmpconns/kick/original-publisher-id"]
    assert transport.get_paths == ["/v3/paths/get/live%2Fkey"] * 2
