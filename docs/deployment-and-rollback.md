# Production deployment and rollback

Production changes and remote-node onboarding require separate, explicitly approved change
windows. Use the incremental procedure below when the `adojapan-restream` project already exists.
The first-install procedure later in this document applies only when the project is absent. Never
combine the two procedures or repeat first-install DNS, proxy, firewall, or secret initialization
during an incremental application release.

The shared control-plane host is `147.45.231.225`. An attached restream node is a different host
and must pass the onboarding gates in [Node onboarding](node-onboarding.md).

## Incremental relay-control release (schema v3)

The native Moblin relay-control release changes the backend application, static UI, and SQLite
schema from version 2 to version 3. It does not change the bootstrap image, MediaMTX image or
configuration, Compose model, reverse-proxy site, ports, or production environment. Consequently,
build and recreate **only** `backend`. `bootstrap` and `mediamtx` must retain their container IDs,
start times, restart counts, and OOM state.

Before the window, require a successful CI run for the exact reviewed commit and a clean checkout.
Preserve the existing production `.env`, bootstrap secret file, and `MASTER_ENCRYPTION_KEY`; do not
print, regenerate, or rotate them. Read the live reverse-proxy configuration without changing it
and verify that the general proxy location forwards `Authorization` and permits a request longer
than the relay agent's 20-second long poll. The reviewed 30-second proxy read timeout is sufficient.

Capture the release commit, current backend container/image, and all three container IDs before
building. Keep a rollback tag for the exact old backend image; a subsequent build otherwise moves
the normal project image tag.

```bash
test -z "$(git status --porcelain)"
release_commit="$(git rev-parse HEAD)"
release_backend_container="$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q backend)"
test -n "$release_backend_container"
release_old_image="$(docker inspect --format '{{.Image}}' "$release_backend_container")"
release_bootstrap_container="$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q bootstrap)"
release_mediamtx_container="$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q mediamtx)"
test -n "$release_bootstrap_container"
test -n "$release_mediamtx_container"
release_bootstrap_fingerprint="$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_bootstrap_container")"
release_mediamtx_fingerprint="$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_mediamtx_container")"
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml config --quiet
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml exec -T backend python - <<'PY'
import sqlite3

with sqlite3.connect("/srv/app/data/restream.db") as connection:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise SystemExit("Live database integrity check failed")
    if connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone() != (2,):
        raise SystemExit("Live database is not the expected schema v2")
print("Live schema v2 verified")
PY
release_old_image_hex="${release_old_image#sha256:}"
case "$release_old_image_hex" in
  ''|*[!0-9a-f]*) printf '%s\n' 'Unexpected old backend image ID' >&2; exit 1 ;;
esac
test "${#release_old_image_hex}" -eq 64
release_old_tag="adojapan-restream-backend:pre-relay-v3-$release_old_image_hex"
if docker image inspect "$release_old_tag" >/dev/null 2>&1; then
  release_tagged_image="$(docker image inspect --format '{{.Id}}' "$release_old_tag")"
  test "$release_tagged_image" = "$release_old_image" || {
    printf '%s\n' 'Rollback tag exists for a different image; refusing to overwrite it' >&2
    exit 1
  }
else
  docker image tag "$release_old_image" "$release_old_tag"
fi
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps
```

Create the SQLite backup through the running backend so SQLite includes the WAL consistently.
Both database and backup storage are named Compose volumes; a host-side
`python scripts/restore.py backups/... --database data/...` command does **not** address those
production volumes. The backup contains encrypted application secrets and session data, so copy it
out of the container only to a protected mode-`0600` file and retain its checksum privately.

```bash
release_backup="$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml exec -T backend \
  python scripts/backup.py --retain 14 | tr -d '\r')"
case "$release_backup" in
  /srv/app/backups/adojapan-restream-*.db) ;;
  *) printf '%s\n' 'Unexpected backup path' >&2; exit 1 ;;
esac
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml exec -T backend \
  python - "$release_backup" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise SystemExit("Backup integrity check failed")
    if connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone() != (2,):
        raise SystemExit("Unexpected pre-release schema")
print("Backup verified")
PY
install -d -m 0700 backups
release_backup_copy="backups/$(basename "$release_backup")"
docker cp "$release_backend_container:$release_backup" "$release_backup_copy"
chmod 0600 "$release_backup_copy"
```

Build and recreate only the backend. Do not pass `--build` to a project-wide `up`, and do not use
`restart`: a recreated container is required to load the new image.

```bash
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml build backend
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml up -d --no-deps --wait --wait-timeout 90 backend
curl --fail --silent http://127.0.0.1:8088/health/live
curl --fail --silent http://127.0.0.1:8088/health/ready
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml exec -T backend python - <<'PY'
import sqlite3

with sqlite3.connect("/srv/app/data/restream.db") as connection:
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
if version != (3,) or not {"relay_nodes", "relay_commands"}.issubset(tables):
    raise SystemExit("Schema v3 verification failed")
print("Schema v3 verified")
PY
release_new_backend_container="$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q backend)"
test -n "$release_new_backend_container"
test "$release_new_backend_container" != "$release_backend_container"
test "$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q bootstrap)" = \
  "$release_bootstrap_container"
test "$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q mediamtx)" = \
  "$release_mediamtx_container"
test "$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_bootstrap_container")" = "$release_bootstrap_fingerprint"
test "$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_mediamtx_container")" = "$release_mediamtx_fingerprint"
printf '%s\n' 'Backend-only container isolation verified'
```

