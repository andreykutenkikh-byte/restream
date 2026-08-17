# Future deployment and rollback

Production deployment and remote-node onboarding require separate, explicitly approved change
windows. This document is a gated plan only. Preparing Stage 4A did not deploy the application,
connect to a production server, change DNS/firewalls/reverse proxy, restart an existing service,
or create production credentials.

The planned shared host is `147.45.231.225`. An attached restream node is a different host and
must pass the onboarding gates in [Node onboarding](node-onboarding.md).

## Required production profile

Every control-plane production command must load exactly `compose.yml` followed by
`compose.production.yml`. Never load `compose.ci.yml`, `TEST_DESTINATION_ALLOWLIST`, or
`TEST_SSH_TARGET_ALLOWLIST` in a production lifecycle. The effective service set is exactly
`backend`, `bootstrap`, and `mediamtx`:

| Service | CPU | RAM | PIDs | Production purpose |
| --- | ---: | ---: | ---: | --- |
| backend + one copy worker | 0.40 | 384 MiB | 96 | web/API and one destination |
| bootstrap worker | 0.10 | 128 MiB | 64 | authenticated UDS and bounded SSH egress |
| MediaMTX | 0.20 | 192 MiB | 64 | RTMP ingest and internal HLS |
| **Total** | **0.70** | **704 MiB** | **224** | one destination |

The override is fail-closed: its effective values include `ENVIRONMENT=production`,
`COOKIE_SECURE=true`, `MAX_DESTINATIONS=1`, `PUBLIC_DOMAIN=restream.adojapan.ru`,
`PUBLIC_RTMP_HOST=restream.adojapan.ru`, `PUBLIC_RTMP_PORT=1935`,
`PUBLIC_CONTROL_URL=https://restream.adojapan.ru`, and `NODE_PROTOCOL_VERSION=1`.

The production `.env` must provide independent `SESSION_SECRET`, `WORKER_AUTH_PASSWORD`, and
`BOOTSTRAP_WORKER_SECRET` values, plus the existing Fernet/admin settings. It must also provide
`BOOTSTRAP_WORKER_SECRET_FILE`, pointing to a mode-`0600` file owned by UID/GID `10001:10001` and
containing exactly the same generated bootstrap secret with no diagnostic output. Keep that file
outside Git and the Docker build context. The backend reads the `.env` value while Compose mounts
the file read-only into the fixed non-root bootstrap worker; authenticated readiness fails when they differ. It must also provide
`NODE_AGENT_IMAGE` as an immutable amd64 registry reference ending in
`@sha256:<64 lowercase hexadecimal characters>`. Do not use a mutable tag, locally built image ID,
or CI-only `adojapan-restream-node:ci` value. The separate **Node Agent image** workflow publishes
`linux/amd64` images only on an explicit `node-v*` tag or manual dispatch, exports the resulting
registry digest, and then starts a separate fresh-runner job with no package permission or registry
login. That job uses an empty temporary Docker configuration to pull
`ghcr.io/andreykutenkikh-byte/restream-node@<digest>` and verifies the exact digest locally. Record
the fully successful workflow, digest, build provenance, reviewed commit, and vulnerability review;
publishing an image alone is not deployment authorization.

The first package release may require one manual owner action: publish the first image, set the
`restream-node` GHCR package visibility to **public**, and rerun the failed anonymous-pull job. Do not
use the digest until the complete workflow is successful. Do not automate visibility with a PAT or
privileged secret. Production operators must not run `docker login ghcr.io` on attached VPS hosts,
and bootstrap never transfers registry credentials to them.

The base Compose file fixes HTTP to `127.0.0.1:8088`. The reviewed public RTMP identity is
`restream.adojapan.ru:1935`, while its host bind remains separately controlled by the approved
`RTMP_BIND_ADDRESS` (planned `147.45.231.225:1935`). The bootstrap worker publishes no port: the
backend reaches it only through the named UDS volume, and it uses a separate egress network for
SSH. Validate these safe fields with a structured parser; never print the resolved environment.

## Evidence required before deployment

1. Complete every gate in [Production audit](production-audit.md) and record a go decision.
2. Require a successful CI run for the exact reviewed commit. CI must load the files in order
   `compose.yml`, `compose.production.yml`, `compose.ci.yml`, exercise the CI-only
   `ci-ssh-target` and `ci-node-agent`, and prove that neither fixture has a host port or appears in
   the effective production service set.
3. Verify the effective limits are 0.40/384 MiB/96 PIDs for backend, 0.10/128 MiB/64 PIDs for
   bootstrap, and 0.20/192 MiB/64 PIDs for MediaMTX.
4. Verify the bootstrap worker has only its UDS volume and `bootstrap-egress`, the backend UDS
   mount is read-only, MediaMTX has no bootstrap network, and no service mounts the Docker socket.
