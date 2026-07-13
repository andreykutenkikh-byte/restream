"""Server-side administrative sessions and CSRF validation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import Database


@dataclass(frozen=True, slots=True)
class NewSession:
    token: str
    csrf_token: str
    expires_at: datetime


class SessionManager:
    """Stores only keyed digests; raw browser tokens never enter SQLite."""

    def __init__(self, database: Database, secret: str, ttl_seconds: int) -> None:
        self.database = database
        self._secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def _digest(self, namespace: bytes, value: str) -> str:
        return hmac.new(
            self._secret, namespace + b":" + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def create(self) -> NewSession:
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        self.database.create_session(
            self._digest(b"session", token),
            self._digest(b"csrf", csrf_token),
            expires_at.isoformat(),
        )
        return NewSession(token=token, csrf_token=csrf_token, expires_at=expires_at)

    def get(self, token: str | None) -> dict[str, str] | None:
        if not token or len(token) > 512:
            return None
        return self.database.get_session(self._digest(b"session", token))

    def validate_csrf(self, token: str | None, csrf_token: str | None) -> bool:
        if not token or not csrf_token or len(csrf_token) > 512:
            return False
        session = self.get(token)
        if session is None:
            return False
        supplied = self._digest(b"csrf", csrf_token)
        return hmac.compare_digest(str(session["csrf_hash"]), supplied)

    def ensure_csrf(self, token: str, csrf_token: str | None) -> str:
        """Return a valid token, replacing a missing browser CSRF cookie."""

        if csrf_token and self.validate_csrf(token, csrf_token):
            return csrf_token
        session = self.get(token)
        if session is None:
            raise ValueError("Session is invalid")
        replacement = secrets.token_urlsafe(32)
        updated = self.database.update_session_csrf(
            self._digest(b"session", token), self._digest(b"csrf", replacement)
        )
        if not updated:  # pragma: no cover - session race
            raise ValueError("Session expired")
        return replacement

    def delete(self, token: str | None) -> None:
        if token and len(token) <= 512:
            self.database.delete_session(self._digest(b"session", token))
