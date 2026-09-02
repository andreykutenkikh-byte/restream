# Native Moblin relay control protocol

The native relay is represented by one `restream_nodes` identity with the
`moblin_relay` capability and one extension row in `relay_nodes`. It initiates
all control-plane connections over HTTPS. The integration does not require an
inbound management port or access to Docker from the web application.

## Provisioning

Provision a new identity from the backend container/environment:

```bash
python -m app.cli provision-relay-node --name "HK relay" --address 203.0.113.10
```

The raw node token is printed once. Only its SHA-256 digest is stored. A
duplicate address fails. Rotating an existing stopped relay is an explicit
operation:

```bash
python -m app.cli provision-relay-node --name "HK relay" \
  --address 203.0.113.10 --rotate-existing
```

Provisioning refuses to convert a generic node. Rotation of a live credential
requires a heartbeat no more than 30 seconds old that reports `inactive` with
the main process stopped. Rotation also fails while a relay command is queued,
leased, acknowledged, or reported as currently executing, or while a bootstrap
job is active. Rotation clears old presence and any unconsumed reveal; the new
credential is unavailable until its first authenticated heartbeat. A revoked
relay may be rotated without an impossible fresh heartbeat because revocation
already fences its credential and cancels its work. A relay stuck in
`connecting` with no bootstrap job may be revoked and then recovered by that
explicit rotation flow.

Revoking a relay credential atomically cancels every pending relay command,
replaces each pending encrypted payload with an encrypted empty object, and
deletes every unconsumed relay secret result. Provisioning, enqueue, completion,
and one-time secret consumption commit their bounded, non-secret audit event in
the same SQLite transaction as the state change.

## Agent protocol v1

Every request uses `Authorization: Bearer <node token>`:

- `POST /relay-agent/v1/heartbeat`
- `GET /relay-agent/v1/commands/next?wait=20`
- `POST /relay-agent/v1/commands/{command_id}/ack`
- `POST /relay-agent/v1/commands/{command_id}/complete`

Agent version 1.1 adds a backward-compatible optional `preview_requested`
boolean to a successful heartbeat response. Version 1.2 adds the separately
gated key-only YouTube command and the optional LIVE input-bitrate sample.
Older agents receive the original response shape and cannot be leased a command
they do not support. Preview media never uses the small control route: the agent
uploads completed MPEG-TS segments to the separate, node-authenticated
`/relay-media/v1/preview/segments/{generation}/{sequence}` endpoint.

The fixed action allowlist is `STATUS`, `START`, `STOP`,
`CONFIGURE_YOUTUBE`, `CONFIGURE_YOUTUBE_KEY`, `CLEAR_YOUTUBE`, and
`REVEAL_MOBLIN_URL`. A leased `CONFIGURE_YOUTUBE` payload contains
`youtube_rtmps_url` and `youtube_stream_key`; `CONFIGURE_YOUTUBE_KEY` contains
only `youtube_stream_key` and is available only to agent 1.2 or newer. All
other command payloads are empty objects.

Leases last 120 seconds and retry at most three times. A command has a ten-minute
absolute delivery lifetime, but a lease is issued only when the entire
120-second execution budget fits before expiry. A queued command, or an expired
lease, is terminalized and erased as soon as that full budget no longer fits;
the agent is never given a shortened lease that can execute past expiry.
Acknowledgement and completion are idempotent. SQLite stores every queued
payload as Fernet ciphertext. Terminal completion or delivery failure atomically
replaces that ciphertext with an encrypted empty object.

An `Idempotency-Key` is cryptographically bound to the exact action and command
payload; reusing it for any different request returns a fixed `409` without
rendering either payload. At most one non-`STATUS` command may be pending for a
relay node. Equivalent in-flight Moblin URL reveals are coalesced; every other
conflicting mutation receives the same fixed pending-command response. An exact
idempotent replay can recover a completed, unconsumed reveal while the agent is
offline; node existence, protocol, and revocation are still checked first, and
every new command still requires a fresh heartbeat.

`REVEAL_MOBLIN_URL` is the only action allowed to return `secret_result`.
The result is encrypted at rest, returned once through the protected admin
facade, and then atomically deleted. Unconsumed results are removed after ten
minutes.

## Administrator API

Read operations require an authenticated session. Mutations additionally
require the synchronizer CSRF token and an exact same-origin `Origin` header.
YouTube configuration, clearing, and Moblin URL reveal require the current
administrator password as a step-up check. Failed step-up checks use a separate
per-session-and-client throttle; it neither consumes nor resets the login
throttle and returns a bounded `Retry-After` when locked.

