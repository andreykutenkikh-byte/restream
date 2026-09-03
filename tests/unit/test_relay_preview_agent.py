from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from relay_agent.client import ControlClient
from relay_agent.errors import RelayAgentError
from relay_agent.models import HostMetrics, RelaySnapshot
from relay_agent.preview import (
    MAX_PLAYLIST_BYTES,
    MAX_SEGMENT_BYTES,
    LocalHLSReader,
    LocalSegment,
    PreviewPump,
    parse_master_playlist,
    parse_media_playlist,
    validate_mpegts,
)
from relay_agent.security import SensitiveToken
from relay_agent.service import AgentService

SESSION_UUID4 = "123e4567-e89b-42d3-a456-426614174000"


def media_playlist(*names: str, sequence: int = 7) -> bytes:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
        "#EXT-X-TARGETDURATION:2",
    ]
    for name in names:
        lines.extend(("#EXTINF:2.000,", name))
    return ("\n".join(lines) + "\n").encode("ascii")


def ts_segment(marker: int = 0) -> bytes:
    return (bytes((0x47, marker)) + bytes(186)) * 3


def test_strict_playlist_parser_accepts_plain_relative_mpegts_names() -> None:
    master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\nvideo.m3u8\n"
    assert parse_master_playlist(master) == "video.m3u8"
    assert parse_media_playlist(media_playlist("seg7.ts", "seg8.ts")) == [
        LocalSegment(7, "seg7.ts"),
        LocalSegment(8, "seg8.ts"),
    ]


def test_strict_playlist_parser_accepts_mediamtx_session_resources() -> None:
    master_resource = f"video.m3u8?session={SESSION_UUID4}"
    master = (f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\n{master_resource}\n").encode("ascii")
    segment_resources = (
        f"seg7.ts?session={SESSION_UUID4}",
        f"seg8.ts?session={SESSION_UUID4}",
    )

    assert parse_master_playlist(master) == master_resource
    assert parse_media_playlist(media_playlist(*segment_resources)) == [
        LocalSegment(7, segment_resources[0]),
        LocalSegment(8, segment_resources[1]),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../secret.ts",
        "/absolute.ts",
        "http://example.test/a.ts",
        "segment.ts?token=value",
        f"segment.ts?session={SESSION_UUID4}&extra=value",
        f"segment.ts?extra=value&session={SESSION_UUID4}",
        f"segment.ts?session={SESSION_UUID4}&session={SESSION_UUID4}",
        "segment.ts?session=123e4567-e89b-12d3-a456-426614174000",
        "segment.ts?session=123e4567-e89b-42d3-7456-426614174000",
        "segment.ts?session=123E4567-E89B-42D3-A456-426614174000",
        "segment.ts?session=123e4567-e89b-42d3-a456-42661417400%30",
        f"segment.ts?session={SESSION_UUID4}#fragment",
        "segment.ts?",
        "segment%2ets",
        "segment.mp4",
    ],
)
def test_media_playlist_rejects_traversal_urls_queries_and_non_ts(name: str) -> None:
    with pytest.raises(RelayAgentError, match="preview_playlist_invalid"):
        parse_media_playlist(media_playlist(name))


def test_media_playlist_requires_one_bounded_sequence() -> None:
    with pytest.raises(RelayAgentError, match="preview_playlist_invalid"):
        parse_media_playlist(b"#EXTM3U\nsegment.ts\n")
    with pytest.raises(RelayAgentError, match="preview_playlist_invalid"):
        parse_media_playlist(media_playlist("segment.ts", "next.ts", sequence=2**63 - 1))


