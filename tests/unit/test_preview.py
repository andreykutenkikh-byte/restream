from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.services.preview import (
    PreviewInvalidRequest,
    PreviewResponse,
    PreviewService,
    PreviewUnavailable,
    PreviewUpstreamError,
)

PATH_NAME = "live/private-ingest-key"
SESSION = "11111111-1111-4111-8111-111111111111"
PREFIX = "abcdef123456"
MEDIA_PLAYLIST = "video1_stream.m3u8"
SEGMENT = f"{PREFIX}_video1_seg7.mp4"


async def response_body(response: PreviewResponse) -> bytes:
    return b"".join([chunk async for chunk in response.body])


class HLSUpstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"].startswith("Basic ")
        asset = request.url.path.rsplit("/", 1)[-1]
        if asset == "index.m3u8":
            assert dict(request.url.params) == {"cookieCheck": "1"}
            assert request.headers["cookie"] == "cookieCheck=1"
            return httpx.Response(
                200,
                headers=[
                    ("Content-Type", "application/vnd.apple.mpegurl"),
                    ("Set-Cookie", f"hlsSession={SESSION}"),
                    ("Set-Cookie", f"hlsSession={SESSION}; Secure; HttpOnly; Partitioned"),
                    ("Access-Control-Allow-Origin", "*"),
                    ("Server", "internal-mediamtx"),
                ],
                stream=httpx.ByteStream(
                    (
                        "#EXTM3U\n"
                        "#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
                        f"{MEDIA_PLAYLIST}?session={SESSION}\n"
                    ).encode()
                ),
            )
        assert request.headers["cookie"] == f"hlsSession={SESSION}"
        if asset == MEDIA_PLAYLIST:
            return httpx.Response(
                200,
                headers={"Set-Cookie": "unexpected=secret", "Access-Control-Allow-Origin": "*"},
                stream=httpx.ByteStream(
                    (
                        "#EXTM3U\n"
                        f'#EXT-X-MAP:URI="{PREFIX}_video1_init.mp4?session={SESSION}"\n'
                        f'#EXT-X-PART:DURATION=0.2,URI="{PREFIX}_video1_part8.mp4"\n'
                        f"{SEGMENT}?session={SESSION}\n"
                    ).encode()
                ),
            )
        if asset == SEGMENT:
            status_code = 206 if request.headers.get("range") else 200
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": "6",
                "Accept-Ranges": "bytes",
                "Set-Cookie": "unexpected=secret",
                "Access-Control-Allow-Origin": "*",
            }
            if status_code == 206:
                headers["Content-Range"] = "bytes 0-5/6"
            return httpx.Response(status_code, headers=headers, stream=httpx.ByteStream(b"media!"))
        return httpx.Response(404, content=b"path may contain a secret")


def service(upstream: HLSUpstream) -> PreviewService:
    return PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(upstream),
    )


@pytest.mark.asyncio
async def test_playlist_uses_private_session_and_rewrites_every_uri() -> None:
    upstream = HLSUpstream()
    preview = service(upstream)
    try:
        master = await preview.open(PATH_NAME, "index.m3u8")
        master_body = (await response_body(master)).decode()
        media = await preview.open(PATH_NAME, MEDIA_PLAYLIST)
        media_body = (await response_body(media)).decode()
    finally:
        await preview.close()

    assert master.headers == {
        "Content-Type": "application/vnd.apple.mpegurl",
        "Content-Length": str(len(master_body.encode())),
        "Cache-Control": "private, no-store, max-age=0",
    }
    assert f"/api/ingest/preview/{MEDIA_PLAYLIST}" in master_body
    assert f"/api/ingest/preview/{PREFIX}_video1_init.mp4" in media_body
    assert f"/api/ingest/preview/{PREFIX}_video1_part8.mp4" in media_body
    assert f"/api/ingest/preview/{SEGMENT}" in media_body
    combined = master_body + media_body
    assert "private-ingest-key" not in combined
    assert "mediamtx" not in combined
    assert "session=" not in combined
    assert "hlsSession" not in combined
    assert "Set-Cookie" not in media.headers
    assert "Access-Control-Allow-Origin" not in media.headers


