# Broadcast orchestrator

## Provenance and release stack

Fetched 2026-09-30. Main: `8136480d22ca7d4fdae82c70b4b34c1d638a9026`.
PR17 remains open/draft at `cf84fcdb0d5e58ae07ddca9fd783c0ca3c469d61`.
PR A is based on that exact HUD commit, B on A, C on B. PR17 is not rewritten.
PR15/16 code is excluded. Historical `STUCK_LIVE_CAUSE=UNRESOLVED` and
`STALL_SWITCH_CAUSE=UNRESOLVED` remain unchanged.

The control plane stores source → session → outputs → routes → relay nodes.
Each independent YouTube output owns its own stream and broadcast binding.
Primary and backup ingest belong to that single binding and share one stream name.
The existing destination and relay v1 interfaces continue to work independently.

Migration 8 is additive; HUD retains migration 7 and native classification 6 is
never marked without executing its own migration. Foreign keys, existing rows,
SQLite sequences, sessions, node credentials and HUD devices are preserved.

Manual configuration does not require Google OAuth. All public projections are
allowlisted; credentials use the existing master-key Fernet mechanism. Durable
request keys reject reuse with different bodies, including changed credentials.
The controller never handles media packets.

Migration 10 adds output-wide egress generations and temporary EgressCredentialLease.
BroadcastOutput owns the canonical YouTubeCredential on the control plane; a relay receives
it only while assigned a role. See [credential delta review](credential-leases.md).

Migration 9 adds managed media nodes, forwarding and measured observations; migration 11
adds separate operator pairing/session records and safe publisher/lease measurements.
The current schema version is 11; the sparse applied sequence remains 1–5, 7–11, without
claiming the unrelated migration 6. Existing Fernet encryption, admin sessions, CSRF/origin
checks, node identity, SQLite transaction helpers, audit, templates and HUD pairing are reused.
No existing bootstrap, native relay runtime or legacy permanent-key command is replaced.

Required capabilities are `multi_output_v1`, `inter_relay_srt_v1`, `route_switch_v1`,
`youtube_dual_ingest_v1`, `egress_credential_lease_v1`; heartbeat protocol is 2.
The UI separates SERVER READY from PHONE → SERVER ROUTE MEASURED and publisher progress
from confirmed viewer playback. See [switch state machine and Moblin workflow](live-route-switching.md),
[actual acceptance](route-switch-acceptance.md), and [future staged plan](staged-broadcast-deployment.md).

`manual` is the default policy. `assisted` provides recommendations only.
The schema reserves `auto`, but activation is rejected and the database enforces
`auto_enabled = 0`. No production deployment or real YouTube acceptance is implied.

## Environment and validation boundaries

The implementation workspace is a fresh local clone. This Windows host initially
has neither Docker nor WSL. Linux container gates must run in GitHub CI; local
native media fixtures must report their actual FFmpeg and MediaMTX versions.
No remote relay, production address, real ingestion credential or YouTube event
may be used by local or CI tests. Physical iPhone and real viewer playback remain
separate owner-authorized acceptance tasks.
