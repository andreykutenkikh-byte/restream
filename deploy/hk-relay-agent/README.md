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
preserves the token and keeps a one-generation `relay_agent.old` code rollback. It deliberately
does not enable or start the agent on a first install.

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
JSON. Its action allowlist is `status`, `start`, `stop`, `configure_youtube`, `clear_youtube`, and
`reveal_moblin_url`. Local broker calls carry a clamped absolute monotonic deadline and use a
true total 20-second timeout. The broker rejects stale/backlogged mutations, gives an isolated
root worker at most 11 seconds, and retains a six-second reconciliation window before the client
deadline. A timed-out worker and its descendants are killed and reaped. START/STOP capture the
original active/enabled state while holding relayctl's existing `control.lock`; a failed or timed
out operation restores and proves that exact state, with no queued systemd job, before a failure
reply is returned. If restoration cannot be proven, the broker closes the request without
claiming a completed failure. YouTube atomic save is the final blocking commit operation, so no
health probe can turn an already committed configuration into a later timeout.

The broker intentionally does **not** use `PrivateNetwork=yes`: relay health reads the existing
MediaMTX metrics endpoint on host loopback `127.0.0.1:9998`. Address families are restricted,
the broker has no network listener, its IP access is limited to localhost, and the agent is
denied all socket binds while retaining outbound HTTPS and Unix-socket client access. The broker
has an empty capability set. It reads `ActiveState`/`MainPID` from systemd, the SRT listener via
fixed `ss` arguments, and source/forward health from the existing loopback metrics endpoint; it
does not require cross-process memory access.

## Safe checks

```bash
sudo systemctl cat adojapan-relay-agent.service adojapan-relay-broker.service
sudo systemctl show adojapan-relay-agent.service -p User -p Environment -p ExecStart
sudo journalctl -u adojapan-relay-agent.service -u adojapan-relay-broker.service --no-pager
sudo stat -c '%a %U %G %F' /etc/adojapan-relay-agent/node.token \
  /run/adojapan-relay/broker.sock
```

Expected: token `600 restream-agent restream-agent regular file`; socket
`660 root restream-agent socket`. Journals contain only fixed safe error codes and completion
status, never Authorization, request bodies, YouTube configuration, or returned SRT URLs.
The command journal is mode 0600 and stores only command ID/action plus a safe status snapshot.

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

For an agent-code rollback, stop the agent, exchange the fixed
`relay_agent`/`relay_agent.old` directories under `/usr/local/lib/adojapan-relay-agent`, then start
the agent again. This does not touch the relay.

```bash
sudo sh deploy/hk-relay-agent/uninstall.sh
```

Before uninstalling, confirm in the control UI that no command is queued, leased, acknowledged,
or currently executing; otherwise stopping the control agent could interrupt acknowledgement of
an operation that already reached `relayctl`.

Uninstall retains `/etc/adojapan-relay-agent/node.token`,
`/var/lib/adojapan-relay-agent/commands.json`, and the system account so rollback remains possible.
After the control-plane credential is revoked and rollback is no longer needed, those two exact
relay-agent directories and the account may be removed manually. Never remove
`/etc/moblin-relay`, its baseline backup, or the `moblin-relay` user as part of this procedure.