@pytest.mark.asyncio
async def test_unexpected_upstream_playlist_uri_is_an_upstream_error() -> None:
    async def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Set-Cookie": f"hlsSession={SESSION}"},
            stream=httpx.ByteStream(b"#EXTM3U\nhttps://example.test/external.m3u8\n"),
        )

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(malformed),
    )
    try:
        with pytest.raises(PreviewUpstreamError, match="Invalid internal HLS playlist"):
            await preview.open(PATH_NAME, "index.m3u8")
    finally:
        await preview.close()


@pytest.mark.asyncio
async def test_segment_is_streamed_with_filtered_headers_and_single_range() -> None:
    upstream = HLSUpstream()
    preview = service(upstream)
    try:
        await response_body(await preview.open(PATH_NAME, "index.m3u8"))
        segment = await preview.open(PATH_NAME, SEGMENT, range_header="bytes=0-5")
        payload = await response_body(segment)
    finally:
        await preview.close()

    assert segment.status_code == 206
    assert payload == b"media!"
    assert segment.headers == {
        "Content-Type": "video/mp4",
        "Cache-Control": "private, no-store, max-age=0",
        "Content-Length": "6",
        "Accept-Ranges": "bytes",
        "Content-Range": "bytes 0-5/6",
    }
    assert upstream.requests[-1].headers["range"] == "bytes=0-5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset",
    (
        "..",
        "../index.m3u8",
        "%2e%2e",
        "https://example.test/video.mp4",
        "arbitrary.mp4",
        "abcdef123456_video1_script.js",
        "трансляция.m3u8",
    ),
)
async def test_asset_allowlist_rejects_traversal_and_arbitrary_names(asset: str) -> None:
    upstream = HLSUpstream()
    preview = service(upstream)
    try:
        with pytest.raises(PreviewInvalidRequest):
            await preview.open(PATH_NAME, asset)
    finally:
        await preview.close()

    assert upstream.requests == []


@pytest.mark.asyncio
async def test_only_ll_hls_query_parameters_are_forwarded() -> None:
    upstream = HLSUpstream()
    preview = service(upstream)
    try:
        await response_body(await preview.open(PATH_NAME, "index.m3u8"))
        response = await preview.open(
            PATH_NAME,
            MEDIA_PLAYLIST,
            query_items=(("_HLS_msn", "7"), ("_HLS_part", "2"), ("_HLS_skip", "YES")),
        )
        await response_body(response)
        with pytest.raises(PreviewInvalidRequest):
            await preview.open(
                PATH_NAME,
                MEDIA_PLAYLIST,
                query_items=(("url", "http://example.test/stream"),),
            )
    finally:
        await preview.close()

    assert dict(upstream.requests[-1].url.params) == {
        "_HLS_msn": "7",
        "_HLS_part": "2",
        "_HLS_skip": "YES",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("range_header", ("bytes=0-1,4-5", "items=0-5", "bytes=-"))
async def test_unsafe_or_multiple_ranges_are_rejected(range_header: str) -> None:
    upstream = HLSUpstream()
    preview = service(upstream)
    try:
        with pytest.raises(PreviewInvalidRequest):
            await preview.open(PATH_NAME, SEGMENT, range_header=range_header)
    finally:
        await preview.close()


@pytest.mark.asyncio
async def test_offline_stream_becomes_safe_unavailable_error() -> None:
    async def offline(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"live/private-ingest-key")

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(offline),
    )
    try:
        with pytest.raises(PreviewUnavailable) as captured:
            await preview.open(PATH_NAME, "index.m3u8")
    finally:
        await preview.close()

    assert "private-ingest-key" not in str(captured.value)
    assert "mediamtx" not in str(captured.value)


@pytest.mark.asyncio
async def test_upstream_timeout_has_no_internal_url_or_key() -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("internal URL with private-ingest-key", request=request)

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(timeout),
    )
    try:
        with pytest.raises(PreviewUpstreamError) as captured:
            await preview.open(PATH_NAME, "index.m3u8")
    finally:
        await preview.close()

    message = str(captured.value)
    assert "private-ingest-key" not in message
    assert "mediamtx" not in message