@pytest.mark.parametrize(
    "resource",
    [
        "../video.m3u8",
        "/video.m3u8",
        "https://example.test/video.m3u8",
        f"video.m3u8?session={SESSION_UUID4}&extra=value",
        f"video.m3u8?session={SESSION_UUID4}#fragment",
        "video.m3u8?session=123e4567-e89b-12d3-a456-426614174000",
        "video.m3u8?session=123E4567-E89B-42D3-A456-426614174000",
        "video%2em3u8",
        "video.m3u8?",
    ],
)
def test_master_playlist_rejects_noncanonical_or_unsafe_resources(resource: str) -> None:
    payload = (f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\n{resource}\n").encode("ascii")
    with pytest.raises(RelayAgentError, match="preview_playlist_invalid"):
        parse_master_playlist(payload)


def test_local_hls_reader_fetches_exact_mediamtx_session_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist_resource = f"video.m3u8?session={SESSION_UUID4}"
    segment_resource = f"seg7.ts?session={SESSION_UUID4}"
    responses = [
        (
            "application/vnd.apple.mpegurl",
            (f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\n{playlist_resource}\n").encode(
                "ascii"
            ),
        ),
        (
            "application/vnd.apple.mpegurl",
            media_playlist(segment_resource),
        ),
        ("video/mp2t", ts_segment()),
    ]
    requests: list[tuple[str, str]] = []

    class Headers:
        def __init__(self, content_type: str) -> None:
            self._content_type = content_type

        def get_content_type(self) -> str:
            return self._content_type

    class Response:
        status = 200

        def __init__(self, content_type: str, payload: bytes) -> None:
            self.headers = Headers(content_type)
            self._payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert len(self._payload) <= limit
            return self._payload

    class Opener:
        def open(self, request: object, *, timeout: float) -> Response:
            requests.append((request.full_url, request.headers["Accept"]))  # type: ignore[attr-defined]
            assert timeout == 4.0
            return Response(*responses.pop(0))

    monkeypatch.setattr("relay_agent.preview.build_opener", lambda *_handlers: Opener())
    reader = LocalHLSReader(SensitiveToken.parse("p" * 32))

    assert reader.completed_segments() == [LocalSegment(7, segment_resource)]
    assert reader.read_segment(segment_resource) == ts_segment()
    assert requests == [
        (
            "http://127.0.0.1:8888/relay-output/index.m3u8?cookieCheck=1",
            "application/vnd.apple.mpegurl",
        ),
        (
            f"http://127.0.0.1:8888/relay-output/{playlist_resource}",
            "application/vnd.apple.mpegurl",
        ),
        (
            f"http://127.0.0.1:8888/relay-output/{segment_resource}",
            "video/mp2t",
        ),
    ]


@pytest.mark.parametrize(
    ("path", "playlist"),
    [
        (f"/relay-output/video.m3u8?session={SESSION_UUID4}&extra=value", True),
        (f"/relay-output/video.m3u8?session={SESSION_UUID4}#fragment", True),
        ("/relay-output/video.m3u8?session=123E4567-E89B-42D3-A456-426614174000", True),
        (f"/relay-output/subdir/video.m3u8?session={SESSION_UUID4}", True),
        (f"/relay-output/segment.ts?session={SESSION_UUID4}/extra", False),
        (f"/iphone-live/segment.ts?session={SESSION_UUID4}", False),
        (f"/other/segment.ts?session={SESSION_UUID4}", False),
    ],
)
def test_local_hls_reader_rejects_unsafe_request_targets_before_network(
    monkeypatch: pytest.MonkeyPatch, path: str, playlist: bool
) -> None:
    class Opener:
        @staticmethod
        def open(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("unsafe request reached the network")

    monkeypatch.setattr("relay_agent.preview.build_opener", lambda *_handlers: Opener())
    reader = LocalHLSReader(SensitiveToken.parse("p" * 32))

    with pytest.raises(RelayAgentError, match="preview_local_request_invalid"):
        reader._fetch(
            path,
            MAX_PLAYLIST_BYTES if playlist else MAX_SEGMENT_BYTES,
            playlist=playlist,
        )


def test_preview_pump_uploads_each_completed_segment_once_and_rotates_generation() -> None:
    class Reader:
        def completed_segments(self) -> list[LocalSegment]:
            return [LocalSegment(10, "a.ts"), LocalSegment(11, "b.ts")]

        def read_segment(self, name: str) -> bytes:
            return ts_segment(1 if name == "a.ts" else 2)

    class Uploader:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, int, bytes]] = []

        def upload_preview_segment(self, generation: str, sequence: int, payload: bytes) -> None:
            self.uploads.append((generation, sequence, payload))

    generations = iter((str(uuid4()), str(uuid4())))
    uploader = Uploader()
    pump = PreviewPump(Reader(), uploader, generation_factory=lambda: next(generations))  # type: ignore[arg-type]
    pump.set_requested(True)
    first_generation = pump._generation
    assert first_generation is not None
    pump._pump_once(first_generation)
    pump._pump_once(first_generation)
    assert [upload[1] for upload in uploader.uploads] == [10, 11]

    pump.set_requested(False)
    pump.set_requested(True)
    second_generation = pump._generation
    assert second_generation is not None and second_generation != first_generation
    pump._pump_once(second_generation)
    assert [upload[1] for upload in uploader.uploads] == [10, 11, 10, 11]


def test_preview_pump_rotates_generation_when_local_hls_sequence_restarts() -> None:
    class Reader:
        segments = [LocalSegment(10, "old.ts")]

        def completed_segments(self) -> list[LocalSegment]:
            return self.segments

        def read_segment(self, _name: str) -> bytes:
            return ts_segment()

    class Uploader:
        def __init__(self) -> None:
            self.generations: list[str] = []

        def upload_preview_segment(self, generation: str, _sequence: int, _payload: bytes) -> None:
            self.generations.append(generation)

    first, second = str(uuid4()), str(uuid4())
    generations = iter((first, second))
    reader = Reader()
    uploader = Uploader()
    pump = PreviewPump(  # type: ignore[arg-type]
        reader,
        uploader,
        generation_factory=lambda: next(generations),
    )
    pump.set_requested(True)
    pump._pump_once(first)
    reader.segments = [LocalSegment(1, "new.ts")]
    pump._pump_once(first)

    assert uploader.generations == [first, second]


def test_agent_mpegts_validation_rejects_bad_sync_without_exposing_payload() -> None:
    sentinel = b"SECRET_PREVIEW_SENTINEL"
    with pytest.raises(RelayAgentError) as raised:
        validate_mpegts(sentinel.ljust(188, b"x"))
    assert sentinel.decode("ascii") not in str(raised.value)


