# ruff: noqa: UP017 -- this native package targets Ubuntu 22.04 Python 3.10.
"""On-demand, loopback-only HLS reader for the remote relay preview."""

from __future__ import annotations

import http.cookiejar
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPCookieProcessor,
    HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from uuid import uuid4

from relay_agent.errors import RelayAgentError
from relay_agent.security import SensitiveToken

LOCAL_HLS_ORIGIN = "http://127.0.0.1:8888"
_LOCAL_HLS_PREFIX = "/relay-output/"
# MediaMTX uses this fixed query to perform its same-origin cookie capability
# check.  Supplying it up front avoids following a redirect while still letting
# the cookie jar authenticate the subsequent session playlist and segments.
LOCAL_HLS_PATH = f"{_LOCAL_HLS_PREFIX}index.m3u8?cookieCheck=1"
LOCAL_HLS_USERNAME = "relay-preview"
MAX_PLAYLIST_BYTES = 64 * 1024
MAX_SEGMENT_BYTES = 3 * 1024 * 1024
_SESSION_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_SESSION_QUERY = rf"(?:\?session={_SESSION_UUID4})?"
_PLAYLIST_RESOURCE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}\.m3u8{_SESSION_QUERY}\Z")
_SEGMENT_RESOURCE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}\.ts{_SESSION_QUERY}\Z")
_MEDIA_SEQUENCE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:([0-9]{1,19})\Z")
logger = logging.getLogger("relay_agent")


class _NoRedirect(HTTPRedirectHandler):
    # urllib handlers use this method by convention.
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass(frozen=True, slots=True)
class LocalSegment:
    sequence: int
    name: str


def _clean_lines(payload: bytes) -> list[str]:
    if not payload or len(payload) > MAX_PLAYLIST_BYTES:
        raise RelayAgentError("preview_playlist_invalid")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RelayAgentError("preview_playlist_invalid") from exc
    if "\x00" in text or not text.startswith("#EXTM3U"):
        raise RelayAgentError("preview_playlist_invalid")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise RelayAgentError("preview_playlist_invalid")
    return lines


def parse_master_playlist(payload: bytes) -> str | None:
    """Return one bounded relative media-playlist filename, if this is a master."""

    lines = _clean_lines(payload)
    candidates = [line for line in lines if not line.startswith("#")]
    if not any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        return None
    if len(candidates) != 1 or _PLAYLIST_RESOURCE.fullmatch(candidates[0]) is None:
        raise RelayAgentError("preview_playlist_invalid")
    return candidates[0]


def parse_media_playlist(payload: bytes) -> list[LocalSegment]:
    """Return completed MPEG-TS segment names with their HLS media sequence."""

    lines = _clean_lines(payload)
    sequence_values = [
        match.group(1) for line in lines if (match := _MEDIA_SEQUENCE.fullmatch(line))
    ]
    if len(sequence_values) != 1:
        raise RelayAgentError("preview_playlist_invalid")
    first_sequence = int(sequence_values[0])
    if first_sequence > 2**63 - 1:
        raise RelayAgentError("preview_playlist_invalid")
    names = [line for line in lines if not line.startswith("#")]
    if not names or len(names) > 32:
        raise RelayAgentError("preview_playlist_invalid")
    if any(_SEGMENT_RESOURCE.fullmatch(name) is None for name in names):
        raise RelayAgentError("preview_playlist_invalid")
    if first_sequence + len(names) - 1 > 2**63 - 1:
        raise RelayAgentError("preview_playlist_invalid")
    return [LocalSegment(first_sequence + offset, name) for offset, name in enumerate(names)]


def validate_mpegts(payload: bytes) -> None:
    if not payload or len(payload) > MAX_SEGMENT_BYTES or len(payload) % 188:
        raise RelayAgentError("preview_segment_invalid")
    if any(payload[offset] != 0x47 for offset in range(0, len(payload), 188)):
        raise RelayAgentError("preview_segment_invalid")


