# Future deployment and rollback

Production deployment is intentionally outside Stage 1. The following procedure is a gated
plan for a separately approved change window.

## Deployment

1. Complete `docs/production-audit.md` and record a go decision.
2. Install the repository under `/opt/adojapan-restream` with a production `.env` owned by the
   deployment account and excluded from Git. Generate and store independent values for
   `SESSION_SECRET` and `WORKER_AUTH_PASSWORD`; production startup rejects reuse between them.
3. Back up the project SQLite database and the one reverse-proxy site file to be changed.
4. Validate configuration with the explicit project name.
5. Build and start only this project:

   ```bash
   docker compose -p adojapan-restream -f compose.yml config --quiet
   docker compose -p adojapan-restream -f compose.yml build
   docker compose -p adojapan-restream -f compose.yml up -d
   ```

6. Add only the dedicated `restream.adojapan.ru` site to the existing reverse proxy, validate
   its configuration, and safely reload it.
7. Run the post-start checks in the audit runbook and compare the saved before/after snapshots.

## Rollback

Stop and remove only this Compose project while preserving its persistent volumes:

```bash
docker compose -p adojapan-restream -f compose.yml down --remove-orphans
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
