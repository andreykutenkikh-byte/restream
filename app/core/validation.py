"""Strict destination and stream compatibility validation.

User-supplied destinations are resolved during validation and every returned IP
address must be globally routable.  Call :func:`validate_destination_url` again
immediately before spawning FFmpeg to reduce the DNS-rebinding window.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import SplitResult, unquote, urlsplit

ALLOWED_DESTINATION_SCHEMES = frozenset({"rtmp", "rtmps"})
DEFAULT_DESTINATION_PORTS = {"rtmp": 1935, "rtmps": 443}
MAX_DESTINATION_URL_LENGTH = 2_048

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type AddressLike = str | bytes | IPAddress


class AddressResolver(Protocol):
    """Injectable DNS resolver used by destination validation."""

    def __call__(self, hostname: str) -> Iterable[AddressLike]: ...


class URLValidationError(ValueError):
    """Raised when a user-supplied destination is unsafe or malformed."""


class CodecCompatibilityError(ValueError):
    """Raised when stream-copy to RTMP/FLV is not possible."""


@dataclass(frozen=True, slots=True)
class ParsedDestinationURL:
    """Syntactically validated destination, before its host is resolved."""

    value: str
    scheme: str
    hostname: str
    port: int
    parsed: SplitResult

    @property
    def url(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ValidatedDestinationURL:
    """Destination URL plus the public IP set observed during validation."""

    value: str
    scheme: str
    hostname: str
    port: int
    resolved_addresses: tuple[IPAddress, ...]

    @property
    def url(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CodecCompatibility:
    """Result of checking whether a stream can be copied to RTMP/FLV."""

    compatible: bool
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]
    container: str | None
    issues: tuple[str, ...] = ()

    @property
    def reason(self) -> str | None:
        """First incompatibility reason, suitable for structured diagnostics."""

        return self.issues[0] if self.issues else None

    @property
    def message(self) -> str:
        """Concise user-facing compatibility message."""

        if self.compatible:
            return "Поток совместим с RTMP/FLV без перекодирования."
        return (
            "Этот поток нельзя передать без перекодирования. "
            "Для первой версии используйте H.264 для видео и AAC для звука."
        )

    def __bool__(self) -> bool:
        return self.compatible


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FORBIDDEN_SHELL_CHARACTERS = frozenset(";&|`$<>{}\\\"'")
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "host.docker.internal",
        "gateway.docker.internal",
        "docker.for.win.localhost",
        "docker.for.mac.localhost",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".lan",
    ".internal",
    ".home.arpa",
    ".docker.internal",
)
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
    }
)
_WELL_KNOWN_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _contains_unsafe_character(value: str) -> bool:
    return any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or character in _FORBIDDEN_SHELL_CHARACTERS
        for character in value
    )


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if not candidate:
        raise URLValidationError("destination hostname is missing")
    if "%" in candidate:
        raise URLValidationError("percent escapes and IPv6 zones are not allowed in hostnames")

    # IP literals are validated by the SSRF layer; DNS names continue below.
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass

    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLValidationError("destination hostname is invalid") from exc
    if len(ascii_hostname) > 253:
        raise URLValidationError("destination hostname is too long")
    if all(character.isdigit() or character == "." for character in ascii_hostname):
        raise URLValidationError("destination contains an invalid IP address")
    labels = ascii_hostname.split(".")
    if len(labels) < 2:
        raise URLValidationError("external destinations must use a fully qualified hostname")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise URLValidationError("destination hostname is invalid")
    return ascii_hostname


def _host_is_blocked(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    return normalized in _BLOCKED_HOSTS or normalized.endswith(_BLOCKED_HOST_SUFFIXES)


def parse_destination_url(
    value: str, *, max_length: int = MAX_DESTINATION_URL_LENGTH
) -> ParsedDestinationURL:
    """Perform strict RTMP/RTMPS syntax validation without resolving DNS.

    Most application code should call :func:`validate_destination_url`, which
    additionally enforces SSRF policy.  This split exists for request parsing and
    deterministic error reporting, not as a way to skip host validation.
    """

    if not isinstance(value, str):
        raise TypeError("destination URL must be a string")
    if isinstance(max_length, bool) or not isinstance(max_length, int):
        raise TypeError("max_length must be an integer")
    if max_length < 1:
        raise ValueError("max_length must be positive")
    if not value:
        raise URLValidationError("destination URL must not be empty")
    if len(value) > max_length:
        raise URLValidationError(f"destination URL exceeds {max_length} characters")
    if _contains_unsafe_character(value) or _contains_unsafe_character(unquote(value)):
        raise URLValidationError("destination URL contains whitespace or unsafe characters")

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise URLValidationError("destination URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_DESTINATION_SCHEMES:
        raise URLValidationError("only rtmp:// and rtmps:// destinations are allowed")
    if not parsed.netloc:
        raise URLValidationError("destination hostname is missing")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("credentials must not be embedded in destination URLs")
    if parsed.fragment:
        raise URLValidationError("URL fragments are not allowed in destinations")

    try:
        raw_hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise URLValidationError("destination hostname or port is invalid") from exc
    if raw_hostname is None:
        raise URLValidationError("destination hostname is missing")
    hostname = _normalize_hostname(raw_hostname)
    if _host_is_blocked(hostname):
        raise URLValidationError("local, metadata, and Docker hostnames are not allowed")
    port = explicit_port if explicit_port is not None else DEFAULT_DESTINATION_PORTS[scheme]
    if not 1 <= port <= 65_535:
        raise URLValidationError("destination port must be between 1 and 65535")
    return ParsedDestinationURL(value, scheme, hostname, port, parsed)


def resolve_host_addresses(hostname: str) -> tuple[IPAddress, ...]:
    """Resolve all IPv4 and IPv6 addresses currently published for ``hostname``."""

    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise URLValidationError("destination hostname could not be resolved") from exc
    addresses: list[IPAddress] = []
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _address_from_record(record: Any) -> IPAddress:
    if isinstance(record, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return record
    # Accept getaddrinfo-style records as a convenience for resolver adapters.
    if isinstance(record, tuple) and len(record) >= 5:
        socket_address = record[4]
        if isinstance(socket_address, tuple) and socket_address:
            record = socket_address[0]
    if isinstance(record, bytes):
        try:
            record = record.decode("ascii")
        except UnicodeDecodeError as exc:
            raise URLValidationError("DNS resolver returned an invalid address") from exc
    try:
        return ipaddress.ip_address(record)
    except (TypeError, ValueError) as exc:
        raise URLValidationError("DNS resolver returned an invalid address") from exc


def is_public_address(address: AddressLike) -> bool:
    """Return ``True`` only for globally routable unicast IP addresses."""

    try:
        parsed = _address_from_record(address)
    except URLValidationError:
        return False
    if parsed in _METADATA_ADDRESSES:
        return False
    if not parsed.is_global or any(
        (
            parsed.is_private,
            parsed.is_loopback,
            parsed.is_link_local,
            parsed.is_multicast,
            parsed.is_unspecified,
            parsed.is_reserved,
        )
    ):
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None and not is_public_address(parsed.ipv4_mapped):
            return False
        if parsed.sixtofour is not None and not is_public_address(parsed.sixtofour):
            return False
        if parsed.teredo is not None and not is_public_address(parsed.teredo[1]):
            return False
        if parsed in _WELL_KNOWN_NAT64:
            embedded = ipaddress.IPv4Address(int(parsed) & 0xFFFFFFFF)
            if not is_public_address(embedded):
                return False
    return True


def validate_public_host(
    hostname: str, *, resolver: AddressResolver | None = None
) -> tuple[IPAddress, ...]:
    """Resolve ``hostname`` and reject it if any answer is not public.

    Rejecting the complete answer set is important: accepting one public answer
    while silently ignoring a private answer still permits DNS rebinding and
    resolver-order attacks.
    """

    normalized = _normalize_hostname(hostname)
    if _host_is_blocked(normalized):
        raise URLValidationError("local, metadata, and Docker hostnames are not allowed")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    addresses: tuple[IPAddress, ...]
    if literal is not None:
        addresses = (literal,)
    else:
        chosen_resolver = resolver or resolve_host_addresses
        try:
            records = chosen_resolver(normalized)
        except URLValidationError:
            raise
        except (socket.gaierror, OSError) as exc:
            raise URLValidationError("destination hostname could not be resolved") from exc
        if isinstance(records, (str, bytes)):
            records = (records,)
        try:
            addresses = tuple(dict.fromkeys(_address_from_record(item) for item in records))
        except TypeError as exc:
            raise URLValidationError("DNS resolver returned no iterable address set") from exc
    if not addresses:
        raise URLValidationError("destination hostname resolved to no addresses")
    blocked = tuple(address for address in addresses if not is_public_address(address))
    if blocked:
        rendered = ", ".join(str(address) for address in blocked)
        raise URLValidationError(f"destination resolves to a non-public address: {rendered}")
    return addresses


def validate_destination_url(
    value: str,
    *,
    resolver: AddressResolver | None = None,
    max_length: int = MAX_DESTINATION_URL_LENGTH,
) -> ValidatedDestinationURL:
    """Validate destination syntax, resolve DNS, and enforce SSRF protections."""

    parsed = parse_destination_url(value, max_length=max_length)
    addresses = validate_public_host(parsed.hostname, resolver=resolver)
    return ValidatedDestinationURL(
        value=parsed.value,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        resolved_addresses=addresses,
    )


def destination_validator(
    *,
    environment: str,
    test_allowlist: Sequence[str] = (),
    resolver: AddressResolver | None = None,
) -> Callable[[str], ValidatedDestinationURL]:
    """Build the runtime URL validator with a fail-closed CI-only exception.

    The exception is an exact match against an explicitly configured value and
    can only exist in the ``test`` environment. Every other URL continues
    through the normal public-address SSRF policy.
    """

    allowed = tuple(dict.fromkeys(test_allowlist))
    if allowed and environment != "test":
        raise URLValidationError("test destination allowlist requires ENVIRONMENT=test")

    def validate(value: str) -> ValidatedDestinationURL:
        if value not in allowed:
            return validate_destination_url(value, resolver=resolver)

        parsed = _parse_exact_test_destination(value)
        chosen_resolver = resolver or resolve_host_addresses
        try:
            addresses = tuple(
                dict.fromkeys(
                    _address_from_record(item) for item in chosen_resolver(parsed.hostname)
                )
            )
        except (socket.gaierror, OSError) as exc:
            raise URLValidationError("test destination hostname could not be resolved") from exc
        if not addresses:
            raise URLValidationError("test destination hostname resolved to no addresses")
        return ValidatedDestinationURL(
            value=parsed.value,
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            resolved_addresses=addresses,
        )

    return validate


def _parse_exact_test_destination(value: str) -> ParsedDestinationURL:
    """Parse an already allowlisted test URL without permitting unsafe syntax."""

    if not value or len(value) > MAX_DESTINATION_URL_LENGTH:
        raise URLValidationError("test destination URL length is invalid")
    if _contains_unsafe_character(value) or _contains_unsafe_character(unquote(value)):
        raise URLValidationError("test destination URL contains unsafe characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("test destination URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_DESTINATION_SCHEMES:
        raise URLValidationError("only rtmp:// and rtmps:// test destinations are allowed")
    if not parsed.netloc or parsed.hostname is None:
        raise URLValidationError("test destination hostname is missing")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("credentials must not be embedded in test destination URLs")
    if parsed.query or parsed.fragment:
        raise URLValidationError("queries and fragments are not allowed in test destinations")
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or "%" in hostname:
        raise URLValidationError("test destination hostname is invalid")
    selected_port = port if port is not None else DEFAULT_DESTINATION_PORTS[scheme]
    if not 1 <= selected_port <= 65_535:
        raise URLValidationError("test destination port must be between 1 and 65535")
    return ParsedDestinationURL(value, scheme, hostname, selected_port, parsed)


def validate_rtmp_url(
    value: str,
    *,
    resolver: AddressResolver | None = None,
    max_length: int = MAX_DESTINATION_URL_LENGTH,
) -> str:
    """Validate a destination and return its original string for schema hooks."""

    return validate_destination_url(value, resolver=resolver, max_length=max_length).value


# Revalidation should be explicit at the FFmpeg process boundary.
revalidate_destination_url = validate_destination_url
validate_url = validate_rtmp_url


_VIDEO_ALIASES = {"h264": "h264", "h.264": "h264", "avc": "h264", "avc1": "h264"}
_AUDIO_ALIASES = {"aac": "aac", "mp4a": "aac"}
_FLV_CONTAINERS = frozenset({"flv", "live_flv"})


def _codec_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        value = value.get("codec_name", value.get("codec"))
    if isinstance(value, str):
        stripped = value.strip().lower()
        return () if stripped in {"", "none", "null"} else (stripped,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_codec_values(item))
        return tuple(result)
    return (str(value).strip().lower(),)


def check_codec_compatibility(
    video_codec: Any,
    audio_codec: Any = None,
    *,
    container: str | None = None,
) -> CodecCompatibility:
    """Check normalized codec metadata against Stage 1 stream-copy support."""

    raw_video = _codec_values(video_codec)
    raw_audio = _codec_values(audio_codec)
    videos = tuple(_VIDEO_ALIASES.get(codec, codec) for codec in raw_video)
    audios = tuple(_AUDIO_ALIASES.get(codec, codec) for codec in raw_audio)
    normalized_container = container.strip().lower() if isinstance(container, str) else None
    issues: list[str] = []
    if not videos:
        issues.append("video stream is missing")
    elif any(codec != "h264" for codec in videos):
        issues.append(f"unsupported video codec: {', '.join(raw_video)}")
    if any(codec != "aac" for codec in audios):
        issues.append(f"unsupported audio codec: {', '.join(raw_audio)}")
    if normalized_container:
        container_names = {item.strip() for item in normalized_container.split(",") if item.strip()}
        if not container_names.intersection(_FLV_CONTAINERS):
            issues.append(f"unsupported container: {normalized_container}")
    return CodecCompatibility(
        compatible=not issues,
        video_codecs=videos,
        audio_codecs=audios,
        container=normalized_container,
        issues=tuple(issues),
    )


def check_stream_compatibility(metadata: Mapping[str, Any]) -> CodecCompatibility:
    """Check ffprobe or simplified MediaMTX codec metadata.

    ffprobe input is expected to contain ``streams`` and optionally
    ``format.format_name``.  For MediaMTX-style metadata, ``video_codec``,
    ``audio_codec``, and optional ``container`` keys are also accepted.
    """

    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    streams = metadata.get("streams")
    video: Any = metadata.get("video_codec", metadata.get("video"))
    audio: Any = metadata.get("audio_codec", metadata.get("audio"))
    if isinstance(streams, Sequence) and not isinstance(streams, (str, bytes, bytearray)):
        video = [
            stream
            for stream in streams
            if isinstance(stream, Mapping) and str(stream.get("codec_type", "")).lower() == "video"
        ]
        audio = [
            stream
            for stream in streams
            if isinstance(stream, Mapping) and str(stream.get("codec_type", "")).lower() == "audio"
        ]

    format_metadata = metadata.get("format")
    if isinstance(format_metadata, Mapping):
        container = format_metadata.get("format_name")
    else:
        container = metadata.get("format_name", metadata.get("container"))
    return check_codec_compatibility(video, audio, container=container)


def ensure_stream_compatible(metadata: Mapping[str, Any]) -> CodecCompatibility:
    """Return compatibility information or raise ``CodecCompatibilityError``."""

    result = check_stream_compatibility(metadata)
    if not result.compatible:
        detail = "; ".join(result.issues)
        raise CodecCompatibilityError(f"{result.message} ({detail})")
    return result


__all__ = [
    "ALLOWED_DESTINATION_SCHEMES",
    "AddressResolver",
    "CodecCompatibility",
    "CodecCompatibilityError",
    "MAX_DESTINATION_URL_LENGTH",
    "ParsedDestinationURL",
    "URLValidationError",
    "ValidatedDestinationURL",
    "check_codec_compatibility",
    "check_stream_compatibility",
    "ensure_stream_compatible",
    "is_public_address",
    "parse_destination_url",
    "resolve_host_addresses",
    "revalidate_destination_url",
    "validate_destination_url",
    "validate_public_host",
    "validate_rtmp_url",
    "validate_url",
]
