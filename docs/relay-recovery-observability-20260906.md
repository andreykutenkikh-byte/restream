# Relay recovery and observability — 2026-09-06

## Scope and evidence

This change follows the September 5 stream analysis, not a new deployment or a
replacement of MediaMTX. The operator confirmed the final Moblin stop at about
23:03 Vladivostok time (UTC+10); the subsequent slate and manual relay stop are
intentional and must not be counted as recovery failures.

The recorded main session contained repeated normalizer stall/restart events and
corrupt-media diagnostics. Historical bitrate, RTT and loss samples were absent,
so their exact relationship to each quality drop cannot be reconstructed. Short
watchdog thresholds were a plausible contributing factor, not proven as the sole
cause of the network or video failures.

## Changes

- Joint input/output stall: 0.5 → 2 seconds; output-only fallback: 0.9 → 2.5
  seconds; metrics-blind timeout: 0.75 → 2 seconds. Video remains H.264 copy,
  portrait 1080x1920; no quality cap or new transcoder is introduced.
- The exact-publisher recovery gate still requires six seconds of uninterrupted,
  valid no-growth evidence plus fresh checks before the bounded API request.
  Already verified watchdog evidence carries into that interval. Growth, failed
  measurements or an identity change invalidate it. No test deadline was enlarged.
- A real isolated systemd test reproduced `RuntimeDirectory=` transferring a
  root-written recovery credential to the service UID. The renderer now owns the
  runtime directory; the service reads but cannot write the credential. An
  unprivileged preflight verifies access before MediaMTX starts. Stop cleanup
  removes validated secret files, including interrupted writes. Only a proven
  empty bind-mounted directory may remain after `EBUSY`.
- The existing broker collects safe telemetry every five seconds, independently
  of panel heartbeats and command execution. No new service or listener is added.
  Broker child reaping is scoped to its own worker process group so it cannot
  consume a concurrent history sampler's service-query exit status.
- History lives in root-only `/var/lib/adojapan-relay-history/history.sqlite3`,
  not below a potentially relay-owned media directory. Limits: seven days,
  120960 samples, 64 MiB database pages plus a bounded rollback journal.
- `sudo relayctl history` reports recent measurements; `--json --since ...
  --until ...` exports exact samples using explicitly zoned ISO timestamps.
  Unknown/reset/reconnected values are `null`, not zero. Raw metric labels,
  connection IDs, addresses, transport URLs and credentials are not stored.
  RTMPS outbound bytes are not proof of YouTube viewer delivery.

## Verification

- Local full suite: **1111 passed, 21 skipped** (platform-specific Linux checks
  are not claimed as Windows passes). Ruff, formatting, Linux-targeted mypy and
  repository safety policy passed.
- HK Python 3.10 root filesystem fixtures: ownership, read permissions, atomic
  writes, partial cleanup, unsafe entries and empty bind-mount cleanup passed.
- Real isolated systemd regression: old runtime became UID 999 and credential
  was rejected; fixed runtime remained root-owned and was accepted by UID 999.
  Synthetic credentials were removed after the probe.
- HK history isolation gate passed, including bounded storage/export, unsafe
  filesystem refusal, concurrent collection and broker requests, and preserving
  an unrelated helper's nonzero exit status during worker-group reaping.
- HK native loopback `--quick` matrix: **PASS**, 2026-09-05 16:22:17–16:31:31
  UTC (September 6 locally). Outages of 15/17/19 seconds, same-session recovery,
  persistent stalled input, supervisor crash, repeated bridge failures and a
  forced RTMP sink disconnect were exercised. The 120/210/300-second full mode
  was not run in this update.
- The capture no-growth maximum was 2.316 seconds (unchanged 3-second gate).
  Persistent input recovery completed its exact-session reset in 8.336 seconds;
  the forced downstream disconnect recovered in 5.696 seconds (15-second gate).
  There were no duplicate downstream publishers. Its identity changed only for
  the deliberately disconnected sink; input transitions preserved it.
