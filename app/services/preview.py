"""Authenticated, secret-safe proxy for the internal MediaMTX HLS muxer."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

_MAX_PLAYLIST_BYTES = 512 * 1024
_MAX_ASSET_NAME_BYTES = 160
_SESSION_COOKIE_RE = re.compile(
    r"^\s*hlsSession=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:;|$)"
)
_MEDIA_PLAYLIST_RE = re.compile(r"^(?:main|video[1-9][0-9]*|audio[1-9][0-9]*)_stream\.m3u8$")
_MEDIA_ASSET_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{12}_(?:main|video[1-9][0-9]*|audio[1-9][0-9]*)_"
    r"(?:init|seg[0-9]+|part[0-9]+)\.(?:mp4|mp|ts)"
    r"|gap\.mp4"
    r")$"
)
_RANGE_RE = re.compile(r"^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (?:[0-9]+-[0-9]+|\*)/(?:[0-9]+|\*)$")
_URI_ATTRIBUTE_RE = re.compile(r'URI=(?:"([^"]*)"|([^,\s]*))')
_SAFE_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]{0,19})$")


class PreviewError(RuntimeError):
    """Base class for errors whose details must not contain the upstream URL."""


class PreviewInvalidRequest(PreviewError):
    """The public asset name, query, or Range header is not allowed."""


class PreviewUnavailable(PreviewError):
    """The incoming stream is offline or its HLS muxer is not ready."""


class PreviewUpstreamError(PreviewError):
    """The internal HLS service failed without exposing internal details."""


@dataclass(frozen=True, slots=True)
class PreviewResponse:
    """Filtered response consumed by the FastAPI route."""

    status_code: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]


async def _bytes_body(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("MediaMTX HLS URL must be an HTTP(S) origin")
    return value.strip().rstrip("/")


def _validate_asset(asset: str) -> bool:
    try:
        encoded_length = len(asset.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise PreviewInvalidRequest("Unsupported preview asset") from exc
    if (
        not asset
        or encoded_length > _MAX_ASSET_NAME_BYTES
        or "%" in asset
        or "/" in asset
        or "\\" in asset
        or asset in {".", ".."}
    ):
        raise PreviewInvalidRequest("Unsupported preview asset")
    if asset == "index.m3u8" or _MEDIA_PLAYLIST_RE.fullmatch(asset):
        return True
    if _MEDIA_ASSET_RE.fullmatch(asset):
        return False
    raise PreviewInvalidRequest("Unsupported preview asset")


def _validated_query(
    query_items: Sequence[tuple[str, str]], *, is_playlist: bool
) -> list[tuple[str, str]]:
    if not query_items:
        return []
    if not is_playlist:
        raise PreviewInvalidRequest("Preview asset query is not allowed")

    allowed: dict[str, str] = {}
    for key, value in query_items:
        if key in allowed:
            raise PreviewInvalidRequest("Duplicate preview query parameter")
        valid_integer = key in {"_HLS_msn", "_HLS_part"} and _SAFE_INTEGER_RE.fullmatch(value)
        valid_skip = key == "_HLS_skip" and value in {"YES", "v2"}
        if valid_integer or valid_skip:
            allowed[key] = value
        else:
            raise PreviewInvalidRequest("Unsupported preview query parameter")
    return list(allowed.items())


def _validated_range(value: str | None, *, is_playlist: bool) -> str | None:
    if value is None:
        return None
    if is_playlist or len(value) > 64 or _RANGE_RE.fullmatch(value) is None:
        raise PreviewInvalidRequest("Unsupported preview byte range")
    return value


def _public_asset_uri(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path:
        raise PreviewUpstreamError("Invalid internal HLS playlist")
    # MediaMTX can append its own session query when cookies are unavailable.
    # The backend uses a private cookie, so every upstream query is deliberately
    # removed before the URI reaches the browser.
    try:
        _validate_asset(parsed.path)
    except PreviewInvalidRequest:
        raise PreviewUpstreamError("Invalid internal HLS playlist") from None
    return f"/api/ingest/preview/{quote(parsed.path, safe='')}"


def _rewrite_playlist(payload: bytes) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreviewUpstreamError("Invalid internal HLS playlist") from exc
    if not text.lstrip().startswith("#EXTM3U"):
        raise PreviewUpstreamError("Invalid internal HLS playlist")

    rewritten: list[str] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if content and not content.startswith("#"):
            content = _public_asset_uri(content.strip())
        elif "URI=" in content:

            def replace_uri(match: re.Match[str]) -> str:
                value = match.group(1) if match.group(1) is not None else match.group(2)
                return f'URI="{_public_asset_uri(value)}"'

            content = _URI_ATTRIBUTE_RE.sub(replace_uri, content)
        rewritten.append(content + ending)
    return "".join(rewritten).encode("utf-8")


def _content_type(asset: str) -> str:
    if asset.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if asset.endswith(".ts"):
        return "video/mp2t"
    return "video/mp4"


class PreviewService:
    """Maintain one private MediaMTX HLS session and proxy its safe assets."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 12.0,
    ) -> None:
        if not username or not password:
            raise ValueError("Preview credentials are required")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Preview timeouts must be positive")
        self._base_url = _validate_base_url(base_url)
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=connect_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )
        self._session_lock = asyncio.Lock()
        self._session_path: str | None = None
        self._session_cookie: str | None = None

    @staticmethod
    def _encoded_path(path_name: str) -> str:
        if not path_name or any(not part for part in path_name.split("/")):
            raise PreviewUpstreamError("Invalid internal preview path")
        return "/".join(quote(part, safe="") for part in path_name.split("/"))

    def _url(self, path_name: str, asset: str) -> str:
        return f"{self._base_url}/{self._encoded_path(path_name)}/{quote(asset, safe='')}"

    async def _send(
        self,
        path_name: str,
        asset: str,
        *,
        query: Sequence[tuple[str, str]] = (),
        cookie: str,
        range_header: str | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.apple.mpegurl" if asset.endswith(".m3u8") else "*/*",
            "Accept-Encoding": "identity",
            "Cookie": cookie,
            "User-Agent": "AdoJapan-Restream-Preview/1",
        }
        if range_header is not None:
            headers["Range"] = range_header
        request = self._client.build_request(
            "GET", self._url(path_name, asset), headers=headers, params=list(query)
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.TimeoutException:
            raise PreviewUpstreamError("Internal preview timed out") from None
        except httpx.HTTPError:
            raise PreviewUpstreamError("Internal preview is unavailable") from None

    @staticmethod
    async def _read_playlist(response: httpx.Response) -> bytes:
        payload = bytearray()
        try:
            async for chunk in response.aiter_raw():
                payload.extend(chunk)
                if len(payload) > _MAX_PLAYLIST_BYTES:
                    raise PreviewUpstreamError("Internal HLS playlist is too large")
        except httpx.HTTPError:
            raise PreviewUpstreamError("Internal preview is unavailable") from None
        return bytes(payload)

    @staticmethod
    def _session_from(response: httpx.Response) -> str | None:
        for value in response.headers.get_list("set-cookie"):
            match = _SESSION_COOKIE_RE.match(value)
            if match is not None:
                return match.group(1).lower()
        return None

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        status_code = response.status_code
        await response.aclose()
        if status_code == 404:
            raise PreviewUnavailable("Incoming preview is not available")
        raise PreviewUpstreamError("Internal preview is unavailable")

    async def _start_session_locked(self, path_name: str) -> bytes:
        response = await self._send(
            path_name,
            "index.m3u8",
            query=(("cookieCheck", "1"),),
            cookie="cookieCheck=1",
        )
        if response.status_code != 200:
            await self._raise_for_status(response)
        session_cookie = self._session_from(response)
        try:
            payload = await self._read_playlist(response)
        finally:
            await response.aclose()
            # Never let the HTTP client's cookie jar become the source of
            # truth; the allowlisted hlsSession value stays in this service.
            self._client.cookies.clear()
        if session_cookie is None:
            raise PreviewUpstreamError("Internal preview session is unavailable")
        self._session_path = path_name
        self._session_cookie = session_cookie
        return payload

    async def _new_session(self, path_name: str) -> bytes:
        async with self._session_lock:
            return await self._start_session_locked(path_name)

    async def _session_for(self, path_name: str) -> str:
        async with self._session_lock:
            if self._session_path != path_name or self._session_cookie is None:
                await self._start_session_locked(path_name)
            if self._session_cookie is None:  # pragma: no cover - locked invariant
                raise PreviewUpstreamError("Internal preview session is unavailable")
            return self._session_cookie

    async def _renew_session(self, path_name: str, expired_cookie: str) -> str:
        async with self._session_lock:
            if self._session_path != path_name or self._session_cookie == expired_cookie:
                await self._start_session_locked(path_name)
            if self._session_cookie is None:  # pragma: no cover - locked invariant
                raise PreviewUpstreamError("Internal preview session is unavailable")
            return self._session_cookie

    @staticmethod
    def _streaming_body(response: httpx.Response) -> AsyncIterator[bytes]:
        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            except httpx.HTTPError:
                # Response headers have already been sent. End the stream
                # without exposing the internal URL or transport exception.
                return
            finally:
                await response.aclose()

        return body()

    @staticmethod
    def _filtered_media_headers(response: httpx.Response, asset: str) -> dict[str, str]:
        headers = {
            "Content-Type": _content_type(asset),
            "Cache-Control": "private, no-store, max-age=0",
        }
        content_length = response.headers.get("content-length", "")
        if content_length.isdecimal() and int(content_length) >= 0:
            headers["Content-Length"] = content_length
        if response.headers.get("accept-ranges", "").lower() == "bytes":
            headers["Accept-Ranges"] = "bytes"
        content_range = response.headers.get("content-range", "")
        if _CONTENT_RANGE_RE.fullmatch(content_range):
            headers["Content-Range"] = content_range
        return headers

    async def open(
        self,
        path_name: str,
        asset: str,
        *,
        query_items: Sequence[tuple[str, str]] = (),
        range_header: str | None = None,
    ) -> PreviewResponse:
        """Open a filtered playlist or streamed media asset."""

        is_playlist = _validate_asset(asset)
        query = _validated_query(query_items, is_playlist=is_playlist)
        safe_range = _validated_range(range_header, is_playlist=is_playlist)

        if asset == "index.m3u8":
            payload = _rewrite_playlist(await self._new_session(path_name))
            return PreviewResponse(
                status_code=200,
                headers={
                    "Content-Type": _content_type(asset),
                    "Content-Length": str(len(payload)),
                    "Cache-Control": "private, no-store, max-age=0",
                },
                body=_bytes_body(payload),
            )

        session_cookie = await self._session_for(path_name)
        response = await self._send(
            path_name,
            asset,
            query=query,
            cookie=f"hlsSession={session_cookie}",
            range_header=safe_range,
        )
        if response.status_code in {401, 403}:
            await response.aclose()
            session_cookie = await self._renew_session(path_name, session_cookie)
            response = await self._send(
                path_name,
                asset,
                query=query,
                cookie=f"hlsSession={session_cookie}",
                range_header=safe_range,
            )
        if response.status_code not in {200, 206}:
            await self._raise_for_status(response)

        if is_playlist:
            try:
                payload = _rewrite_playlist(await self._read_playlist(response))
            finally:
                await response.aclose()
            return PreviewResponse(
                status_code=response.status_code,
                headers={
                    "Content-Type": _content_type(asset),
                    "Content-Length": str(len(payload)),
                    "Cache-Control": "private, no-store, max-age=0",
                },
                body=_bytes_body(payload),
            )

        return PreviewResponse(
            status_code=response.status_code,
            headers=self._filtered_media_headers(response, asset),
            body=self._streaming_body(response),
        )

    async def reset(self) -> None:
        """Forget the current private HLS session after a stream-key change."""

        async with self._session_lock:
            self._session_path = None
            self._session_cookie = None
            self._client.cookies.clear()

    async def close(self) -> None:
        await self.reset()
        await self._client.aclose()


__all__ = [
    "PreviewError",
    "PreviewInvalidRequest",
    "PreviewResponse",
    "PreviewService",
    "PreviewUnavailable",
    "PreviewUpstreamError",
]
