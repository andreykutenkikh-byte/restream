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
64 PIDs. The aggregate ceiling is 0.60 CPU, 576 MiB RAM, and 160 PIDs.

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
confirm the actual Docker resource limits, and reject a second destination with
`409 destination_limit_reached`. That synthetic evidence is not deployment authorization. The
DNS-owner and host/provider firewall gates in the production audit remain NO-GO until separately
approved.

## Deployment

1. Complete `docs/production-audit.md` and record a go decision.
2. Install the repository under `/opt/adojapan-restream` with a production `.env` owned by the
   deployment account and excluded from Git. Generate and store independent values for
   `SESSION_SECRET` and `WORKER_AUTH_PASSWORD`; production startup rejects reuse between them.
3. Back up the project SQLite database and the one reverse-proxy site file to be changed.
4. Validate configuration with the explicit project name, environment file, base file, and
   shared-host production override.
5. Build and start only this project:

   ```bash
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --quiet
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml build
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml up -d
   docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps
   ```

6. Add only the dedicated `restream.adojapan.ru` site to the existing reverse proxy, validate
   its configuration, and safely reload it.
7. Run the post-start checks in the audit runbook and compare the saved before/after snapshots.

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
