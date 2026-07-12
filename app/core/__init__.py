"""Shared security, validation, redaction, and retry primitives."""

from .backoff import BackoffExhausted, ExponentialBackoff, calculate_backoff
from .redaction import (
    REDACTION_MARKER,
    mask_secret,
    redact_destination_url,
    redact_mapping,
    redact_text,
    redact_url,
)
from .security import (
    SecretDecryptionError,
    decrypt_destination_key,
    encrypt_destination_key,
    generate_csrf_token,
    generate_ingest_key,
    generate_session_id,
    hash_password,
    rotate_ingest_key,
    verify_csrf_token,
    verify_password,
)
from .validation import (
    CodecCompatibility,
    URLValidationError,
    ValidatedDestinationURL,
    check_stream_compatibility,
    is_public_address,
    validate_destination_url,
    validate_rtmp_url,
)

__all__ = [
    "BackoffExhausted",
    "CodecCompatibility",
    "ExponentialBackoff",
    "REDACTION_MARKER",
    "SecretDecryptionError",
    "URLValidationError",
    "ValidatedDestinationURL",
    "calculate_backoff",
    "check_stream_compatibility",
    "decrypt_destination_key",
    "encrypt_destination_key",
    "generate_csrf_token",
    "generate_ingest_key",
    "generate_session_id",
    "hash_password",
    "is_public_address",
    "mask_secret",
    "redact_destination_url",
    "redact_mapping",
    "redact_text",
    "redact_url",
    "rotate_ingest_key",
    "validate_destination_url",
    "validate_rtmp_url",
    "verify_csrf_token",
    "verify_password",
]