Compare the saved IDs with the post-start snapshot. Only the backend ID and start time may change.
Verify HTTPS login/logout, `/servers`, CSRF, the relay status facade, restart count, and OOM state.
Do not provision or activate the HK agent until these checks pass. The native agent release does
not require publishing or changing `NODE_AGENT_IMAGE` because it is not a Docker Node Agent.

### Incremental rollback from schema v3

The previous backend expects schema version 2 exactly and will fail readiness against schema 3.
Code-only rollback is therefore unsafe. Stop only the HK control agent first if it was activated;
never stop, start, enable, disable, or reconfigure `moblin-relay.service` as part of control-plane
rollback. Then stop only the backend, restore the verified pre-release database through a one-off
Compose container that mounts the named volumes, restore the saved backend image tag, and recreate
only the backend.

```bash
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml stop backend
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml run --rm --no-deps backend \
  python scripts/restore.py "$release_backup" \
  --database /srv/app/data/restream.db --confirm RESTORE_ADOJAPAN_RESTREAM
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml run --rm --no-deps backend \
  python -c 'import sqlite3; c=sqlite3.connect("/srv/app/data/restream.db"); assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",); assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (2,); print("Schema v2 backup restored")'
test "$(docker image inspect --format '{{.Id}}' "$release_old_tag")" = "$release_old_image"
docker image tag "$release_old_tag" adojapan-restream-backend:latest
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml up -d --no-deps --force-recreate \
  --wait --wait-timeout 90 backend
curl --fail --silent http://127.0.0.1:8088/health/live
curl --fail --silent http://127.0.0.1:8088/health/ready
test "$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q bootstrap)" = \
  "$release_bootstrap_container"
test "$(docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml ps -q mediamtx)" = \
  "$release_mediamtx_container"
test "$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_bootstrap_container")" = "$release_bootstrap_fingerprint"
test "$(docker inspect --format \
  '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}' \
  "$release_mediamtx_container")" = "$release_mediamtx_fingerprint"
printf '%s\n' 'Rollback preserved bootstrap and MediaMTX containers'
```

Restoring the pre-release database discards every database change made after the backup, including
relay enrollment and queued commands. Use a maintenance window and decide explicitly whether that
loss is acceptable before deployment. Retain the old image and protected backup until the rollback
window closes. Never use project-wide `down`, delete a volume, reload the proxy, restart Docker, or
touch `bootstrap`/`mediamtx` for this incremental rollback.

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

Production overrides all three core services to `restart: unless-stopped`; development retains
bounded `on-failure:5`, while the final CI override sets each service to `restart: "no"`. A
deliberate project-scoped Compose stop remains effective across a Docker daemon restart. Because
`unless-stopped` can retry a persistent crash indefinitely, production monitoring must alert on
readiness failure, restart-count growth, and OOM state rather than restarting Docker or unrelated
services.

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
   the effective production service set. The same run must exercise offline and active ingest-key
   rotation against the pinned MediaMTX, reject the previous key, accept the replacement, and show
   zero unexpected restarts or OOM events. The same exact commit must pass the Debian/RHEL platform
   matrix, strict fake rpm/dnf/systemd fixture, repository policy, and real Compose parsing of the
   generated `create_host_path: false` + private-`Z` agent bind. CI stays unprivileged.
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

After this code change is independently reviewed, merged, and deployed in a separately authorized
window, the next real-host acceptance may use a disposable AlmaLinux 8.10 amd64 VPS. The operator
must supply only the normal address/SSH credentials; seeing `almalinux` 8.10 classified as RHEL
family + dnf is an observed result, never a UI selection. That later acceptance must confirm
SELinux remains Enforcing, Docker is installed from the allowlisted official-compatible RPM path,
the exact Node Agent digest is used, and no host port/host network/socket mount appears. This plan
does not authorize contacting or modifying that VPS during the coding PR.

## First control-plane deployment only

Use this section only when the `adojapan-restream` Compose project is absent. For an existing
production installation, use the incremental procedure above.

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
6. Run the post-start audit and compare before/after snapshots. Verify all three container IDs,
   start times, `RestartPolicy.Name=unless-stopped`, restart counts, OOM state, resource limits,
   readiness, and the bootstrap healthcheck without exposing secrets or the UDS externally.

Do not combine first control-plane rollout, DNS/firewall cutover, and first real SSH onboarding
unless one reviewed change plan explicitly authorizes and sequences all three. Stage 4A nodes do
not carry video, so onboarding one does not validate a media path.

## First-deployment project rollback

Stop and remove only this Compose project while preserving persistent volumes:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml down --remove-orphans
```

Restore only the dedicated reverse-proxy site backup and safely reload the existing proxy. If a
database rollback is explicitly approved, keep the backend stopped and restore only a selected
project backup through a one-off container that mounts the project's named volumes:

```bash
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml run --rm --no-deps backend \
  python scripts/restore.py \
  /srv/app/backups/adojapan-restream-YYYYMMDDTHHMMSSZ.db \
  --database /srv/app/data/restream.db --confirm RESTORE_ADOJAPAN_RESTREAM
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
On RHEL-family hosts, SELinux is not disabled or reconfigured; only the marker-owned data bind gets
Compose's private `Z` relabel, with automatic host-path creation disabled.

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