- `GET /api/nodes/{node_id}/relay`
- `POST /api/nodes/{node_id}/relay/refresh`
- `POST /api/nodes/{node_id}/relay/start`
- `POST /api/nodes/{node_id}/relay/stop`
- `PUT /api/nodes/{node_id}/relay/configure-youtube`
- `PUT /api/nodes/{node_id}/relay/configure-youtube-key`
- `DELETE /api/nodes/{node_id}/relay/youtube`
- `POST /api/nodes/{node_id}/relay/reveal-moblin-url`
- `GET /api/nodes/{node_id}/relay/commands/{command_id}`

Start, stop, refresh, configure, and clear return `202` with a `command_id`.
The UI must poll the relay-specific command endpoint and report success only
after `state=completed` and `completion_status=ok`. A queued command is not a
successful operation. The reveal facade waits up to 20 seconds and returns the
SRT URL only on a successful terminal result; otherwise it returns a safe
pending or fixed error response.

Admin status and command views never include a command payload, YouTube value,
or SRT URL. The authorized one-time reveal response is the only intentional
secret-bearing admin response. API responses use `Cache-Control: no-store`,
and audit details contain only node IDs, action names, command IDs, and fixed
statuses.

## Remote relay preview transport

The browser renews a short preview lease with same-origin session, CSRF, and
Origin protection, then reads a session-authenticated playlist and segments:

- `POST /api/nodes/{node_id}/relay/preview/lease`
- `GET /api/nodes/{node_id}/relay/preview/index.m3u8`
- `GET /api/nodes/{node_id}/relay/preview/segment/{generation}/{sequence}.ts`

The HK host remains outbound-only. MediaMTX HLS must bind only to
`127.0.0.1:8888`, use the `mpegts` variant and two-second segments, and grant
read access to `iphone-live` only to the dedicated `relay-preview` user. No HLS,
API, WebRTC, firewall, route, interface, Docker, Amnezia, SRT, or management
port is published or changed for this feature.
The transport uses the standard MPEG-TS HLS functionality present in MediaMTX
1.19.2 and does not depend on its control API or WebRTC; the HK image version
must nevertheless remain whatever the existing relay manifest already pins.

The backend accepts no agent-supplied playlist or URL. It validates canonical
generation/sequence values, exact `video/mp2t`, declared and streamed size,
and every 188-byte MPEG-TS sync byte. It keeps at most four segments and 12 MiB
per node, 24 MiB process-wide, and purges on lease expiry, non-LIVE heartbeat,
successful stop completion, administrator logout, and application shutdown.
Nginx raises its body allowance to 3 MiB only for the exact relay-media segment
route and disables request and response buffering there; the global 1 MiB and
the relay-agent 16 KiB limits remain unchanged.

While `moblin-relay.service` and the control agent are inactive, generate the
local HLS reader credential without printing or transporting it:

```bash
sudo adojapan-relay-install-preview-token --generate
```

The root-only MediaMTX renderer must read that same local file, write only the
supported password hash into its root-owned runtime configuration, and never
place the value in argv, environment, logs, Git, unit files, or reports. The
agent reads `/etc/adojapan-relay-agent/preview-reader.token` as
`restream-agent`; absence or unsafe ownership/mode disables only preview.
Deployment must verify owner `restream-agent`, mode `0600`, loopback listeners,
and external refusal on ports 8888 and 9998 before enabling the agent. Rollout
order is backend and narrow proxy route, then the inactive HK agent, then the
loopback renderer; rollback is the reverse order and does not touch relay or
YouTube secrets.

## Operator data flow

Keep these three values separate:

1. On the main **Трансляция** page, enter the **YouTube stream key** in the
   protected first-step dialog. The exact **YouTube RTMPS URL** from Live
   Control Room is required only during initial setup or when its endpoint must
   be replaced; it stays collapsed under the dialog's additional settings once
   configured. Both values are written to the HK relay's existing root-owned
   secret store and are never shown back. A reusable stream key normally needs
   to be set once; use the same dialog to replace only the key whenever YouTube
   issues a new one.
2. Reveal and copy only the **SRT URL** into Moblin at
   `Settings → Streams → profile → URL`. If Moblin offers to replace it with a
   direct YouTube RTMP URL, choose `No`.
3. Put the scheduled broadcast's **YouTube Video ID** in Moblin at
   `Settings → Streams → profile → Streaming platforms → YouTube → Manage
   streams / Video IDs`. The Video ID is not the stream key and normally
   changes for every scheduled broadcast.

Moblin's relay profile is portrait: Portrait ON, 720p producing 720×1280,
30 FPS, H.264/AVC, 3.5–4 Mbit/s, adaptive bitrate ON, two-second keyframes,
SRT latency 2000–3000 ms, and local recording ON.

The safe broadcast order is: create the scheduled YouTube broadcast; configure
the key on the main «Трансляция» page if needed; start the relay; wait for the
slate and good Stream health; press Go Live manually in YouTube Studio; then
start SRT in Moblin. To finish, end the YouTube broadcast manually before
stopping the relay.
