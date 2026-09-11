# Native HK relay agent runbook

This package adds an outbound-only control agent and a root Unix-socket broker. It does not
install Docker, open a listening network port, or change Amnezia, firewall rules, routes,
interfaces, MediaMTX, or `moblin-relay.service` state. The existing interactive `relayctl`
remains the local fallback and its `/etc/moblin-relay/secrets.json` remains the only relay
secret store.

## Install or update (staged, no relay mutation)

From a reviewed checkout on the HK host:

```bash
sudo sh deploy/hk-relay-agent/install.sh
sudo adojapan-relay-install-token
sudo systemctl status adojapan-relay-broker.socket --no-pager
```

The token prompt is hidden. Paste the permanent node token there; never put it in a shell
argument, environment variable, unit, command substitution, or deployment log. `install.sh`
preserves the token and keeps a one-generation `relay_agent.old` code rollback. Before swapping
code, it also creates `/etc/adojapan-relay-agent/commands.v1.rollback.json`: a root-owned,
mode-`0600`, old-agent-compatible command journal. An already valid rollback point is never
overwritten. It deliberately does not enable or start the agent on a first install.

Before first activation, verify the control plane has no queued relay commands. Then:

```bash
sudo systemctl enable --now adojapan-relay-agent.service
sudo systemctl status adojapan-relay-agent.service --no-pager
```

For an update of an already active agent, first confirm the same empty-command condition, stop
only `adojapan-relay-agent.service` and the socket-activated broker service, run `install.sh`, and
start the agent again. The broker socket stays available throughout. The installer refuses a
live update if either process is still resident:

```bash
sudo systemctl stop adojapan-relay-agent.service
sudo systemctl stop adojapan-relay-broker.service
sudo sh deploy/hk-relay-agent/install.sh
sudo systemctl start adojapan-relay-agent.service
```

No broker action is run by installation. The agent uses outbound HTTPS only. The root broker is
reachable only at `/run/adojapan-relay/broker.sock`, mode `0660 root:restream-agent`, validates
Linux `SO_PEERCRED` against the exact `restream-agent` UID, and accepts at most 16 KiB of strict
JSON. Its action allowlist is `status`, `start`, `stop`, `configure_youtube`,
`configure_youtube_key`, `clear_youtube`, and `reveal_moblin_url`. Local broker calls carry a
clamped absolute monotonic deadline and use a
true total 20-second timeout. The broker rejects stale/backlogged mutations, gives an isolated
root worker at most 11 seconds, and retains a six-second reconciliation window before the client
deadline. A timed-out worker and its descendants are killed and reaped. START/STOP capture the
original active/enabled state while holding relayctl's existing `control.lock`; a failed or timed
out operation restores and proves that exact state, with no queued systemd job, before a failure
reply is returned. If restoration cannot be proven, the broker closes the request without
claiming a completed failure. YouTube atomic save is the final blocking commit operation, so no
health probe can turn an already committed configuration into a later timeout.

The optional loopback preview credential is generated locally and never displayed:

```bash
sudo adojapan-relay-install-preview-token --generate
```

Creation or rotation fails closed unless both `moblin-relay.service` and
`adojapan-relay-agent.service` are inactive with no main process. Install the renderer and
credential together, then start only the agent. A later rotation uses the same stop-update-start
sequence so MediaMTX and the agent cannot temporarily use different credentials.

The broker intentionally does **not** use `PrivateNetwork=yes`: relay health reads the existing
MediaMTX metrics endpoint on host loopback `127.0.0.1:9998`. Address families are restricted,
the broker has no network listener, its IP access is limited to localhost, and the agent is
denied all socket binds while retaining outbound HTTPS and Unix-socket client access. The broker
has an empty capability set. It reads `ActiveState`/`MainPID` from systemd, the SRT listener via
fixed `ss` arguments, and source/forward health from the existing loopback metrics endpoint. LIVE
requires both the public SRT publisher on `iphone-live` and the internal RTMP normalizer publisher
on `relay-output`; bitrate remains an ingress-SRT sample, while path readiness, YouTube forwarding,
and HLS preview use `relay-output`. The broker does not require cross-process memory access.

