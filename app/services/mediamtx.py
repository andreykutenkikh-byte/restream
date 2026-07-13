"""Small, dependency-injectable client for the MediaMTX control API.

The MediaMTX API intentionally stays behind this module.  Callers receive a
stable, public status model instead of depending on a particular MediaMTX JSON
shape.  The transport is injectable so unit tests and deployments with a
shared HTTP client do not need a running MediaMTX instance.
"""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import quote

type JsonObject = Mapping[str, Any]
type JsonFetcher = Callable[[str], Awaitable[JsonObject | None] | JsonObject | None]


class MediaMTXError(RuntimeError):
    """Base class for safe MediaMTX client errors."""


class MediaMTXNotFound(MediaMTXError):
    """The requested path is not currently known to MediaMTX."""


class MediaMTXTransportError(MediaMTXError):
    """MediaMTX could not be contacted or returned an invalid response."""


class AsyncJsonTransport(Protocol):
    """Minimal transport contract used by :class:`MediaMTXClient`."""

    async def get_json(self, path: str) -> JsonObject | None:
        """Return a decoded JSON object for an API-relative path."""


class AsyncJsonActionTransport(AsyncJsonTransport, Protocol):
    """Transport contract for MediaMTX control actions."""

    async def post_json(self, path: str) -> JsonObject | None:
        """POST an API-relative action path."""