class LocalHLSReader:
    """Fetch a fixed loopback MediaMTX path without following redirects."""

    def __init__(self, password: SensitiveToken) -> None:
        password_manager = HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            LOCAL_HLS_ORIGIN,
            LOCAL_HLS_USERNAME,
            password.reveal_for_authorization_header(),
        )
        # HTTPBasicAuthHandler keeps the password out of URLs and process arguments.
        self._opener = build_opener(
            _NoRedirect(),
            HTTPCookieProcessor(http.cookiejar.CookieJar()),
            HTTPBasicAuthHandler(password_manager),
        )
        # MediaMTX returns a session-bound media-playlist URL.  Reusing it is
        # essential: bootstrapping through index.m3u8 on every poll creates a
        # fresh session and makes already published browser segment URLs stale.
        self._media_path: str | None = None

    def completed_segments(self) -> list[LocalSegment]:
        if self._media_path is not None:
            try:
                media = self._fetch(self._media_path, MAX_PLAYLIST_BYTES, playlist=True)
            except RelayAgentError as exc:
                if exc.code != "preview_local_unavailable":
                    raise
                self._media_path = None
            else:
                return parse_media_playlist(media)

        root = self._fetch(LOCAL_HLS_PATH, MAX_PLAYLIST_BYTES, playlist=True)
        nested = parse_master_playlist(root)
        media = root
        if nested is not None:
            self._media_path = f"{_LOCAL_HLS_PREFIX}{nested}"
            try:
                media = self._fetch(self._media_path, MAX_PLAYLIST_BYTES, playlist=True)
            except RelayAgentError:
                self._media_path = None
                raise
        return parse_media_playlist(media)

    def read_segment(self, name: str) -> bytes:
        if _SEGMENT_RESOURCE.fullmatch(name) is None:
            raise RelayAgentError("preview_segment_invalid")
        try:
            payload = self._fetch(f"{_LOCAL_HLS_PREFIX}{name}", MAX_SEGMENT_BYTES, playlist=False)
        except RelayAgentError as exc:
            if exc.code == "preview_local_unavailable":
                self._media_path = None
            raise
        validate_mpegts(payload)
        return payload

    def _fetch(self, path: str, limit: int, *, playlist: bool) -> bytes:
        resource_pattern = _PLAYLIST_RESOURCE if playlist else _SEGMENT_RESOURCE
        initial_cookie_check = playlist and path == LOCAL_HLS_PATH
        regular_resource = (
            path.startswith(_LOCAL_HLS_PREFIX)
            and resource_pattern.fullmatch(path[len(_LOCAL_HLS_PREFIX) :]) is not None
        )
        if not initial_cookie_check and not regular_resource:
            raise RelayAgentError("preview_local_request_invalid")
        url = f"{LOCAL_HLS_ORIGIN}{path}"
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 8888
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise RelayAgentError("preview_local_request_invalid")
        accepted = "application/vnd.apple.mpegurl" if playlist else "video/mp2t"
        # ``url`` was reconstructed from the fixed validated loopback origin above.
        request = Request(url, headers={"Accept": accepted}, method="GET")  # noqa: S310
        try:
            with self._opener.open(request, timeout=4.0) as response:
                if response.status != 200:
                    raise RelayAgentError("preview_local_unavailable")
                content_type = response.headers.get_content_type().lower()
                allowed = (
                    {"application/vnd.apple.mpegurl", "application/x-mpegurl"}
                    if playlist
                    else {"video/mp2t"}
                )
                if content_type not in allowed:
                    raise RelayAgentError("preview_local_invalid_response")
                payload = bytes(response.read(limit + 1))
        except RelayAgentError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise RelayAgentError("preview_local_unavailable") from exc
        if not payload or len(payload) > limit:
            raise RelayAgentError("preview_local_invalid_response")
        return payload


class PreviewUploader(Protocol):
    def upload_preview_segment(self, generation: str, sequence: int, payload: bytes) -> None: ...


class PreviewPump:
    """Send only newly completed local segments while the control plane requests them."""

    def __init__(
        self,
        reader: LocalHLSReader,
        uploader: PreviewUploader,
        *,
        interval_seconds: float = 1.0,
        generation_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("preview interval must be positive")
        self._reader = reader
        self._uploader = uploader
        self._interval = interval_seconds
        self._generation_factory = generation_factory
        self._state_lock = threading.Lock()
        self._requested = False
        self._generation: str | None = None
        self._uploaded: dict[int, str] = {}

    def set_requested(self, requested: bool) -> None:
        with self._state_lock:
            requested = requested is True
            if requested and not self._requested:
                self._generation = self._generation_factory()
                self._uploaded.clear()
            elif not requested:
                self._generation = None
                self._uploaded.clear()
            self._requested = requested

    def run(self, stop_event: threading.Event) -> None:
        last_error_code: str | None = None
        while not stop_event.is_set():
            with self._state_lock:
                requested = self._requested
                generation = self._generation
            if requested and generation is not None:
                try:
                    self._pump_once(generation)
                except RelayAgentError as exc:
                    # The heartbeat/control loops remain independent of preview failures.
                    if last_error_code != exc.code:
                        logger.warning("Preview cycle failed safely (%s)", exc.code)
                    last_error_code = exc.code
                except Exception:
                    if last_error_code != "internal_error":
                        logger.warning("Preview cycle failed safely (internal_error)")
                    last_error_code = "internal_error"
                else:
                    last_error_code = None
            else:
                last_error_code = None
            stop_event.wait(self._interval if requested else 0.5)

    def _pump_once(self, generation: str) -> None:
        segments = self._reader.completed_segments()
        with self._state_lock:
            reset = bool(self._uploaded) and (
                segments[-1].sequence < max(self._uploaded)
                or any(
                    segment.sequence in self._uploaded
                    and self._uploaded[segment.sequence] != segment.name
                    for segment in segments
                )
            )
            if reset and self._requested and self._generation == generation:
                generation = self._generation_factory()
                self._generation = generation
                self._uploaded.clear()
        for segment in segments[-4:]:
            with self._state_lock:
                if not self._requested or self._generation != generation:
                    return
                if segment.sequence in self._uploaded:
                    continue
            payload = self._reader.read_segment(segment.name)
            self._uploader.upload_preview_segment(generation, segment.sequence, payload)
            with self._state_lock:
                if self._requested and self._generation == generation:
                    self._uploaded[segment.sequence] = segment.name
                    if len(self._uploaded) > 32:
                        newest = sorted(self._uploaded)[-16:]
                        self._uploaded = {sequence: self._uploaded[sequence] for sequence in newest}


__all__ = [
    "LOCAL_HLS_ORIGIN",
    "LOCAL_HLS_PATH",
    "LocalHLSReader",
    "LocalSegment",
    "PreviewPump",
    "parse_master_playlist",
    "parse_media_playlist",
    "validate_mpegts",
]
