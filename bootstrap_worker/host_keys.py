"""SHA-256 host-key pinning for explicit fingerprints and first-use TOFU."""

from __future__ import annotations

import base64
import binascii
import hmac
import re

from bootstrap_worker.errors import safe_failure
from bootstrap_worker.models import HostKeyResult, HostTrustMode

_FINGERPRINT = re.compile(r"^(?:SHA256:)?([A-Za-z0-9+/]{42}[AEIMQUYcgkosw048])={0,2}$")
SUPPORTED_HOST_KEY_ALGORITHMS = frozenset(
    {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
        "rsa-sha2-256",
        "rsa-sha2-512",
    }
)


def normalize_fingerprint(value: str) -> str:
    """Return canonical OpenSSH ``SHA256:`` form after validating 32 bytes."""

    if not isinstance(value, str):
        raise TypeError("fingerprint must be a string")
    candidate = value.strip()
    match = _FINGERPRINT.fullmatch(candidate)
    if match is None:
        raise ValueError("fingerprint must be an OpenSSH SHA256 fingerprint")
    encoded = match.group(1)
    try:
        digest = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("fingerprint contains invalid base64") from exc
    if len(digest) != 32:
        raise ValueError("fingerprint must contain a SHA-256 digest")
    canonical = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{canonical}"


class HostKeyVerifier:
    """Validate one presented host key before SSH user authentication proceeds."""

    def __init__(
        self,
        *,
        expected_fingerprint: str | None,
        pinned_fingerprint: str | None,
    ) -> None:
        try:
            self._expected = (
                normalize_fingerprint(expected_fingerprint)
                if expected_fingerprint is not None
                else None
            )
            self._pinned = (
                normalize_fingerprint(pinned_fingerprint)
                if pinned_fingerprint is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise safe_failure("ssh_host_key_changed") from exc
        if (
            self._expected is not None
            and self._pinned is not None
            and not hmac.compare_digest(self._expected, self._pinned)
        ):
            raise safe_failure("ssh_host_key_changed")
        self._result: HostKeyResult | None = None

    @property
    def result(self) -> HostKeyResult | None:
        return self._result

    def verify(self, algorithm: str, fingerprint: str) -> HostKeyResult:
        if algorithm not in SUPPORTED_HOST_KEY_ALGORITHMS:
            raise safe_failure("ssh_host_key_unsupported")
        try:
            normalized = normalize_fingerprint(fingerprint)
        except (TypeError, ValueError) as exc:
            raise safe_failure("ssh_host_key_unsupported") from exc

        required = self._pinned or self._expected
        if required is not None and not hmac.compare_digest(required, normalized):
            raise safe_failure("ssh_host_key_changed")
        if self._result is not None:
            if self._result.algorithm != algorithm or not hmac.compare_digest(
                self._result.fingerprint, normalized
            ):
                raise safe_failure("ssh_host_key_changed")
            return self._result

        if self._pinned is not None:
            trust_mode = HostTrustMode.PINNED
        elif self._expected is not None:
            trust_mode = HostTrustMode.EXPECTED
        else:
            trust_mode = HostTrustMode.TOFU
        self._result = HostKeyResult(
            algorithm=algorithm,
            fingerprint=normalized,
            trust_mode=trust_mode,
        )
        return self._result


__all__ = ["HostKeyVerifier", "SUPPORTED_HOST_KEY_ALGORITHMS", "normalize_fingerprint"]