## Local telemetry history

Once the existing socket-activated broker process has started, a dedicated thread samples
loopback MediaMTX metrics about every five seconds. It runs independently of HTTPS heartbeats,
operator requests, and the relay command lock. There is no additional daemon or service.
An outage before the broker first starts, or while that process is stopped, leaves a history
gap; samples are not reconstructed later. Exporting history does not activate the broker.

```bash
sudo relayctl history
sudo relayctl history --since '2026-09-05T08:00:00Z' --until '2026-09-05T10:00:00Z' --json
```

The default report covers the previous six hours. Both range arguments require ISO timestamps
with an explicit timezone (`Z` or an offset such as `+10:00`); naive local times are rejected.
`--json` returns exact samples with UTC Unix timestamps. `--limit` accepts 1–10000 and defaults
to 10000. An oversized export returns the newest samples in chronological order with
`truncated: true`; split a large range into smaller windows to retrieve every sample.
These commands only read the local database. They do not read relay secrets or invoke a
START/STOP, recovery reset, or YouTube configuration action.

The database is `/var/lib/adojapan-relay-history/history.sqlite3`, under a `0700 root:root`
directory, with mode `0600 root:root` and one hard link. Retention is seven days, capped at
120960 rows and 64 MiB of database pages; a bounded rollback journal may exist during a write.
Exports use a read-only SQLite connection and a separate bounded history lock without
creating side files. Unsafe owners, permissions, symlinks, hard links, and side files are
rejected. Collection failures leave streaming and control available, with only the rate-limited
fixed message `relay_history_collection_failed` for persistence failures.

Samples contain unique and gross SRT ingress bytes/bitrate, normalizer RTMP output bytes/bitrate,
RTMPS-forward outbound bytes/bitrate, SRT RTT and received/unique/loss/drop/retransmission packet
counters and deltas, plus fixed source/error states. Missing data, counter resets, reconnects,
ambiguous publishers, and long sample gaps yield `null` for unproven deltas and rates.
Zero means a valid interval with no counter growth. Packet categories may overlap.
Raw labels, connection UUIDs, IP addresses, URLs, keys and passwords are never persisted or
exported. YouTube outbound measurements prove server-side sending, not viewer delivery or
playback. MediaMTX's forward counter spans internal destination retries: an observed error/idle
state breaks the baseline, but a retry entirely between polls cannot be distinguished.

The history reader ships with the matching broker package. Before its first collected sample,
or when storage cannot be safely opened, `relayctl history` reports unavailability. History
adds no fields to the existing heartbeat, model or dashboard contracts.

## Relay recovery runtime contract

The paired relay runtime uses a two-second jointly verified input/output stall threshold,
a 2.5-second output-only fallback, and a two-second metrics-blind timeout. These thresholds
allow short pauses to recover within the existing FFmpeg process. The native downstream
capture no-growth acceptance limit remains three seconds. A short verified stall only restarts
the local bridge; resetting SRT still requires six seconds of continuously validated unchanged
media counters for the same publisher, followed by fresh confirmation samples. The interval
starts at the first valid unchanged-counter observation: the already proven two-second joint
stall carries forward, without adding another full six seconds after SLATE. Counter growth,
metric failures or a publisher identity change invalidate that evidence. Native self-test
deadlines are unchanged.

The privileged renderer owns `/run/moblin-relay` as `0750 root:moblin-relay` and writes
`mediamtx.json` and `control-api.token` atomically as `0640 root:moblin-relay`. The relay unit
does not use `RuntimeDirectory=`, which could recursively transfer ownership to the service
account after rendering. A subsequent unprivileged `ExecStartPre` checks the recovery credential
as the actual `moblin-relay` user before MediaMTX starts. Root `ExecStopPost` cleans up only the
validated runtime files and directory, retaining permanent relay secrets.

