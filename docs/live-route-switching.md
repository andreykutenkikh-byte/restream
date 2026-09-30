# Live route switching

BroadcastOutput owns YouTubeCredential. The encrypted one-to-one output binding is
canonical; a relay receives a temporary EgressCredentialLease for one output, node,
PRIMARY/BACKUP slot, generation and deadline. A standby receives no YouTube key.
See [credential correction and delta review](credential-leases.md).

The controller uses the same BroadcastSession, output, stream name and YouTube IDs
through A → B → C. It never calls YouTube insert, bind or complete during a switch.
Two independent outputs still require different credentials. Moving output B to node D
uses B's credential only and does not move the phone when `handoff_ingress=false`.

## Durable sequence

```text
REQUESTED → VALIDATING_TARGET → PREPARING_TARGET
  → TARGET_MEDIA_READY → TARGET_CREDENTIAL_LEASED
  → TARGET_EGRESS_STARTING → TARGET_YOUTUBE_CONNECTED → CUTOVER_ARMED
  → OLD_EGRESS_DRAINING → OLD_CREDENTIAL_REVOKED
  ├─ egress only: EGRESS_SWITCH_COMPLETED
  └─ phone handoff: AWAITING_DIRECT_SOURCE → DIRECT_SOURCE_SEEN
       → DIRECT_SOURCE_CONFIRMED → REWARMING_OLD_ROUTE → COMPLETED

Before cutover: ROLLING_BACK → FAILED or CANCELLED
```

REQUESTED validates a running output, explicit v2 opt-in, fresh capabilities,
compatible H.264/AAC portrait profile, capacity and a free slot. PREPARING_TARGET
warms only the encrypted inter-relay media copy. Readiness requires advancing video
and audio PTS, at least 90 packets/frames, repeated valid samples, nonzero bitrate,
stable source identity and observations no older than five seconds. TCP/SRT socket
connection alone cannot authorize a key or cutover.

Only TARGET_MEDIA_READY can assign the target's temporary key. Before draining the
old publisher, the target must report a current plan/lease, a live publisher,
nonzero outgoing bytes and at least 90 advancing publisher frames. Primary and backup
share one stream name; they are not separate key settings. Two valid transition leases
exist while both publishers send. Cutover revokes the old assignment, increments the
output-wide generation, and preserves the target. The old slot remains reserved until
a matching stop/secret-removal acknowledgment or its last grant deadline plus five seconds.

SQLite transactions and a ten-second controller owner lease serialize reconciliation.
Duplicate request keys return the existing operation; conflicting bodies fail. A new
controller owner waits for the old owner deadline, reads durable actual observations,
and does not create a third lease. Preparation times out after 120 seconds. Target failure
or cancellation before cutover revokes only the target. After cutover there is no blind
rollback or timeout merely because the phone has not moved. Healthy forwarded input
continues while the UI says: «Сервер передачи переключён. Ожидается прямое подключение Moblin.»

AWAITING_DIRECT_SOURCE never treats forwarded packets as phone proof. Three advancing
direct runtime samples and repeated controller observations are required. Then the session
ingress changes, the target's old forwarding path is removed and the old route has a free
slot. REWARMING_OLD_ROUTE marks standby availability, not a measured phone path.

## Egress and phone handoff are separate

After egress cutover the valid topology is phone → A → B → YouTube. Only the later phone
reconnect changes it to phone → B → YouTube. Selecting phone handoff changes ingress for
the entire source/session, including other outputs; the confirmation explains this scope.
Use egress-only move for an independent multicast output relocation.

The relay has a stable logical source identity per route and two authenticated physical
paths (`forward/...` and `source/.../direct`). The current copy pipeline replaces the
publisher process when the selected physical input changes. It does not provide a stable
downstream process or a zero-gap input splice. Measurements are in
[route acceptance](route-switch-acceptance.md).

The inspected [MediaMTX 1.19.2 path implementation](https://github.com/bluenviron/mediamtx/blob/v1.19.2/internal/core/path.go)
closes the previous publisher when overriding it; ordinary source removal closes readers.
Its `AlwaysAvailable` branch can retain a stream with offline media, but that is not proof
of a decoded-frame-continuous splice between these two independently timed copy inputs.
No such splice was implemented or certified here. FFmpeg's fixed input copy process is
replaced deliberately; [the pinned protocol documentation](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/doc/protocols.texi)
was used for RTSP/SRT timeout and transport options. This candidate is a measured bounded
reconnect, not a claim that hot switching is impossible in every MediaMTX configuration.

## Standard Moblin workflow

Before going live, an administrator opens **Профили Moblin** and imports the generated
`moblin://?` settings link on the phone. It contains per-node source SRT credentials,
not YouTube credentials. Close the dialog to discard the link from the page. Configure
H.264/AAC, portrait 1080×1920, 30 fps and a two-second keyframe interval in Moblin;
the current import sets names, source URLs and `selected=false`, not the video profile.

The inspected [Moblin settings model at b305b1de](https://github.com/eerimoq/moblin/blob/b305b1de82289be7fca035b7880cf64a08e2f29f/Moblin/Various/MoblinSettingsUrl.swift)
supports named stream URL imports and optional selection. This schema does not establish
that a settings URL safely changes the active LIVE stream. The operator button opens
standard Moblin; the user selects the already saved target profile and reconnects.
There is no custom app, invented LIVE deep link, or claimed physical-device verification.

## Access and evidence in the UI

The existing stream monitor remains read-only. The broadcast extension appears only when
a broadcast session exists and waits for the original HUD's authenticated render.
Operator access is separate: explicit admin checkbox, one-use ten-minute pairing fragment,
session-scoped secure HttpOnly SameSite=Strict cookie, server expiry and immediate revoke.
Pairing and session tokens are stored only as HMAC digests. Allowed operations are switch,
pre-cutover cancel and self-logout, with Origin/Fetch Metadata, CSRF, rate and body limits.
It cannot create outputs, change credentials, start/stop output, use OAuth or access admin APIs.

Admin can see credential stored and active lease counts without the key. Every route shows
phone → server, inter-relay and server → YouTube evidence separately. SERVER READY means
fresh agent/capability evidence. An unused phone route stays UNKNOWN; RTT/loss/retransmits
remain UNKNOWN when no measurement exists. Publisher connected is not viewer playback.
YouTube API health is aggregate; per-slot viewer health remains UNKNOWN. Assisted policy
offers a manual suggestion only. Auto failover remains disabled.

Admin batch start/stop uses explicit output checkboxes; individual controls remain available.
Selection survives status polling in memory and never enters browser storage. History records
output live/failure and inter-relay connection transitions only when state changes, plus
YouTube stream/health change events without copying provider payloads into event detail.
