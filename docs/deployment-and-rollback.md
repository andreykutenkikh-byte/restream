# Future deployment and rollback

Production deployment is intentionally outside Stage 1. The following procedure is a gated
plan for a separately approved change window.

No deployment, DNS update, firewall change, Nginx change, or existing-service restart was
performed while preparing this plan. The planned shared host is `147.45.231.225`.

## Required production profile

Every production Compose command must load exactly `compose.yml` followed by
`compose.production.yml`. The CI-only `compose.ci.yml` and `TEST_DESTINATION_ALLOWLIST` must never
enter the production lifecycle. The production override restricts the backend to 0.40 CPU,
384 MiB RAM, 96 PIDs, and one destination; it restricts MediaMTX to 0.20 CPU, 192 MiB RAM, and
64 PIDs. The aggregate ceiling is 0.60 CPU, 576 MiB RAM, and 160 PIDs. It also sets both `backend`
and `mediamtx` to `restart: unless-stopped`. The base profile keeps the bounded
`restart: on-failure:5`, while the final CI override sets both services to `restart: "no"` so a
retry cannot hide a test failure.

In production, `unless-stopped` restores either service after a process, Docker daemon, or host
restart, including a clean exit that `on-failure` would not recover. A deliberate
`docker compose -p adojapan-restream stop` remains deliberate across a daemon restart. Because a persistent crash can
therefore retry indefinitely, the operator must monitor readiness, restart-count changes, and OOM
state and investigate a loop rather than repeatedly restarting the Docker daemon.

The override is fail-closed: its effective values are `ENVIRONMENT=production`,
`COOKIE_SECURE=true`, `MAX_DESTINATIONS=1`, `PUBLIC_DOMAIN=restream.adojapan.ru`,
`PUBLIC_RTMP_HOST=restream.adojapan.ru`, and `PUBLIC_RTMP_PORT=1935`. This repository has one
defined public identity, so these fixed values prevent a production `.env` from selecting
development mode, insecure cookies, or `localhost`. The `.env` must still provide independent
`SESSION_SECRET` and `WORKER_AUTH_PASSWORD` values.

The base Compose file fixes the HTTP host address to loopback, keeping HTTP on
`127.0.0.1:8088` without an environment override. The public RTMP identity is
`restream.adojapan.ru:1935`; the host-side bind remains separately configurable through the
approved `RTMP_BIND_ADDRESS`, with reviewed target `147.45.231.225:1935`. Confirm both effective
mappings with a parser that emits only these safe fields before any build or start; never print
the resolved production environment.

A successful GitHub Actions run for the exact reviewed commit must first exercise the shared-host
limits with `compose.yml`, `compose.production.yml`, and the CI-only `compose.ci.yml` in that order,
confirm the actual Docker resource limits and CI restart policy `no`, exercise offline and active
key rotation against the real pinned MediaMTX, reject the previous key, and reject a second
destination with `409 destination_limit_reached`. That synthetic evidence is not deployment
authorization. The DNS-owner and host/provider firewall gates in the production audit remain
NO-GO until separately approved.

## Deployment

1. Complete `docs/production-audit.md` and record a go decision.
2. Install the repository under `/opt/adojapan-restream` with a production `.env` owned by the
   deployment account and excluded from Git. Generate and store independent values for
   `SESSION_SECRET` and `WORKER_AUTH_PASSWORD`; production startup rejects reuse between them.
3. Back up the project SQLite database and the one reverse-proxy site file to be changed.
4. Validate configuration with the explicit project name, environment file, base file, and
   shared-host production override. Pipe the resolved model through the secret-safe policy validator;
   it must confirm `unless-stopped` for both services without printing environment values:

   ```bash
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --format json \
     | python3 scripts/validate_production_compose.py
   ```

5. Build and start only this project:

   ```bash
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --quiet
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml build
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml up -d
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps
   ```

