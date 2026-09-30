"""Session-bound OAuth with PKCE, encrypted refresh tokens and explicit revocation."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.broadcast.models import BroadcastError
from app.broadcast.store import BroadcastStore
from app.broadcast.youtube import YouTubeHTTP
from app.core.security import digest_opaque_token
from app.db import utc_now

SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


class YouTubeOAuth:
    def __init__(
        self,
        store: BroadcastStore,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store = store
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.transport = transport

    def begin(self, session_token: str) -> str:
        if (
            not self.client_id
            or not self.client_secret
            or not self.redirect_uri.startswith("https://")
        ):
            raise BroadcastError("youtube_oauth_not_configured", 503)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        with self.store.transaction() as db:
            db.execute("DELETE FROM youtube_oauth_states WHERE expires_at<?", (utc_now(),))
            db.execute(
                "INSERT INTO youtube_oauth_states VALUES (?,?,?,?,NULL)",
                (
                    digest_opaque_token(state),
                    digest_opaque_token(session_token),
                    self.store.seal({"verifier": verifier}),
                    (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                ),
            )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )

    def token_request(self, data: dict[str, str]) -> dict[str, Any]:
        with httpx.Client(timeout=15, follow_redirects=False, transport=self.transport) as client:
            try:
                response = client.post(
                    "https://oauth2.googleapis.com/token",
                    data={**data, "client_id": self.client_id, "client_secret": self.client_secret},
                )
                result: dict[str, Any] = response.json()
            except (httpx.HTTPError, ValueError):
                raise BroadcastError("youtube_oauth_unavailable", 503) from None
        if not response.is_success:
            code = (
                "youtube_token_revoked"
                if result.get("error") == "invalid_grant"
                else "youtube_oauth_failed"
            )
            raise BroadcastError(code, 502)
        if not isinstance(result.get("access_token"), str):
            raise BroadcastError("youtube_oauth_failed", 502)
        return result

    def callback(self, session_token: str, state: str, code: str) -> str:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM youtube_oauth_states WHERE digest=?", (digest_opaque_token(state),)
            ).fetchone()
            if (
                not row
                or row["used_at"]
                or row["expires_at"] < utc_now()
                or row["session_digest"] != digest_opaque_token(session_token)
            ):
                raise BroadcastError("oauth_state_invalid", 403)
            verifier = self.store.unseal(row["verifier_encrypted"])["verifier"]
            db.execute(
                "UPDATE youtube_oauth_states SET used_at=? WHERE digest=?",
                (utc_now(), row["digest"]),
            )
        tokens = self.token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.redirect_uri,
            }
        )
        if not tokens.get("refresh_token"):
            raise BroadcastError("youtube_refresh_token_required", 422)
        provider = YouTubeHTTP(lambda: tokens["access_token"], transport=self.transport)
        result = provider.request("GET", "channels", {"part": "snippet", "mine": "true"})
        if len(result.get("items", [])) != 1:
            raise BroadcastError("youtube_channel_ambiguous")
        channel = result["items"][0]
        now = utc_now()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO youtube_accounts VALUES (?,?,?,'connected',?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET display_name=excluded.display_name,"
                "tokens_encrypted=excluded.tokens_encrypted,status='connected',updated_at=?",
                (
                    channel["id"],
                    channel["snippet"]["title"],
                    self.store.seal({"refresh_token": tokens["refresh_token"]}),
                    now,
                    now,
                    now,
                ),
            )
        self.store.database.add_audit_event("youtube.connected", "OAuth channel connected")
        return str(channel["id"])

    def access_token(self, channel_id: str) -> str:
        with self.store.database.connect() as db:
            account = self.store.row(
                db, "SELECT * FROM youtube_accounts WHERE channel_id=?", (channel_id,)
            )
        if account["status"] != "connected":
            raise BroadcastError("youtube_account_unavailable")
        token = self.store.unseal(account["tokens_encrypted"])["refresh_token"]
        try:
            result = self.token_request({"grant_type": "refresh_token", "refresh_token": token})
        except BroadcastError as exc:
            if exc.code == "youtube_token_revoked":
                self._disconnect(channel_id, "revoked")
            raise
        return str(result["access_token"])

    def revoke(self, channel_id: str) -> None:
        with self.store.database.connect() as db:
            account = self.store.row(
                db, "SELECT * FROM youtube_accounts WHERE channel_id=?", (channel_id,)
            )
        if account["status"] == "disconnected":
            return
        token = self.store.unseal(account["tokens_encrypted"])["refresh_token"]
        with httpx.Client(timeout=15, follow_redirects=False, transport=self.transport) as client:
            try:
                response = client.post(
                    "https://oauth2.googleapis.com/revoke", data={"token": token}
                )
            except httpx.HTTPError:
                raise BroadcastError("youtube_revoke_unconfirmed", 503) from None
        if not response.is_success:
            raise BroadcastError("youtube_revoke_unconfirmed", 502)
        self._disconnect(channel_id, "disconnected")

    def _disconnect(self, channel_id: str, state: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE youtube_accounts SET status=?,tokens_encrypted=?,updated_at=? "
                "WHERE channel_id=?",
                (state, self.store.seal({}), utc_now(), channel_id),
            )
        self.store.database.add_audit_event(f"youtube.{state}", "OAuth access removed")