5. Verify all three effective production restart policies are `unless-stopped` and the bootstrap
   worker has a 90-second stop grace; changed Compose settings require container recreation and a
    post-start `docker inspect`, not merely `docker compose -p adojapan-restream restart`.
6. Resolve and approve the exact `NODE_AGENT_IMAGE` digest and retain a successful anonymous-pull
   gate for that same reference. The control-plane rollout must not build the Node Agent in
   production, retag it, substitute another digest, use a mutable tag, or treat publish success
   without anonymous-pull evidence as a released image.
7. Keep DNS ownership, host/provider firewall, reverse-proxy, port, resource/OOM, backup, and
   existing-service checks at GO. CI evidence never closes those operational gates.

## Control-plane deployment

1. Install the reviewed repository under `/opt/adojapan-restream` with a root/deployment-owned
   production `.env` and a bootstrap secret file excluded from Git, both mode `0600`. The dedicated
   bootstrap file must be owned by UID/GID `10001:10001`, while `.env` remains deployment-owned.
   Generate the three trust-domain secrets independently, then write the bootstrap value identically
   to its `.env` entry and dedicated file.
2. Back up the project SQLite database and only the reverse-proxy site file scheduled to change.
3. Validate the production model using the explicit project name and file order.
4. Build and start only this project:

   ```bash
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --quiet
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml build
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml up -d
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps
   ```

5. Add only the dedicated `restream.adojapan.ru` site to the existing proxy, validate its
   configuration, and safely reload it.
6. Run the post-start audit and compare before/after snapshots. Verify the bootstrap healthcheck
   without exposing its secret or socket externally.

Do not combine first control-plane rollout, DNS/firewall cutover, and first real SSH onboarding
unless one reviewed change plan explicitly authorizes and sequences all three. Stage 4A nodes do
not carry video, so onboarding one does not validate a media path.

## Control-plane rollback

Stop and remove only this Compose project while preserving persistent volumes:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml down --remove-orphans
```

Restore only the dedicated reverse-proxy site backup and safely reload the existing proxy. If a
database rollback is explicitly approved, keep the backend stopped and restore only a selected
project backup:

```bash
python scripts/restore.py backups/adojapan-restream-YYYYMMDDTHHMMSSZ.db \
  --database data/restream.db --confirm RESTORE_ADOJAPAN_RESTREAM
```

Ordinary rollback never deletes persistent volumes. Volume deletion requires a separate reviewed
command and explicit human confirmation. Do not stop Docker itself or another Compose project.

## Remote-node rollback and revocation

During bootstrap, automatic rollback is constrained by
`/opt/adojapan-restream-node/.managed-by-adojapan` with exact value
`adojapan-restream-node:v1`:

- before enrollment, a failed new install brings down only Compose project
  `adojapan-restream-node` and removes only the correctly marked managed directory;
- a failed managed update can restore its saved previous Compose/credential/process state under
  the exact marker-and-node-ID guard, including after enrollment;
- after enrollment, a failed new install can stop its exact Compose project but retains its managed
  directory and current evidence;
- rollback never removes Docker, prunes Docker state, directly edits SSH/firewall configuration, or touches
  a foreign directory/service.

The remote installer never invokes firewall tools, edits existing user rules, or changes Docker
daemon/firewall configuration. “No firewall change by AdoJapan” does not mean byte-for-byte static
netfilter state: installing/starting an absent Docker Engine or creating its project-scoped bridge
can add Docker-managed rules required for bridge networking, NAT, and isolation. Those standard
Docker-managed rules are explicitly permitted. An already supported Docker daemon is not
reconfigured or restarted, and the Node Agent has neither host-published ports nor host networking.

Managed updates keep root-owned mode-`0600` rollback copies under the marker-owned directory:
`.compose.rollback-<job UUID>` and, when credentials are involved,
`.enrollment.rollback-<job UUID>` or `.node-token.rollback-<job UUID>`. A successful commit or a
verified successful restore removes those temporary copies, even when the restore follows
enrollment. An incomplete restore retains the copies, marker, node ID, configuration, and
credential evidence for a separately reviewed recovery; do not improvise deletion.

Enrollment is the filesystem-deletion boundary for a **new** install. Before it, exact
marker/node-ID rollback may remove the owned scope after successful Compose shutdown. After it,
cancellation/failure may still stop that exact fresh project and the backend revokes the issued
token, but automatic rollback retains its managed root and current evidence. Existing managed
installs are different: their exact guarded rollback may restore the saved prior state after
enrollment and then remove the temporary rollback copies after verified success. The final
container check and completed workflow transition must succeed before rollback is fully disarmed.

After that commit, the administrator may revoke the node token, which blocks heartbeat/commands and
cancels in-flight control commands, but revocation does not SSH to the node or remove its
container/files. Stage 4A has no remote uninstall operation. Any manual cleanup must be designed,
reviewed, and authorized as a later node-specific change; do not improvise `rm -rf`, Docker prune,
or host-wide package removal.