6. Add only the dedicated `restream.adojapan.ru` site to the existing reverse proxy, validate
   its configuration, and safely reload it.
7. Resolve the exact `backend` and `mediamtx` container IDs through this Compose project, then use
   a constrained `docker inspect` format to confirm `RestartPolicy.Name=unless-stopped`, running and
   healthy state, resource limits, restart counts, OOM state, IDs, and start times. Do not inspect or
   print the container environment.

   ```bash
   backend_container_id="$(docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps -q backend)"
   mediamtx_container_id="$(docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps -q mediamtx)"
   test -n "$backend_container_id" && test -n "$mediamtx_container_id"
   docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.HostConfig.RestartPolicy.Name}} {{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}} {{.RestartCount}} {{.State.OOMKilled}} {{.State.Status}} {{.State.Health.Status}}' \
     "$backend_container_id" "$mediamtx_container_id"
   ```

8. Run the post-start checks in the audit runbook and compare the saved before/after snapshots.
   Require several consecutive successful `/health/ready` samples. Do not use a raw MediaMTX `ERR`
   count as a gate and never copy authenticated paths, stream keys, URLs, cookies, or credentials
   into logs or reports.

## Restart-policy-only rollout

Changing the Compose file and running `docker compose -p adojapan-restream restart` is not a rollout: `restart` does not
apply changed service configuration. In a separately approved production window, the normal
`docker compose -p adojapan-restream ... up -d` path may recreate the affected project containers and must therefore use
the full preflight, maintenance, and post-start gates above.

When avoiding an immediate container restart is an explicit requirement, an approved operator may
instead resolve the exact two project container IDs and update only their runtime policies:

```bash
compose=(docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml)
backend_container_id="$("${compose[@]}" ps -q backend)"
mediamtx_container_id="$("${compose[@]}" ps -q mediamtx)"
test -n "$backend_container_id"
test -n "$mediamtx_container_id"
test "$backend_container_id" != "$mediamtx_container_id"
docker update --restart unless-stopped "$backend_container_id" "$mediamtx_container_id"
```

This command is an option only inside the approved window, after the matching reviewed Compose files
are present. Verify the same IDs and start times, the new runtime policy, unchanged restart/OOM state,
and consecutive readiness success afterward. This no-restart option can leave the existing Compose
configuration hash on the containers, so a later normal `docker compose -p adojapan-restream ... up -d` can still recreate
them; schedule and verify that convergence rather than treating it as unexpected drift.

## Rollback

Stop and remove only this Compose project while preserving its persistent volumes:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml down --remove-orphans
```

Restore the dedicated reverse-proxy site backup and safely reload the existing proxy. If a
database rollback is explicitly approved, keep the backend stopped and restore only a backup
named `adojapan-restream-*.db`:

```bash
python scripts/restore.py backups/adojapan-restream-YYYYMMDDTHHMMSSZ.db \
  --database data/restream.db --confirm RESTORE_ADOJAPAN_RESTREAM
```

Persistent volumes are never deleted by ordinary rollback. Volume deletion requires a separate
reviewed command and explicit human confirmation.

If only the runtime restart-policy change must be reversed during the same approved window, target
the exact two project container IDs and restore the previous bounded policy without restarting them:

```bash
compose=(docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml)
backend_container_id="$("${compose[@]}" ps -q backend)"
mediamtx_container_id="$("${compose[@]}" ps -q mediamtx)"
test -n "$backend_container_id"
test -n "$mediamtx_container_id"
test "$backend_container_id" != "$mediamtx_container_id"
docker update --restart on-failure:5 "$backend_container_id" "$mediamtx_container_id"
```

Re-validate IDs, start times, runtime policy, restart/OOM state, and readiness. This restores the old
reboot exposure and is not a permanent remediation; also restore the previously reviewed repository
configuration before the next Compose lifecycle operation. Do not restart Docker or any unrelated
service as part of either rollout or rollback.