@pytest.mark.asyncio
async def test_expired_private_session_is_renewed_once() -> None:
    sessions = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    index_requests = 0

    async def expiring(request: httpx.Request) -> httpx.Response:
        nonlocal index_requests
        asset = request.url.path.rsplit("/", 1)[-1]
        if asset == "index.m3u8":
            session = sessions[index_requests]
            index_requests += 1
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"hlsSession={session}"},
                stream=httpx.ByteStream(f"#EXTM3U\n{MEDIA_PLAYLIST}\n".encode()),
            )
        if request.headers.get("cookie") == f"hlsSession={sessions[0]}":
            return httpx.Response(401)
        return httpx.Response(200, stream=httpx.ByteStream(f"#EXTM3U\n{SEGMENT}\n".encode()))

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(expiring),
    )
    try:
        await response_body(await preview.open(PATH_NAME, "index.m3u8"))
        media = await preview.open(PATH_NAME, MEDIA_PLAYLIST)
        assert SEGMENT in (await response_body(media)).decode()
    finally:
        await preview.close()

    assert index_requests == 2


@pytest.mark.asyncio
async def test_concurrent_expired_session_requests_share_one_renewal() -> None:
    sessions = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    index_requests = 0

    async def expiring(request: httpx.Request) -> httpx.Response:
        nonlocal index_requests
        asset = request.url.path.rsplit("/", 1)[-1]
        if asset == "index.m3u8":
            session = sessions[index_requests]
            index_requests += 1
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"hlsSession={session}"},
                stream=httpx.ByteStream(f"#EXTM3U\n{MEDIA_PLAYLIST}\n".encode()),
            )
        if request.headers.get("cookie") == f"hlsSession={sessions[0]}":
            await asyncio.sleep(0)
            return httpx.Response(401)
        return httpx.Response(200, stream=httpx.ByteStream(f"#EXTM3U\n{SEGMENT}\n".encode()))

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(expiring),
    )
    try:
        await response_body(await preview.open(PATH_NAME, "index.m3u8"))
        responses = await asyncio.gather(
            preview.open(PATH_NAME, MEDIA_PLAYLIST),
            preview.open(PATH_NAME, MEDIA_PLAYLIST),
        )
        await asyncio.gather(*(response_body(response) for response in responses))
    finally:
        await preview.close()

    assert index_requests == 2


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"media!"

    async def aclose(self) -> None:
        self.closed = True


class OversizedPlaylistStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"#EXTM3U\n"
        yield b"#" + b"x" * (512 * 1024)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_oversized_playlist_is_rejected_and_closed() -> None:
    stream = OversizedPlaylistStream()

    async def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Set-Cookie": f"hlsSession={SESSION}"},
            stream=stream,
        )

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(oversized),
    )
    try:
        with pytest.raises(PreviewUpstreamError, match="too large"):
            await preview.open(PATH_NAME, "index.m3u8")
    finally:
        await preview.close()

    assert stream.closed is True


@pytest.mark.asyncio
async def test_streaming_response_closes_upstream_body() -> None:
    stream = TrackingStream()

    async def streaming(request: httpx.Request) -> httpx.Response:
        asset = request.url.path.rsplit("/", 1)[-1]
        if asset == "index.m3u8":
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"hlsSession={SESSION}"},
                stream=httpx.ByteStream(f"#EXTM3U\n{MEDIA_PLAYLIST}\n".encode()),
            )
        return httpx.Response(200, stream=stream)

    preview = PreviewService(
        "http://mediamtx:8888",
        username="worker",
        password="independent-worker-password",
        transport=httpx.MockTransport(streaming),
    )
    try:
        await response_body(await preview.open(PATH_NAME, "index.m3u8"))
        response = await preview.open(PATH_NAME, SEGMENT)
        assert await response_body(response) == b"media!"
    finally:
        await preview.close()

    assert stream.closed is True


@pytest.mark.parametrize(
    "base_url",
    (
        "file:///tmp/hls",
        "http://worker:secret@mediamtx:8888",
        "http://mediamtx:8888/live/key",
        "http://mediamtx:8888?url=http://example.test",
    ),
)
def test_fixed_upstream_must_be_an_http_origin(base_url: str) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\) origin"):
        PreviewService(base_url, username="worker", password="password")
