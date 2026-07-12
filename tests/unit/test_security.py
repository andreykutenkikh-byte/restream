from __future__ import annotations

import re

import pytest

from app.core.security import (
    InvalidMasterKeyError,
    SecretDecryptionError,
    decrypt_destination_key,
    digest_opaque_token,
    encrypt_destination_key,
    generate_csrf_token,
    generate_ingest_key,
    generate_master_key,
    generate_session_id,
    hash_password,
    password_hash_needs_rehash,
    rotate_ingest_key,
    verify_csrf_token,
    verify_password,
)


def test_password_hash_uses_argon2id_and_random_salts() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("$argon2id$")
    assert second.startswith("$argon2id$")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)
    assert not password_hash_needs_rehash(first)


@pytest.mark.parametrize("invalid_hash", ["", "not-a-hash", "$argon2i$broken"])
def test_password_verification_fails_closed_for_invalid_hash(invalid_hash: str) -> None:
    assert not verify_password("password", invalid_hash)


def test_password_hash_rejects_empty_password() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_ingest_key_is_url_safe_random_and_has_256_bit_entropy() -> None:
    keys = {generate_ingest_key() for _ in range(8)}

    assert len(keys) == 8
    assert all(len(key) >= 43 for key in keys)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in keys)


def test_rotate_ingest_key_replaces_previous_value() -> None:
    old_key = generate_ingest_key()

    assert rotate_ingest_key(old_key) != old_key


def test_token_entropy_floor_is_enforced() -> None:
    with pytest.raises(ValueError, match="128 bits"):
        generate_ingest_key(15)


def test_destination_key_fernet_round_trip_is_authenticated() -> None:
    master_key = generate_master_key()
    plaintext = "youtube-stream-key-не-сохранять"

    encrypted = encrypt_destination_key(plaintext, master_key)

    assert plaintext not in encrypted
    assert encrypted != encrypt_destination_key(plaintext, master_key)
    assert decrypt_destination_key(encrypted, master_key) == plaintext


def test_destination_key_cannot_be_decrypted_with_another_key() -> None:
    encrypted = encrypt_destination_key("destination-secret", generate_master_key())

    with pytest.raises(SecretDecryptionError):
        decrypt_destination_key(encrypted, generate_master_key())


@pytest.mark.parametrize("master_key", ["", "not-base64", "ключ"])
def test_invalid_master_key_is_rejected(master_key: str) -> None:
    with pytest.raises(InvalidMasterKeyError):
        encrypt_destination_key("destination-secret", master_key)


def test_session_and_csrf_tokens_are_independent_and_constant_time_comparable() -> None:
    session_id = generate_session_id()
    csrf_token = generate_csrf_token()

    assert session_id != csrf_token
    assert verify_csrf_token(csrf_token, csrf_token)
    assert not verify_csrf_token(csrf_token, "wrong")
    assert not verify_csrf_token(csrf_token, None)
    assert not verify_csrf_token(None, csrf_token)


def test_opaque_token_digest_is_deterministic_without_retaining_token() -> None:
    token = generate_session_id()
    digest = digest_opaque_token(token)

    assert digest == digest_opaque_token(token)
    assert token not in digest
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
