"""Centralized redaction helpers for logs and safe diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

REDACTION_MARKER = "[REDACTED]"

_NORMALIZED_SENSITIVE_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "adminkey",
        "adminpassword",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "csrf",
        "csrftoken",
        "destinationkey",
        "encryptionkey",
        "masterkey",
        "masterencryptionkey",
        "password",
        "passwd",
        "privatekey",
        "pwd",
        "secret",
        "session",
        "sessioncookie",
        "sessionid",
        "signature",
        "sshpassword",
        "sudopassword",
        "enrollmenttoken",
        "nodetoken",
        "bootstrapsecret",
        "streamkey",
        "key",
        "token",
    }
)
_SENSITIVE_SUFFIXES = (
    "password",
    "secret",
    "streamkey",
    "token",
    "apikey",
    "sessioncookie",
    "privatekey",
)
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_BEARER_TOKEN = re.compile(r"(?i)(\b(?:authorization\s*[:=]\s*)?bearer\s+)[^\s,;]+")
_SENSITIVE_TEXT_KEY_PATTERN = (
    r"password|passwd|pwd|secret|token|access[_-]?token|api[_-]?key|"
    r"stream[_-]?key|session(?:[_-]?id)?|csrf(?:[_-]?token)?|signature|"
    r"ssh[_-]?password|sudo[_-]?password|enrollment[_-]?token|"
    r"node[_-]?token|bootstrap[_-]?secret"
)
_UNQUOTED_ASSIGNMENT = re.compile(rf"(?i)(\b(?:{_SENSITIVE_TEXT_KEY_PATTERN})\b\s*=\s*)[^\s,;&]+")
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?i)((?:[\"']?)(?:{_SENSITIVE_TEXT_KEY_PATTERN})(?:[\"']?)\s*:\s*)"
    r"([\"'])(.*?)(\2)"
)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: Any) -> bool:
    """Return whether a field or query name conventionally contains a secret."""

    normalized = _normalized_key(key)
    return normalized in _NORMALIZED_SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def mask_secret(
    value: Any,
    *,
    visible_tail: int = 0,
    marker: str = REDACTION_MARKER,
) -> str:
    """Return a fixed marker, optionally retaining a short non-sensitive tail."""

    if isinstance(visible_tail, bool) or not isinstance(visible_tail, int):
        raise TypeError("visible_tail must be an integer")
    if visible_tail < 0:
        raise ValueError("visible_tail must not be negative")
    text = "" if value is None else str(value)
    if visible_tail and len(text) > visible_tail:
        return f"{marker}…{text[-visible_tail:]}"
    return marker


def _redact_query(query: str, marker: str) -> str:
    if not query:
        return query
    components: list[str] = []
    for component in query.split("&"):
        if "=" in component:
            name, value = component.split("=", 1)
            if is_sensitive_key(unquote_plus(name)):
                value = marker
            components.append(f"{name}={value}")
        elif is_sensitive_key(unquote_plus(component)):
            components.append(marker)
        else:
            components.append(component)
    return "&".join(components)


def redact_url(value: str, *, marker: str = REDACTION_MARKER) -> str:
    """Mask URL credentials and sensitive query/fragment parameters.

    The host, port, and non-sensitive path remain visible for useful diagnostics.
    Use :func:`redact_destination_url` when the final path segment is a stream key.
    """

    if not isinstance(value, str):
        raise TypeError("value must be a string")
    try:
        parsed = urlsplit(value)
    except ValueError:
        # Best-effort fallback for malformed input that still contains userinfo.
        return re.sub(r"(?<=://)[^\s/@]+(?::[^\s/@]*)?@", f"{marker}@", value)

    netloc = parsed.netloc
    if "@" in netloc:
        netloc = f"{marker}@{netloc.rsplit('@', 1)[1]}"
    query = _redact_query(parsed.query, marker)
    if parsed.scheme.lower() in {"rtmp", "rtmps"} and parsed.fragment:
        # MediaMTX uses a bare URL fragment as the separator before the
        # destination stream key.  It is never a diagnostic-safe anchor.
        fragment = marker
    else:
        fragment = (
            _redact_query(parsed.fragment, marker) if "=" in parsed.fragment else parsed.fragment
        )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def redact_destination_url(
    value: str,
    *,
    stream_key: str | None = None,
    hide_last_path_segment: bool = True,
    marker: str = REDACTION_MARKER,
) -> str:
    """Redact a complete RTMP output URL used in FFmpeg diagnostics.

    RTMP output URLs commonly append a stream key to the application path.  By
    default the final non-empty path segment is therefore hidden.  Supplying the
    exact ``stream_key`` additionally removes it wherever it appears.
    """

    redacted = redact_url(value, marker=marker)
    if stream_key:
        redacted = redacted.replace(stream_key, marker)
    if not hide_last_path_segment:
        return redacted
    try:
        parsed = urlsplit(redacted)
    except ValueError:
        return redacted
    segments = parsed.path.split("/")
    non_empty = [index for index, segment in enumerate(segments) if segment]
    if non_empty:
        segments[non_empty[-1]] = marker
    path = "/".join(segments)
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _redact_embedded_url(match: re.Match[str], marker: str) -> str:
    candidate = match.group(0)
    # Sentence punctuation is not part of the URL.  Retaining it avoids changing
    # the shape of human-readable diagnostic messages.
    suffix = ""
    while candidate and candidate[-1] in ".,)]":
        suffix = candidate[-1] + suffix
        candidate = candidate[:-1]
    return redact_url(candidate, marker=marker) + suffix


def redact_text(
    value: Any,
    *,
    secrets: Sequence[str] = (),
    marker: str = REDACTION_MARKER,
) -> str:
    """Redact known secrets and common credential forms from arbitrary text."""

    text = str(value)
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, marker)
    text = _URL_IN_TEXT.sub(lambda match: _redact_embedded_url(match, marker), text)
    text = _BEARER_TOKEN.sub(lambda match: f"{match.group(1)}{marker}", text)
    text = _QUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{marker}{match.group(4)}",
        text,
    )
    text = _UNQUOTED_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{marker}", text)
    return text


# A discoverable alias for logging call sites.
redact_secrets = redact_text


def redact_mapping[T](
    value: T,
    *,
    secrets: Sequence[str] = (),
    marker: str = REDACTION_MARKER,
) -> T:
    """Recursively redact structured log data while preserving its shape."""

    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                redacted[key] = marker
            else:
                redacted[key] = redact_mapping(item, secrets=secrets, marker=marker)
        return redacted  # type: ignore[return-value]
    if isinstance(value, list):
        return [redact_mapping(item, secrets=secrets, marker=marker) for item in value]  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item, secrets=secrets, marker=marker) for item in value)  # type: ignore[return-value]
    if isinstance(value, set):
        return {redact_mapping(item, secrets=secrets, marker=marker) for item in value}  # type: ignore[return-value]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets, marker=marker)  # type: ignore[return-value]
    return value


def redact_command(
    arguments: Sequence[str],
    *,
    secrets: Sequence[str] = (),
    marker: str = REDACTION_MARKER,
) -> list[str]:
    """Return an FFmpeg argument vector that is safe to include in diagnostics."""

    result: list[str] = []
    for argument in arguments:
        cleaned = redact_text(argument, secrets=secrets, marker=marker)
        if cleaned.lower().startswith(("rtmp://", "rtmps://")):
            cleaned = redact_destination_url(cleaned, marker=marker)
        result.append(cleaned)
    return result


__all__ = [
    "REDACTION_MARKER",
    "is_sensitive_key",
    "mask_secret",
    "redact_command",
    "redact_destination_url",
    "redact_mapping",
    "redact_secrets",
    "redact_text",
    "redact_url",
]