def test_control_client_accepts_optional_preview_demand_and_uses_separate_media_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = str(uuid4())
    captured: list[tuple[str, str, bytes | None, dict[str, str]]] = []
    responses = [
        (
            200,
            "application/json",
            json.dumps(
                {
                    "status": "ok",
                    "node_id": str(uuid4()),
                    "heartbeat_interval_seconds": 5,
                    "command_poll_interval_seconds": 5,
                    "preview_requested": True,
                }
            ).encode("ascii"),
        ),
        (204, "", b""),
        (204, "", b""),
    ]

    class Response:
        def __init__(self, response: tuple[int, str, bytes]) -> None:
            self.status, self.content_type, self.body = response

        def getheader(self, name: str, default: str = "") -> str:
            return self.content_type if name.lower() == "content-type" else default

        def read(self, _limit: int) -> bytes:
            return self.body

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.response = Response(responses.pop(0))

        def request(
            self, method: str, path: str, body: bytes | None, headers: dict[str, str]
        ) -> None:
            captured.append((method, path, body, headers))

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr("relay_agent.client.http.client.HTTPSConnection", Connection)
    client = ControlClient(SensitiveToken.parse("n" * 32))
    relay = RelaySnapshot(
        "active",
        False,
        "running",
        "listening",
        "LIVE",
        "active",
        "healthy",
        True,
        True,
        True,
        True,
        None,
        4_000_000,
    )
    intervals = client.heartbeat(
        hostname="hk-relay",
        relay=relay,
        host=HostMetrics(100, 0.1, 1.0, 1000, 500, 2000, 1000),
        current_command_id=None,
    )
    assert intervals.preview_requested is True

    assert client.next_command(wait_seconds=0) is None
    method, path, sent, headers = captured[-1]
    assert method == "GET"
    assert path == "/relay-agent/v1/commands/next?wait=0"
    assert sent is None
    assert headers["X-Relay-Agent-Version"] == "1.2.3"

    payload = ts_segment()
    client.upload_preview_segment(generation, 22, payload)
    method, path, sent, headers = captured[-1]
    assert method == "PUT"
    assert path == f"/relay-media/v1/preview/segments/{generation}/22"
    assert sent == payload
    assert headers["Content-Type"] == "video/mp2t"
    assert headers["Content-Length"] == str(len(payload))


def test_control_client_rejects_unknown_heartbeat_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = json.dumps(
        {
            "status": "ok",
            "node_id": str(uuid4()),
            "heartbeat_interval_seconds": 5,
            "command_poll_interval_seconds": 5,
            "preview_requested": False,
            "untrusted": "field",
        }
    ).encode("ascii")

    class Response:
        status = 200

        @staticmethod
        def getheader(name: str, default: str = "") -> str:
            return "application/json" if name.lower() == "content-type" else default

        @staticmethod
        def read(_limit: int) -> bytes:
            return response_body

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def getresponse() -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr("relay_agent.client.http.client.HTTPSConnection", Connection)
    client = ControlClient(SensitiveToken.parse("n" * 32))
    relay = RelaySnapshot.unavailable()
    with pytest.raises(RelayAgentError, match="invalid_control_response"):
        client.heartbeat(
            hostname="hk-relay",
            relay=relay,
            host=HostMetrics(100, 0.1, 1.0, 1000, 500, 2000, 1000),
            current_command_id=None,
        )


@pytest.mark.parametrize(("source", "expected"), [("LIVE", True), ("SLATE", False)])
def test_agent_service_enables_preview_only_for_a_live_snapshot(
    source: str, expected: bool
) -> None:
    class StopAfterOne:
        calls = 0

        def wait(self, _delay: float) -> bool:
            self.calls += 1
            return self.calls > 1

    relay = RelaySnapshot(
        "active",
        False,
        "running",
        "listening",
        source,  # type: ignore[arg-type]
        "active",
        "healthy",
        True,
        True,
        True,
        True,
        None,
        4_000_000 if source == "LIVE" else None,
    )

    class Control:
        @staticmethod
        def heartbeat(**_kwargs: object) -> object:
            return SimpleNamespace(preview_requested=True)

    class Processor:
        @staticmethod
        def status() -> RelaySnapshot:
            return relay

    class Metrics:
        @staticmethod
        def collect() -> HostMetrics:
            return HostMetrics(100, 0.1, 1.0, 1000, 500, 2000, 1000)

    class Preview:
        requests: list[bool] = []

        def set_requested(self, requested: bool) -> None:
            self.requests.append(requested)

    preview = Preview()
    service = AgentService(
        control=Control(),  # type: ignore[arg-type]
        processor=Processor(),  # type: ignore[arg-type]
        metrics=Metrics(),  # type: ignore[arg-type]
        stop_event=StopAfterOne(),  # type: ignore[arg-type]
        preview=preview,  # type: ignore[arg-type]
    )

    service._heartbeat_loop()

    assert preview.requests == [expected]
