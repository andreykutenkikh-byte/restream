"""Fail-closed SSH target validation with DNS-rebinding protection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Protocol

from bootstrap_worker.errors import BootstrapError, safe_failure
from bootstrap_worker.models import TargetIdentity

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_HOST_LENGTH = 253
DNS_TIMEOUT_SECONDS = 10.0
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ALLOWLIST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?$")
_FORBIDDEN_HOSTS = frozenset(
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
_FORBIDDEN_SUFFIXES = (
    ".localhost",
    ".local",
    ".lan",
    ".internal",
    ".home.arpa",
    ".docker.internal",
)
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
    }
)
_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)
_WELL_KNOWN_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class TargetResolver(Protocol):
    async def resolve(self, hostname: str) -> Sequence[str]: ...


class SystemTargetResolver:
    """Resolve A/AAAA records without blocking the ASGI event loop."""

    async def resolve(self, hostname: str) -> Sequence[str]:
        def lookup() -> tuple[str, ...]:
            records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            return tuple(str(record[4][0]) for record in records)

        try:
            async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
                return await asyncio.to_thread(lookup)
        except (socket.gaierror, OSError, TimeoutError) as exc:
            raise safe_failure("invalid_target") from exc


def _contains_forbidden_character(value: str) -> bool:
    return any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        or character in "/\\@?#[]%;&|`$<>{}\"'"
        for character in value
    )


def _normalize_address(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_HOST_LENGTH:
        raise safe_failure("invalid_target")
    if value != value.strip() or _contains_forbidden_character(value):
        raise safe_failure("invalid_target")
    candidate = value.rstrip(".").lower()
    if not candidate:
        raise safe_failure("invalid_target")

    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass

    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise safe_failure("invalid_target") from exc
    if len(candidate) > MAX_HOST_LENGTH or "." not in candidate:
        raise safe_failure("invalid_target")
    if all(character.isdigit() or character == "." for character in candidate):
        raise safe_failure("invalid_target")
    if any(not _HOST_LABEL.fullmatch(label) for label in candidate.split(".")):
        raise safe_failure("invalid_target")
    if candidate in _FORBIDDEN_HOSTS or candidate.endswith(_FORBIDDEN_SUFFIXES):
        raise safe_failure("invalid_target")
    return candidate


def _normalize_allowlist_address(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_HOST_LENGTH:
        raise ValueError("test SSH allowlist contains an invalid host")
    if value != value.strip() or _contains_forbidden_character(value):
        raise ValueError("test SSH allowlist contains an invalid host")
    candidate = value.rstrip(".").lower()
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("test SSH allowlist contains an invalid host") from exc
    if not _ALLOWLIST_LABEL.fullmatch(candidate):
        raise ValueError("test SSH allowlist contains an invalid host")
    return candidate


def _parse_allowlist_entry(entry: str) -> tuple[str, int]:
    if not isinstance(entry, str) or not entry or entry != entry.strip():
        raise ValueError("test SSH allowlist entry must be an exact host:port")
    host: str
    port_text: str
    if entry.startswith("["):
        closing = entry.find("]")
        if closing < 0 or closing + 1 >= len(entry) or entry[closing + 1] != ":":
            raise ValueError("test SSH allowlist IPv6 entry must use [address]:port")
        host = entry[1:closing]
        port_text = entry[closing + 2 :]
    else:
        if entry.count(":") != 1:
            raise ValueError("test SSH allowlist entry must be an exact host:port")
        host, port_text = entry.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("test SSH allowlist port is invalid") from exc
    if not 1 <= port <= 65_535 or str(port) != port_text:
        raise ValueError("test SSH allowlist port is invalid")
    return _normalize_allowlist_address(host), port


def _is_public_address(address: IPAddress) -> bool:
    if address in _METADATA_IPS:
        return False
    if any(address in network for network in _DOCUMENTATION_NETWORKS):
        return False
    if not address.is_global or any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_unspecified,
            address.is_reserved,
        )
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None and not _is_public_address(address.ipv4_mapped):
            return False
        if address.sixtofour is not None and not _is_public_address(address.sixtofour):
            return False
        if address.teredo is not None and not _is_public_address(address.teredo[1]):
            return False
        if address in _WELL_KNOWN_NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            if not _is_public_address(embedded):
                return False
    return True


def _parse_resolution(records: Iterable[str]) -> tuple[IPAddress, ...]:
    parsed: set[IPAddress] = set()
    try:
        for record in records:
            parsed.add(ipaddress.ip_address(record))
    except (TypeError, ValueError) as exc:
        raise safe_failure("invalid_target") from exc
    if not parsed:
        raise safe_failure("invalid_target")
    if len(parsed) > 64:
        raise safe_failure("invalid_target")
    return tuple(sorted(parsed, key=lambda address: (address.version, int(address))))


class TargetPolicy:
    """Validate, resolve, and pin one SSH target for a bootstrap job."""

    def __init__(
        self,
        *,
        environment: str,
        test_allowlist: Sequence[str] = (),
        resolver: TargetResolver | None = None,
    ) -> None:
        normalized_environment = environment.strip().lower()
        parsed_allowlist = frozenset(_parse_allowlist_entry(entry) for entry in test_allowlist)
        if parsed_allowlist and normalized_environment != "test":
            raise ValueError("TEST_SSH_TARGET_ALLOWLIST is only allowed in ENVIRONMENT=test")
        self._environment = normalized_environment
        self._test_allowlist = parsed_allowlist
        self._resolver = resolver or SystemTargetResolver()

    @property
    def is_test(self) -> bool:
        return self._environment == "test"

    @property
    def environment(self) -> str:
        return self._environment

    async def resolve(self, address: str, port: int) -> TargetIdentity:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise safe_failure("invalid_target")

        allowlisted = False
        try:
            normalized = _normalize_address(address)
        except BootstrapError:
            try:
                normalized = _normalize_allowlist_address(address)
            except ValueError as exc:
                raise safe_failure("invalid_target") from exc
            if (normalized, port) not in self._test_allowlist:
                raise safe_failure("invalid_target") from None
            allowlisted = True
        else:
            allowlisted = (normalized, port) in self._test_allowlist

        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            records = await self._resolver.resolve(normalized)
            addresses = _parse_resolution(records)
        else:
            addresses = (literal,)

        if not allowlisted and any(not _is_public_address(item) for item in addresses):
            raise safe_failure("invalid_target")
        rendered = tuple(str(item) for item in addresses)
        return TargetIdentity(
            address=normalized,
            port=port,
            resolved_ip=rendered[0],
            resolution_set=rendered,
            test_allowlisted=allowlisted,
        )

    async def revalidate(self, target: TargetIdentity) -> TargetIdentity:
        """Resolve again and retain the originally selected IP or fail closed."""

        try:
            current = await self.resolve(target.address, target.port)
        except BootstrapError as exc:
            raise safe_failure("target_resolution_changed") from exc
        if current.test_allowlisted != target.test_allowlisted:
            raise safe_failure("target_resolution_changed")
        if target.resolved_ip not in current.resolution_set:
            raise safe_failure("target_resolution_changed")
        # Continue using the original IP even if DNS answer ordering changes.
        return target.model_copy(update={"resolution_set": current.resolution_set})


def parse_test_allowlist(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated environment setting without CIDR semantics."""

    if value is None or not value.strip():
        return ()
    entries = tuple(item.strip() for item in value.split(","))
    if any(not item for item in entries):
        raise ValueError("TEST_SSH_TARGET_ALLOWLIST contains an empty entry")
    return entries


__all__ = [
    "DNS_TIMEOUT_SECONDS",
    "MAX_HOST_LENGTH",
    "SystemTargetResolver",
    "TargetPolicy",
    "TargetResolver",
    "parse_test_allowlist",
]