class HttpxJsonTransport:
    """Default HTTP transport.

    ``httpx`` is imported lazily.  This keeps metadata normalization usable in
    lightweight unit-test environments while still providing an async client
    in the application image.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def _request(self, method: str, path: str) -> JsonObject | None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - application packaging failure
            raise MediaMTXTransportError("async HTTP transport is unavailable") from exc

        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(method, url)
        except httpx.HTTPError as exc:
            # Do not include ``exc`` in the message: the URL path can be a
            # stream key and therefore must never reach logs/UI diagnostics.
            raise MediaMTXTransportError("MediaMTX API is unavailable") from exc

        if response.status_code == 404:
            raise MediaMTXNotFound("MediaMTX path is offline")

        try:
            response.raise_for_status()
            if not response.content:
                return {}
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MediaMTXTransportError("MediaMTX returned an invalid response") from exc

        if not isinstance(payload, Mapping):
            raise MediaMTXTransportError("MediaMTX returned an invalid response")
        return cast(JsonObject, payload)

    async def get_json(self, path: str) -> JsonObject | None:
        return await self._request("GET", path)

    async def post_json(self, path: str) -> JsonObject | None:
        return await self._request("POST", path)


class IngestState(StrEnum):
    OFFLINE = "offline"
    CONNECTING = "connecting"
    LIVE = "live"
    UNSTABLE = "unstable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StreamMetadata:
    """Normalized subset of MediaMTX/ffprobe stream metadata."""

    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_bps: int | None = None
    tracks: tuple[str, ...] = ()

    @property
    def frame_rate(self) -> float | None:
        """Readable alias used by some API serializers."""

        return self.fps

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tracks"] = list(self.tracks)
        return result


@dataclass(frozen=True, slots=True)
class IngestStatus:
    """Secret-free ingest status returned to the rest of the application."""

    state: IngestState
    metadata: StreamMetadata = StreamMetadata()
    since: datetime | None = None
    bytes_received: int | None = None
    message: str | None = None

    @property
    def is_available(self) -> bool:
        """Whether workers can consume the incoming stream."""

        return self.state in {IngestState.LIVE, IngestState.UNSTABLE}

    @property
    def ready(self) -> bool:
        return self.is_available

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "metadata": self.metadata.to_dict(),
            "since": self.since.isoformat() if self.since else None,
            "bytes_received": self.bytes_received,
            "message": self.message,
        }


_VIDEO_CODECS = {
    "avc": "h264",
    "avc1": "h264",
    "h264": "h264",
    "h265": "h265",
    "hevc": "h265",
    "vp8": "vp8",
    "vp9": "vp9",
    "av1": "av1",
}
_AUDIO_CODECS = {
    "aac": "aac",
    "mpeg4audio": "aac",
    "mp4a": "aac",
    "mpeg4generic": "aac",
    "opus": "opus",
    "mp3": "mp3",
    "mpeg1audiolayer3": "mp3",
    "ac3": "ac3",
}


def normalize_codec(value: object) -> str | None:
    """Normalize common MediaMTX and ffprobe codec spellings."""

    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    compact = re.sub(r"[^a-z0-9]", "", text)
    if compact in _VIDEO_CODECS:
        return _VIDEO_CODECS[compact]
    if compact in _AUDIO_CODECS:
        return _AUDIO_CODECS[compact]
    return re.sub(r"\s+", "_", text)


def _codec_kind(codec: str | None) -> str | None:
    if codec in set(_VIDEO_CODECS.values()):
        return "video"
    if codec in set(_AUDIO_CODECS.values()):
        return "audio"
    return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return round(result, 3) if result > 0 and math.isfinite(result) else None


def _stream_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Any] = [payload.get("streams")]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("streams"))
    result: list[Mapping[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            result.extend(item for item in candidate if isinstance(item, Mapping))
    return result


def _track_items(payload: Mapping[str, Any]) -> list[object]:
    candidates: list[Any] = [payload.get("tracks")]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("tracks"))
    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            return list(candidate)
    return []


def normalize_stream_metadata(payload: Mapping[str, Any] | None) -> StreamMetadata:
    """Normalize MediaMTX path data or ffprobe-style stream data.

    Missing fields stay ``None``.  No raw metadata is retained, which keeps
    arbitrary publisher data out of diagnostics and API responses.
    """

    if not payload:
        return StreamMetadata()

    metadata = payload.get("metadata")
    meta = metadata if isinstance(metadata, Mapping) else {}

    video_codec = normalize_codec(_first(meta, "video_codec", "videoCodec"))
    audio_codec = normalize_codec(_first(meta, "audio_codec", "audioCodec"))
    width = _positive_int(_first(meta, "width", "video_width", "videoWidth"))
    height = _positive_int(_first(meta, "height", "video_height", "videoHeight"))
    fps = _positive_float(_first(meta, "fps", "frame_rate", "frameRate"))
    bitrate = _positive_int(_first(meta, "bitrate_bps", "bitrate", "bit_rate", "bitRate"))

    video_codec = video_codec or normalize_codec(_first(payload, "video_codec", "videoCodec"))
    audio_codec = audio_codec or normalize_codec(_first(payload, "audio_codec", "audioCodec"))
    width = width or _positive_int(_first(payload, "width", "videoWidth"))
    height = height or _positive_int(_first(payload, "height", "videoHeight"))
    fps = fps or _positive_float(_first(payload, "fps", "frameRate"))
    bitrate = bitrate or _positive_int(
        _first(payload, "bitrate_bps", "bitrate", "bit_rate", "bitRate")
    )

    tracks: list[str] = []
    stream_bitrates: list[int] = []
    for stream in _stream_items(payload):
        kind_value = _first(stream, "codec_type", "type", "media_type")
        kind = str(kind_value).strip().lower() if kind_value is not None else None
        codec = normalize_codec(_first(stream, "codec_name", "codec", "codecName"))
        kind = kind if kind in {"video", "audio"} else _codec_kind(codec)
        if codec and codec not in tracks:
            tracks.append(codec)
        if kind == "video":
            video_codec = video_codec or codec
            width = width or _positive_int(stream.get("width"))
            height = height or _positive_int(stream.get("height"))
            fps = fps or _positive_float(
                _first(stream, "avg_frame_rate", "r_frame_rate", "fps", "frameRate")
            )
        elif kind == "audio":
            audio_codec = audio_codec or codec
        stream_bitrate = _positive_int(_first(stream, "bit_rate", "bitrate", "bitRate"))
        if stream_bitrate:
            stream_bitrates.append(stream_bitrate)

    for track in _track_items(payload):
        if isinstance(track, Mapping):
            codec = normalize_codec(_first(track, "codec", "codec_name", "name", "type"))
            kind_value = _first(track, "media_type", "codec_type", "kind")
            kind = str(kind_value).strip().lower() if kind_value is not None else None
            width = width or _positive_int(track.get("width"))
            height = height or _positive_int(track.get("height"))
            fps = fps or _positive_float(_first(track, "fps", "frameRate"))
        else:
            codec = normalize_codec(track)
            kind = None
        if codec and codec not in tracks:
            tracks.append(codec)
        kind = kind if kind in {"video", "audio"} else _codec_kind(codec)
        if kind == "video":
            video_codec = video_codec or codec
        elif kind == "audio":
            audio_codec = audio_codec or codec

    if bitrate is None and stream_bitrates:
        bitrate = sum(stream_bitrates)

    # Explicit fields are useful to consumers even when MediaMTX omitted a
    # ``tracks`` array.
    for codec in (video_codec, audio_codec):
        if codec and codec not in tracks:
            tracks.append(codec)

    return StreamMetadata(
        video_codec=video_codec,
        audio_codec=audio_codec,
        width=width,
        height=height,
        fps=fps,
        bitrate_bps=bitrate,
        tracks=tuple(tracks),
    )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "ready", "live"}
    return bool(value)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _unwrap_path(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    item = payload.get("item")
    if isinstance(item, Mapping):
        return item
    return payload


def map_ingest_status(payload: Mapping[str, Any] | None) -> IngestStatus:
    """Map API-specific path data onto the Stage 1 ingest state machine."""

    if not payload:
        return IngestStatus(
            state=IngestState.OFFLINE,
            message="Incoming stream is offline",
        )

    path = _unwrap_path(payload)
    explicit_value = _first(path, "state", "status", "connectionState")
    explicit = str(explicit_value).strip().lower() if explicit_value is not None else ""
    has_error = bool(_first(path, "error", "lastError"))
    ready = _truthy(_first(path, "ready", "isReady", "online"))
    unstable = _truthy(_first(path, "unstable", "degraded"))
    source = path.get("source")
    has_source = bool(source) and not (
        isinstance(source, str) and source.lower() in {"none", "offline"}
    )

    if has_error or explicit in {"error", "failed", "unhealthy"}:
        state = IngestState.ERROR
        message = "Incoming stream status is unavailable"
    elif unstable or explicit in {"unstable", "degraded"}:
        state = IngestState.UNSTABLE if ready or has_source else IngestState.ERROR
        message = "Incoming stream is unstable"
    elif ready or explicit in {"live", "ready", "publishing", "online"}:
        state = IngestState.LIVE
        message = "Incoming stream is live"
    elif explicit in {"connecting", "starting", "initializing", "probing"} or has_source:
        state = IngestState.CONNECTING
        message = "Incoming stream is connecting"
    else:
        state = IngestState.OFFLINE
        message = "Incoming stream is offline"

    return IngestStatus(
        state=state,
        metadata=normalize_stream_metadata(path),
        since=_parse_datetime(_first(path, "readyTime", "ready_time", "since")),
        bytes_received=_positive_int(
            _first(path, "bytesReceived", "bytes_received", "receivedBytes")
        ),
        message=message,
    )


class MediaMTXClient:
    """Async facade for querying one configured ingest path."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: AsyncJsonTransport | JsonFetcher | None = None,
        timeout_seconds: float = 3.0,
        api_prefix: str = "/v3",
        status_mapper: Callable[[Mapping[str, Any] | None], IngestStatus] = map_ingest_status,
    ) -> None:
        self._transport = transport or HttpxJsonTransport(base_url, timeout_seconds=timeout_seconds)
        self._api_prefix = "/" + api_prefix.strip("/")
        self._status_mapper = status_mapper

    async def _get_json(self, path: str) -> JsonObject | None:
        transport = self._transport
        if hasattr(transport, "get_json"):
            return await cast(AsyncJsonTransport, transport).get_json(path)
        result = transport(path)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _post_json(self, path: str) -> JsonObject | None:
        transport = self._transport
        method = getattr(transport, "post_json", None)
        if method is None:
            raise MediaMTXTransportError("MediaMTX action transport is unavailable")
        result = method(path)
        if inspect.isawaitable(result):
            return await cast(Awaitable[JsonObject | None], result)
        return cast(JsonObject | None, result)

    async def get_path(self, path_name: str) -> JsonObject | None:
        """Fetch raw path data for trusted server-side integrations only."""

        if not path_name:
            raise ValueError("path_name must not be empty")
        encoded_name = quote(path_name, safe="")
        return await self._get_json(f"{self._api_prefix}/paths/get/{encoded_name}")

    async def get_ingest_status(self, path_name: str) -> IngestStatus:
        """Return a stable status; transport failures become ``error``."""

        try:
            payload = await self.get_path(path_name)
        except MediaMTXNotFound:
            return IngestStatus(
                state=IngestState.OFFLINE,
                message="Incoming stream is offline",
            )
        except Exception:
            # Dependency-injected transports may use their own exception type.
            # Keep arbitrary exception strings (and possibly URLs) out of the
            # public diagnostic model.
            return IngestStatus(
                state=IngestState.ERROR,
                message="Incoming stream status is unavailable",
            )
        return self._status_mapper(payload)

    async def status(self, path_name: str) -> IngestStatus:
        """Short alias convenient for dependency injection."""

        return await self.get_ingest_status(path_name)

    async def kick_publishers(self, path_name: str) -> int:
        """Disconnect the active RTMP publisher for a rotated ingest key.

        MediaMTX exposes the active source ID on the path record.  Stage 1
        accepts RTMP ingest only, so other source types are left untouched.
        Returning zero for an offline path makes stream-key rotation
        idempotent.
        """

        try:
            payload = await self.get_path(path_name)
        except MediaMTXNotFound:
            return 0
        if not payload:
            return 0
        path = _unwrap_path(payload)
        source = path.get("source")
        if not isinstance(source, Mapping):
            return 0
        source_type = str(_first(source, "type", "sourceType") or "").lower()
        source_id = _first(source, "id", "sourceId")
        if source_type.replace("_", "") not in {"rtmpconn", "rtmpconnection"}:
            return 0
        if source_id is None or not str(source_id):
            return 0
        encoded_id = quote(str(source_id), safe="")
        await self._post_json(f"{self._api_prefix}/rtmpconns/kick/{encoded_id}")
        return 1


__all__ = [
    "AsyncJsonActionTransport",
    "AsyncJsonTransport",
    "HttpxJsonTransport",
    "IngestState",
    "IngestStatus",
    "JsonFetcher",
    "MediaMTXClient",
    "MediaMTXError",
    "MediaMTXNotFound",
    "MediaMTXTransportError",
    "StreamMetadata",
    "map_ingest_status",
    "normalize_codec",
    "normalize_stream_metadata",
]
