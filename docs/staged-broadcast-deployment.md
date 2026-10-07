# Staged broadcast deployment plan — not executed

This is a reviewable future plan. It does not authorize a production/HK connection,
firewall change, installation, merge, OAuth grant, or real YouTube event.

1. Review the A/B/C Draft stack and exact source/base/merge-tree CI evidence. Back up the
   existing database and master encryption key separately with restricted access. Verify
   restore on an isolated copy; migrations 8–11 are additive and preserve sparse migration
   history, legacy nodes, HUD sessions, IDs, foreign keys and sequences. Roll back application
   binaries/configuration only after stopping new managed outputs; never downgrade a live DB
   by deleting migration markers or restoring over newer user data.
2. Allocate isolated staging nodes and identities. Keep v1 `legacy_static` services and
   permanent legacy configuration unchanged. Explicitly opt new nodes into `managed_egress`
   with pinned X25519 public keys, exact capabilities, compatible media profiles, measured
   resource capacity and separate ports/runtime directories. There is no automatic migration.
   A separately installed v2 service may use an existing Node Agent or relay identity after
   explicit media enablement with its pinned key. The original v1 credentials and services
   remain unchanged; v2 acceptance does not authorize either identity in the other v1 API.
3. Verify HTTPS certificate/hostname and time synchronization on control plane and nodes.
   Configure proxy access logs to omit OAuth callback query values. Exclude environment,
   process argv, memory/core dumps, transport URLs and runtime files from log/crash/backup
   collectors. Use a dedicated service account, restricted runtime directory/tmpfs and 0600
   key/fence/config files. Preserve the non-secret generation fence across process restart;
   after complete host loss require fresh authenticated authority before publishing.
4. Pin/checksum MediaMTX/FFmpeg and install only through separately approved service packaging.
   Declare the outbound HTTPS/RTMPS and ingress SRT UDP rules needed by the selected nodes;
   keep RTMP/RTSP/API loopback-only. Review each rule before applying it. Configure finite
   publisher/forward/bitrate/CPU/memory/PID budgets, including simultaneous transition leases.
   The 300-second lease bounds outage survival; expiry intentionally stops egress.
5. First run synthetic media on staging with loopback test sinks in an isolated lab process.
   Verify ≥90 decoded frames and audio packets, codec/GOP/timestamps, unrelated-output failure
   isolation, B→D transfer, A→B→C, rollback, owner crash, old-node partition, expiry and restart.
   Check all runtime secret objects/arguments disappear on revoke/expiry. Do not publish
   synthetic test destination overrides through the installed agent API/CLI.
6. In a separately authorized owner session, create at least two **unlisted** independent
   YouTube events with different stream names; record IDs without recording keys. Verify
   manual mode first, optional OAuth separately, including permissions, quota/limit errors,
   lost-response reconciliation and revoke. Disable YouTube auto-stop/auto-start. Confirm
   independent output stop/restart never changes the other event.
7. For one unlisted output, verify official primary/backup ingestion addresses and one shared
   stream name. Observe target media and primary/backup overlap before old revoke, then
   perform A→B→C. Record real broadcast/stream IDs, page URL, chat continuity, viewer count
   and timestamped playback/audio gaps on an independent receiver. Do not use aggregate
   YouTube health as proof of individual-slot or viewer continuity. Stop if viewer behavior
   differs from the lab assumptions; do not complete the event as part of route switching.
8. Use the standard App Store Moblin on a physical iPhone. Import saved profiles before LIVE,
   manually confirm portrait H.264/AAC 1080×1920/30/GOP2s, pair the monitor, then separately
   grant limited operator access. Test one-tap app opening and manual target-profile selection
   while LIVE. Measure egress cutover, phone reconnect and source gaps separately on two
   network paths, including weak signal/congestion, app backgrounding and reconnect.
9. Exercise operator expiry, revoke, logout failure, duplicate tap, reload, loss of monitoring
   and recovery. Confirm read-only monitor cannot switch, operator cannot create/start/stop
   outputs or see keys, and unused phone routes remain UNKNOWN. Keep assisted mode advisory
   and auto failover disabled. Revoke temporary access and stop staging outputs after evidence
   collection. A later rollout decision requires the owner to review real acceptance results.

If preparation fails before cutover, keep the old working publisher and revoke the target.
After cutover, inspect actual media/lease state; do not blindly restore the old key. Keep
healthy forwarding while the phone has not moved. An unreachable old relay retains a
bounded lease until its deadline, so do not reuse that slot prematurely. YouTube itself
does not enforce agent leases; an OS administrator who already copied a key cannot be
cryptographically made to forget it. This design assumes trusted managed agents and a
separately reviewed key-rotation response for compromised hosts.
