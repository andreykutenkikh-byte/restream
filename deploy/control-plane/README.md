# Optional SSH media separation (review candidate)

The deployed all-in-one backend owns SQLite, MediaMTX authentication and FFmpeg.
Moving that backend alone either sends media to the control host or breaks the
source MediaMTX auth callback. This opt-in delta moves its process launcher and
private MediaMTX transports to one pinned SSH source. Default local behavior,
schema v5, existing IDs, encryption and credentials remain compatible.

This is not a relay-agent update, Orchestrator release or seamless handoff.
Do not deploy before review. The source controller remains authoritative during
preparation. Never start a second controller with copied production credentials.

## Transport and failure behavior

- Target: one backend/DB and the preserved bootstrap image; no MediaMTX service,
  public media listener, FFmpeg child or Docker access from the backend.
- Source: existing MediaMTX and a separate non-root media sandbox using the
  original backend image. Mount only the reviewed `scripts/media_exec.py` at
  `/opt/media_exec.py`; no DB, env file, session or node credentials in the sandbox.
- SSH uses an explicit IP, dedicated key, pinned known_hosts, no agent or SSH
  user config. API/HLS listeners bind target-container loopback. Reverse auth
  listens only on the source private Docker gateway and forwards to target
  backend-container loopback:8000. None of these ports is public.
- The helper receives bounded JSON over encrypted stdin, revalidates the public
  output URL and reconstructs fixed stream-copy arguments. It cannot run an
  arbitrary command. Worker diagnostics retain the existing redaction policy.
- A per-destination Linux flock prevents overlapping publishers across reconnects.
  Heartbeats every 3 seconds maintain a 15-second lease; EOF, invalid control,
  timeout or stop terminates and reaps the process group (3 seconds before KILL).
  A lost connection can interrupt video; this change makes no continuity promise.
  The guarantee assumes the source sandbox/helper remains alive to enforce its
  lease; a source sandbox failure requires operator reconciliation before restart.
- Missing SSH/tunnels fails startup. Reconnect failures never select a local
  launcher. Readiness continues to query the real source MediaMTX API. No readiness
  override, DB migration, key rotation or automatic media-host discovery is added.

## Reviewed installation prerequisites

Record the actual source private network, gateway, reserved MediaMTX IP and
immutable images. Check address collisions. `media-runtime.compose.yml` is a
separate SOURCE project; `compose.yml` is a standalone TARGET project, not an
overlay on the root all-in-one Compose file. Review the rendered configurations
and verify kernel-enforced CPU/memory/PIDs and log limits.

On source, provision a dedicated `restream-media` account and key, without Docker
group membership or password login. Install both exec wrappers root-owned 0755
under `/usr/local/libexec`. Give only this sudo rule, validated with `visudo -cf`:

```sudoers
restream-media ALL=(root) NOPASSWD: /usr/local/libexec/restream-media-exec-root ""
```

Use an account-scoped SSH Match block (replace placeholders with verified private
addresses). Validate effective account settings with `sshd -T -C` and keep the
existing administrative SSH connection open while testing a separate connection:

```text
Match User restream-media
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowAgentForwarding no
    X11Forwarding no
    AllowTcpForwarding yes
    AllowStreamLocalForwarding no
    GatewayPorts clientspecified
    PermitOpen <MEDIAMTX_PRIVATE_IP>:9997 <MEDIAMTX_PRIVATE_IP>:8888
    PermitListen <SOURCE_BRIDGE_GATEWAY>:18088
    ForceCommand /usr/local/libexec/restream-media-exec
Match all
```

The account's authorized_keys entry must also restrict origin to the target IP
and disable PTY/agent/X11/user-rc. Permit only the above TCP forwards. Never use
`known_hosts=None` or disable host verification. Mount target key and known_hosts
read-only, mode 0600, readable only by container UID 10001 (account for rootless
UID mapping). No SSH or signing secrets belong in Git or reports.

During the gated source maintenance window, reserve MediaMTX's private IP and
change only its auth callback to `http://backend:18088/internal/mediamtx/auth`.
Map its `backend` hostname to the verified source bridge gateway with extra_hosts.
Keep the existing MediaMTX image, public source ingest bind, paths and stream key.
Validate that the SSH reverse listener is private and both source and target
Nginx deny public `/internal/`. Do not expose API/HLS/auth ports to the Internet.

Target `backend.env` is a protected transfer of the original production settings:
same admin hash, master key, session/service secrets, node agent image and IDs.
Set trusted proxy addresses for the actual target network, not the example source
network. Preserve bootstrap secret and map UID/mode correctly. No secrets are
generated against a copied DB. Pin `CONTROL_IMAGE` to the reviewed delta artifact;
pin `BOOTSTRAP_IMAGE` and `PRESERVED_BACKEND_IMAGE` to preserved source artifacts.

## Migration gates and rollback

1. Reproduce the original source artifact on target using a synthetic empty DB,
   synthetic secrets, loopback HTTP and private auxiliary MediaMTX. Confirm exact
   runtime configs/layers, health, schema v5, zero nodes/jobs/media and limits;
   observe 10 minutes, then stop only this test project and retain its data.
2. Review this delta, its CI and the source account/network changes. Exercise
   API, HLS, reverse auth, remote launch/stop, lease expiry, host-key rejection and
   reconnect on a synthetic deployment before production approval. No real
   YouTube stream or remote relay mutation is needed for these checks.
3. Preserve the legacy input before changing the panel A record: create a separate
   media hostname pointing to the old source; the owner manually updates existing
   OBS/Moblin profiles to that hostname with the same stream key. Verify profiles
   are ready. The old panel hostname cannot point to two different hosts by port.
4. Prepare TLS/full chain, renewal and the sole Restream vhost; test HTTPS with
   hostname verification against target before DNS switch. Do not alter other
   sites, routes, firewall or Docker settings. Recheck A/AAAA and actual TTL.
5. Recheck exact SHAs and no streams, readers, publishers, enabled destinations,
   installs or pending commands. Freeze administrative writes. Stop source
   backend/bootstrap writers only. Make a final consistent SQLite backup, verify
   integrity/FKs/schema/counts and protected transfer checksums/permissions.
6. Activate source media plumbing and exactly one target controller. Source's old
   public HTTP endpoint must return explicit maintenance or proxy over a verified
   protected transport to the target; never leave the old writable backend running.
7. Change only the panel DNS record. Verify admin login, preserved IDs/settings,
   nodes through the same control URL, real readiness, zero unintended FFmpeg,
   jobs/commands, redacted diagnostics and 10 minutes of resources/restarts/OOM.
   Keep source checkout/images/volumes/backups. Synthetic checks are not production
   migration acceptance and are not proof of seamless streaming.

Before any target writes, rollback can restore the gated source snapshot and
original MediaMTX auth configuration. After target writes, first stop its writers,
capture and validate current DB/secrets, and restore those to source; do not revive
a stale DB or lose revocations. Stop source media sandbox publishers before
restoring the local launcher. DNS reversal alone is not a complete rollback.