Credential checks log only `moblin-relay-normalize:credential-check:<fixed-code>`, with `ready`
on success. `relayctl incidents` maps failures into `RECOVERY_CREDENTIAL_*`, including
`PARENT_OWNER_UNSAFE`, `FILE_OWNER_UNSAFE`, `METADATA_UNSAFE`, `READ_DENIED`, `MISSING`,
`METADATA_UNAVAILABLE`, `FILE_CHANGED`, `OVERSIZED`, `READ_FAILED`, `FORMAT_INVALID`, and
`UNAVAILABLE`. No credential contents, URLs or exception details appear in those records.
Update the relay unit, renderer and normalizer together to preserve this ownership contract.

## Safe checks

```bash
sudo systemctl cat adojapan-relay-agent.service adojapan-relay-broker.service
sudo systemctl show adojapan-relay-agent.service -p User -p Environment -p ExecStart
sudo journalctl -u adojapan-relay-agent.service -u adojapan-relay-broker.service --no-pager
sudo stat -c '%a %U %G %F' /etc/adojapan-relay-agent/node.token \
  /etc/adojapan-relay-agent/commands.v1.rollback.json \
  /run/adojapan-relay/broker.sock
sudo stat -c '%a %U %G %F' /var/lib/adojapan-relay-history
```

Expected: token `600 restream-agent restream-agent regular file`; socket
`660 root restream-agent socket`; history directory `700 root root directory`.
Once collected, `history.sqlite3` is a `600 root root regular file`.
Journals contain only fixed safe error codes and completion
status, never Authorization, request bodies, YouTube configuration, or returned SRT URLs.
The command journal is mode 0600 and stores only command ID/action plus a safe status snapshot.
The current agent writes journal v2 and strictly accepts an existing legacy v1 journal. Journal
v2 is required for key-only YouTube rotation entries and the optional input-bitrate sample.

## Rotate the node credential

The agent reads its credential once at process start. In the control UI, first confirm there is
no queued, leased, acknowledged, or currently executing relay command. Then stop only the control
agent, rotate the server-side node credential, install the matching token through hidden input,
and start the agent again:

```bash
sudo systemctl stop adojapan-relay-agent.service
sudo adojapan-relay-install-token
sudo systemctl start adojapan-relay-agent.service
sudo systemctl status adojapan-relay-agent.service --no-pager
```

Verify a fresh heartbeat in the control UI. This sequence never stops, starts, enables, or
reconfigures `moblin-relay.service`.

## Rollback / uninstall

For an agent-code rollback, stop the agent and the socket-activated broker process, exchange the
fixed `relay_agent`/`relay_agent.old` directories under `/usr/local/lib/adojapan-relay-agent`, and
restore the compatible journal **before** starting the old agent:

```bash
sudo systemctl stop adojapan-relay-agent.service
sudo systemctl stop adojapan-relay-broker.service
sudo adojapan-relay-restore-v1-journal
```

The restore command is root-only and refuses to run while either process is active. It validates
the protected backup and live journal metadata without following links, then atomically restores
an agent-owned mode-`0600` v1 journal. The projection intentionally omits v2-only key-rotation
entries and removes the optional bitrate field. This may allow such a command to be delivered again
after a later upgrade, so confirm the control-plane command queue before either rollback or
re-upgrade. The journal restore and code-directory exchange do not touch the relay or its secrets.

```bash
sudo sh deploy/hk-relay-agent/uninstall.sh
```

Before uninstalling, confirm in the control UI that no command is queued, leased, acknowledged,
or currently executing; otherwise stopping the control agent could interrupt acknowledgement of
an operation that already reached `relayctl`.

Uninstall retains `/etc/adojapan-relay-agent/node.token`, the root-only v1 rollback point and
restore command, `/var/lib/adojapan-relay-agent/commands.json`, local history under
`/var/lib/adojapan-relay-history`, and the system account so recovery remains possible.
History collection stops when the broker is removed; retained files are not automatically
pruned while no collector is running. After the control-plane credential is revoked and rollback is no longer needed,
the two exact relay-agent data directories, `/usr/local/sbin/adojapan-relay-restore-v1-journal`,
and the account may be removed manually. Never remove
`/etc/moblin-relay`, its baseline backup, or the `moblin-relay` user as part of this procedure.