- Thirteen final RTMP sink segments passed strict media validation, including
  1170 video frames and 2457 audio frames. Presentation timestamps and packet
  DTS were monotonic; portrait 1080x1920/yuv420p was retained, with no 1280x720
  or legacy 720x1280 frames. Aggregate RTSP capture did contain a decode-error
  diagnostic: that pre-existing splice-sensitive diagnostic is not used as the
  final-output corruption oracle. This is **not** a claim that every diagnostic
  log was empty or that all possible output corruption has been excluded.
- Test credentials were absent from scanned journals, process arguments,
  environments and unexpected files. Temporary media/credentials were cleaned
  up and test ports released. No real YouTube destination or stream key was used.

## Operations

### HK installation result

The checked update was applied on HK. Read-only preflight verified the audited
before-hashes; the transaction created
`/var/backups/adojapan-relay-observability-20260905T164627447789Z` before replacing
these eight files atomically:

| Installed file | Repository source |
| --- | --- |
| `/usr/local/sbin/relayctl` | `deploy/moblin-relay/relayctl` |
| `/opt/moblin-relay/libexec/moblin-relay-normalize` | `deploy/moblin-relay/moblin-relay-normalize` |
| `/usr/local/libexec/moblin-relay-render-config` | `deploy/moblin-relay/moblin-relay-render-config` |
| `/etc/systemd/system/moblin-relay.service` | `deploy/moblin-relay/moblin-relay.service` |
| `/usr/local/lib/adojapan-relay-agent/relay_agent/broker.py` | `relay_agent/broker.py` |
| `/usr/local/lib/adojapan-relay-agent/relay_agent/history.py` | `relay_agent/history.py` |
| `/etc/systemd/system/adojapan-relay-broker.service` | `deploy/hk-relay-agent/adojapan-relay-broker.service` |
| `/etc/tmpfiles.d/adojapan-relay-agent.conf` | `deploy/hk-relay-agent/adojapan-relay-agent.tmpfiles` |

The installed broker had an older fixed-host URL interface than the existing
portable relayctl. Pairing it with the repository version also preserves the
current `node.json`-based URL builder and its validation; no key was regenerated.

Post-update checks:

- Real Unix-socket `status` request as `restream-agent`: `ok`, no error code,
  no secret result, relay `inactive`, `enabled=false`.
- Twelve persisted samples over 55 seconds, five-second spacing, state `NONE` /
  `service_inactive`; bitrate/RTT remained unavailable, correctly not zero, with
  no running stream. Root-only directory/database modes were `0700` / `0600`.
- `relayctl history` read-only text export succeeded. A repeated deployment
  preflight reported `NOOP-ALREADY-CURRENT`.
- Protected fingerprints matched: relay secrets, node/preview credentials,
  node configuration, install manifest, slate text/video, MediaMTX binary,
  baseline backups and the existing command-journal rollback file.
- Existing agent, broker and socket were restored to their prior active states.
  The actual relay remained inactive and disabled throughout the update.

The root-private transaction backup contains each old file plus `metadata.json`
with its original target, owner/mode and checksum. A manual rollback must stop
the control agent and idle broker, restore only those exact files, remove the
new `history.py` only after matching its recorded checksum, reload systemd, and
restore the previous control-service states. Preserve the history database and
all relay secrets/baselines; do not start the relay as part of rollback.

Pair the renderer, normalizer, relay unit and relayctl with the matching broker
package, broker unit and tmpfiles definition. Create the root-only history
directory before restarting the existing broker. Preserve source backups and
protected configuration before any update; update only while the relay is
inactive and disabled. Do not reinstall MediaMTX or change Amnezia, Docker,
firewall, routes, interfaces or host power state.

The detailed runtime and history commands are documented in
`deploy/moblin-relay/README.md` and `deploy/hk-relay-agent/README.md`.
History starts with new collected samples; it cannot recreate previous stream
telemetry. A subsequent controlled Moblin/YouTube stream remains necessary to
evaluate real mobile-link quality and end-to-end viewer delivery.
