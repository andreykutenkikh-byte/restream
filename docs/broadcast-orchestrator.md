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
