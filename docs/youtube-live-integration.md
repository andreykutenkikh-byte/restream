# YouTube Live integration

BroadcastOutput owns YouTubeCredential; a relay receives a temporary EgressCredentialLease.
See [credential authority and correction](credential-leases.md). No standby key provisioning
or permanent orchestrated node configuration is required.

Reviewed 2026-09-30 against the official [liveStreams resource](https://developers.google.com/youtube/v3/live/docs/liveStreams),
[liveBroadcasts resource](https://developers.google.com/youtube/v3/live/docs/liveBroadcasts),
[broadcast lifecycle](https://developers.google.com/youtube/v3/live/life-of-a-broadcast)
and the [v3 discovery document](https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest).
The discovery document confirms `rtmpsIngestionAddress`, `rtmpsBackupIngestionAddress`
and the common `streamName` in `IngestionInfo`. RTMPS still uses ingestionType `rtmp`.

An independent output owns one unique broadcast and one unique stream, even on the
same channel. No channel concurrency limit is guessed. Primary and backup routes for
the **same** output use those same IDs and streamName. Route switches must never call
insert, bind-to-a-new-stream, or transition complete. Auto-stop and auto-start are disabled.
Stream/health status is aggregate YouTube evidence; it cannot certify an individual
primary/backup connection, nor viewer playback. Manual mode leaves these fields UNKNOWN.

## Manual setup

Create the event in YouTube Studio, then save its official RTMPS server address and
key in the output form. A backup address is optional but required for dual-ingest route
switching. Use a different key for every independent output. Both modes validate exact
official hostnames, TLS port, path and backup query; redirects and arbitrary destinations
are not supported. The key is encrypted using the existing master key, never returned
by a dashboard/HUD API, placed in browser storage or shown after saving.

## Optional OAuth

Configure `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` and `YOUTUBE_REDIRECT_URI`
using the existing deployment secret mechanism. The redirect is the public HTTPS URL
`/api/broadcasts/youtube/callback`. Nothing is configured or connected by default.
The requested scope is `youtube.force-ssl`, an officially accepted write scope for
Live API operations. State is short-lived, single-use, tied to the administrator session;
PKCE S256 is required. The verifier and refresh token use existing Fernet encryption.
Access tokens are used only server-side. Callback query arguments are removed from
Uvicorn access records; configure any external proxy to omit this query too before rollout.

Connect and revoke require admin CSRF + same-origin + Fetch Metadata checks. Revocation
is reported confirmed only after Google's revoke response. An invalid refresh grant
marks the account revoked and clears its stored token. Lifecycle transitions are explicit
admin operations and forbidden while a route switch is active.

## Durable uncertainty and retries

Provisioning records STREAM_PENDING before insert. A response timeout or process crash
leaves this phase durable. The next request searches the account for the exact unique
marker; a found object is adopted. If no object can be found, the operation remains
`youtube_reconciliation_required`. It does **not** issue a second insert. Streams are
reusable so `mine=true` can list them for reconciliation, while database uniqueness still
enforces one stream per output. Reconciliation scans at most 20 pages and fails closed
on multiple matches or an incomplete scan. Broadcast insert follows the same rule.
Binding is retried against the same two IDs. A ten-minute database lease prevents
concurrent provisioning, and every write checks the lease owner and expiry.

Recover an unresolved operation by reviewing the marker in the account during a separate
authorized acceptance session. There is intentionally no automatic “create another event”
escape hatch. Quota, rate, permissions, live-disabled, invalid-transition and both concurrent
broadcast/shared-ingestion limits retain safe reason codes. API bodies, keys and tokens
never become user-visible error text. No retry loop spends quota while a user is absent.

## Acceptance boundary

Unit tests use a fake provider and `httpx.MockTransport`: independent create/bind, loss of
insert responses, restart, replay, permissions/quota reasons, OAuth state, refresh/revocation,
RTMPS allowlisting and encrypted persistence. No real OAuth authorization, channel, event,
key, stream, or viewer was used. REAL_YOUTUBE_ACCEPTANCE=NOT_RUN.
