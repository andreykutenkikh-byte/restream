"""Canonical, non-DNS identity for SSH bootstrap targets."""

from __future__ import annotations

import ipaddress
import re
import unicodedata

MAX_SSH_HOST_LENGTH = 253
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FORBIDDEN_CHARACTERS = frozenset("/\\@?#[]%;&|`$<>{}\"'")


def canonicalize_ssh_address(value: str) -> str:
    """Normalize equivalent host spellings before persistence or retry lookup.

    Public-address and DNS-rebinding checks remain the isolated worker's job. This
    helper deliberately performs no DNS lookup and also accepts CI-only single-label
    names, while making trailing-dot, IDNA, case, and IP spellings unambiguous.
    """

    if not isinstance(value, str) or not value or len(value) > MAX_SSH_HOST_LENGTH:
        raise ValueError("SSH address is invalid")
    if value != value.strip() or any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or character in _FORBIDDEN_CHARACTERS
        for character in value
    ):
        raise ValueError("SSH address is invalid")
    candidate = value.rstrip(".").lower()
    if not candidate:
        raise ValueError("SSH address is invalid")
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("SSH address is invalid") from exc
    if (
        len(candidate) > MAX_SSH_HOST_LENGTH
        or all(character.isdigit() or character == "." for character in candidate)
        or any(not _HOST_LABEL.fullmatch(label) for label in candidate.split("."))
    ):
        raise ValueError("SSH address is invalid")
    return candidate


__all__ = ["canonicalize_ssh_address"]
