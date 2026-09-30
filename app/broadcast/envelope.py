"""Authenticated node-bound delivery; control APIs only return ciphertext."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def decode(value: str) -> bytes:
    return base64.b64decode(value, altchars=b"-_", validate=True)


def public_key(private: X25519PrivateKey) -> str:
    return encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def _key(shared: bytes, context: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context).derive(shared)


def seal(public: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    ephemeral = X25519PrivateKey.generate()
    aad = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    key = _key(ephemeral.exchange(X25519PublicKey.from_public_bytes(decode(public))), aad)
    nonce = secrets.token_bytes(12)
    return {
        "context": context,
        "ephemeral_public": public_key(ephemeral),
        "nonce": encode(nonce),
        "ciphertext": encode(
            ChaCha20Poly1305(key).encrypt(
                nonce, json.dumps(payload, separators=(",", ":")).encode(), aad
            )
        ),
    }


def open_envelope(
    private: X25519PrivateKey, envelope: dict[str, Any], expected_node: str
) -> dict[str, Any]:
    context = envelope["context"]
    if context["node_id"] != expected_node or context["purpose"] != "broadcast-desired-v2":
        raise ValueError("Envelope scope mismatch")
    aad = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    peer = X25519PublicKey.from_public_bytes(decode(envelope["ephemeral_public"]))
    payload: dict[str, Any] = json.loads(
        ChaCha20Poly1305(_key(private.exchange(peer), aad)).decrypt(
            decode(envelope["nonce"]), decode(envelope["ciphertext"]), aad
        )
    )
    return payload
