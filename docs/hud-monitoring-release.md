# HUD monitoring release

This is a backend/panel release for **already registered relay servers**. It
reuses the Moblin HUD implementation from PR #16, provides one-time fragment
pairing, a separate read-only device cookie, revoke, existing Moblin homepage
import, and conservative monitoring. It does not deploy or repair the native
media runtime. A complete release result requires the candidate's own CI and
local beta evidence; historical PASS results are not inherited.

## Source and dependency boundary

The authoritative base is Git commit
`8136480d22ca7d4fdae82c70b4b34c1d638a9026`, not another local checkout. The source
HUD stack was PR #16 `cfc8b11ba33c79582d8e326eb1207b936643155d`, based on PR #15
`de42b2bfe8663b3ec6c137e5669a87c856cb66ac`. The extraction preserves the original
HUD commits' authorship and source; the release branch supplies only the small
adaptations required by its main base. PRs #15 and #16 remain separate and open.

| HUD need | Already present in pinned main | Release treatment |
| --- | --- | --- |
| Main ingest/source/bitrate | `ApplicationRuntime.ingest_view()` | Existing MediaMTX GET and in-memory sampler |
| Main output state | `ApplicationRuntime.list_destination_views()` | Existing worker status; no platform health claim |
| Registered relay inventory and stream state | `RelayService.list_nodes()` and `_status_from_row()` | Existing source, bitrate, forward, lifecycle, configuration flags, timestamp |
| Host resources and pending command | `NodeService.list_nodes()` | Existing CPU/RAM/current-command fields |
| Protocol compatibility | `RelayHeartbeatRequest`, `relay_agent.models.RelaySnapshot` | Protocol 1 retained; omitted optional bitrate stays unavailable |
| Pairing and scoped device authentication | New HUD service/tables | Digests only, atomic one-time exchange, revoke, bounded last_seen |
| Quality/recommendation | HUD `RelayQualityTracker` | Bounded in-memory state; no worker or remote actions |
| Native SSH installation and recovery | Not in pinned main | PR15 installer, media bundle and diagnostic excluded |

No compatibility changes to RelayService, NodeService, heartbeat schemas,
worker supervision, transport, preview or bootstrap are needed. The only read
compatibility distinction is that absent bitrate telemetry cannot establish a
zero-media interval. An observed numeric zero retains its existing recovery and
alert policy. Missing metrics are displayed as `Нет данных`. Forwarding means
server-reported sending, not verified YouTube playback. Standby readiness does
not measure the phone-to-standby route.

`tests/fixtures/hud_release/main_boundary.json` records 89 original Git blob
identities obtained with `git ls-tree -r` at the pinned base. The boundary test
checks unchanged runtime/services, API/protocol, agents, bootstrap, media
configuration, packaging, Compose and existing CI scripts. It also rejects
added files in protected deployment/agent/bootstrap directories. In particular,
`deploy/moblin-relay` and `bootstrap_worker/relay_installer.py` remain absent.
The frozen `main_ci.yml` is checked against its original blob and every original
job property and complete step must remain unchanged and in order. Additional
HUD checks do not replace main's RTMP/output/preview/bootstrap/resource gates.

One source-byte exception is explicit: the pinned formatter requires collapsing
one multiline expression in `relay_agent/preview.py`, plus two expressions in
its existing unit test. The original main code also fails that unchanged format
gate. The boundary retains the original preview blob in the manifest and freezes
its original source in `main_preview.py.txt`; both Python AST and non-layout
tokens (including comments) must match. This permits formatting only, without
weakening the gate or changing preview behavior/protocol. The backend Dockerfile
does not copy `relay_agent`, and this release does not deliver an agent bundle.

The failed native-startup diagnostic in the stacked release stages the PR15
normalizer, renderer and native self-test. Those sources, its test topology and
the native installer are not shipped here. The backend Dockerfile copies
`app` and `scripts`; it does not install a remote native media bundle. Exclusion
is therefore supported by file and dependency boundaries, not merely by removing
a workflow job. Historical media failures remain open; they are neither fixed
nor accepted by this release. If any excluded media implementation enters the
candidate or its deployment package, the corresponding failed case applies again.

## Reads, startup and persistence

The actual local Windows WebKit navigation probe sent the HUD cookie despite
the server's `SameSite=Strict` header. The release therefore also refuses HUD
cookie authentication when the browser reports `Sec-Fetch-Site: cross-site`.
Direct homepage entry and existing clients without fetch metadata retain the
existing checks. This is additional read-side protection, not a cookie-policy
relaxation. The beta browser regression requires the real cross-site navigation
to return 401, alongside the original strict cookie/header and origin tests.

HUD status performs SQLite reads, one existing MediaMTX status GET, in-memory
sampling and quality evaluation. The browser requests only status, pairing and
logout; its admin component creates/lists/revokes HUD grants. It cannot submit
START/STOP/CONFIGURE, onboarding, credential rotation, destination edits or
FFmpeg restarts. The integration regression installs failing actuator spies
after startup and compares all control/media tables before and after login,
panel reads, pairing, HUD reads, replay rejection and revoke. It also verifies
that the HUD cookie cannot authorize administrator APIs and survives restart.

Startup is **not** harmless for active broadcasting: pinned main's
`ApplicationRuntime.startup()` reconciles enabled destinations;
`ApplicationRuntime.shutdown()` calls `WorkerManager.shutdown()`, stopping
the backend-owned FFmpeg processes with bounded terminate/kill escalation.
Existing bootstrap startup cancels surviving installation jobs and marks
interrupted jobs failed. Existing maintenance reconciles command leases and
prunes old records. These behaviors are preserved, and require a planned window
without an active main stream, onboarding or pending control commands.

Main has migrations 1–5. HUD uses its existing identity **7**; migration **6**
belongs to the separate native relay classification and is not recorded as
performed. HUD adds only its device/pairing tables and indexes, preserving
existing identifiers, rows, sequences and foreign keys. Restart is idempotent.
The migration regression executes frozen main and future combined migration
sources on a synthetic database to verify main → HUD → combined, including
the migration-6 gap. This is evidence for those pinned sources, not an assertion
that every future PR15 revision is automatically compatible.

## Candidate verification

Run the existing locked project checks and the unchanged main CI, plus HUD
backend/frontend, migration and real HTTPS Chromium/WebKit checks. Focused local
regressions can be run with:

```sh
uv sync --locked --group browser
uv run --locked pytest tests/integration/test_hud_release_compatibility.py tests/unit/test_hud_release_boundary.py -q
```

The legacy regression serializes an actual main `RelaySnapshot` with omitted
bitrate, submits protocol-1 heartbeats, and advances synthetic fresh observations
through 180 seconds. It must never manufacture zero bitrate, a baseline, media
loss, or a switch/reconnect recommendation from missing telemetry. Other reused
HUD tests cover expiration, replay, revoke, origins/CSRF, bounded polling,
connection loss/recovery, digest-only storage and secret-safe payloads.

Browser tests and synthetic telemetry are not physical iPhone acceptance. Use
the local beta launcher documented in the candidate handoff; no public URL,
VPS or tunnel is created by this release. Physical Moblin verification requires
at most these five actions on an operator-approved reachable HTTPS test origin:

1. Sign into the test panel and choose `Подключить Moblin HUD`.
2. Open the one-time Moblin homepage import link and use Moblin's private Web browser.
3. Confirm source, `Отправка потока`, freshness and unavailable metrics are intelligible.
4. Observe normal → monitoring connection loss → recovery using the test scenario.
5. Revoke the device in the panel and verify the HUD requests pairing again.

## Deployment plan — commands are not executed by this change

Before separate deployment authorization, inventory the actual Compose project,
backend image/container, database and backup volumes, environment/secret sources,
reverse proxy route, MediaMTX ownership, active outputs, active installation jobs
and queued/leased/acknowledged node/relay commands. Verify a window with no active
main stream and no installation/control work, then freeze administrator changes
for the backup/update/rollback interval. Missing production inventory prevents
executing this plan, not building and testing the beta. Do not stop an unrelated
stream or cancel a pending command as an implicit part of inventory.

Only the backend image changes. Keep the running bootstrap, MediaMTX, remote
relay/agent services, proxy configuration, DNS, firewall and network settings.
The production Compose environment contains the actual domain; verify it rather
than substituting a guessed URL. The snippets below assume the verified existing
project is `adojapan-restream`, its approved checkout and unchanged protected
`.env` are the current directory, and the backend image was built from the tested
release SHA. Do not print the environment or expanded Compose configuration.

Prepare the backend image before the maintenance window. Substitute the full
tested SHA in the first command; the variable is an image identifier, not a secret.

```sh
set -eu
set +x
umask 077
RELEASE_SHA=REPLACE_WITH_TESTED_FULL_RELEASE_SHA
test "${#RELEASE_SHA}" -eq 40
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
git diff --quiet HEAD
CANDIDATE_IMAGE="adojapan-restream-backend:hud-${RELEASE_SHA}"
PREVIOUS_IMAGE="adojapan-restream-backend:before-hud-${RELEASE_SHA}"
docker build --file Dockerfile --tag "$CANDIDATE_IMAGE" .
ROLLOUT_RECORD="$(mktemp -d "$PWD/backups/hud-rollout.XXXXXXXX")"
BASE_COMPOSE="$PWD/compose.yml"
PROD_COMPOSE="$PWD/compose.production.yml"
BACKEND_ID="$(docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" ps -q backend)"
test -n "$BACKEND_ID"
docker tag "$(docker inspect --format '{{.Image}}' "$BACKEND_ID")" "$PREVIOUS_IMAGE"
printf 'services:\n  backend:\n    image: %s\n' "$PREVIOUS_IMAGE" > "$ROLLOUT_RECORD/previous.yml"
printf 'services:\n  backend:\n    image: %s\n' "$CANDIDATE_IMAGE" > "$ROLLOUT_RECORD/candidate.yml"
BACKUP_DIR="/srv/app/backups/hud-before-${RELEASE_SHA}"
```

After the authorized window starts, stop **only backend**, create a dedicated
backup directory inside the existing backup volume, and use SQLite's backup API
with the preserved old image. The dedicated directory makes the existing
backup script's retention independent of other backups. Neither helper starts
the application, FFmpeg or dependencies.

```sh
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" stop backend
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)' "$BACKUP_DIR"
BACKUP_FILE="$(docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend -c 'import os,runpy; os.umask(0o077); runpy.run_module("scripts.backup",run_name="__main__")' --output "$BACKUP_DIR" --retain 1)"
printf '%s\n' "$BACKUP_FILE" > "$ROLLOUT_RECORD/backup-path.txt"
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; assert not c.execute("PRAGMA foreign_key_check").fetchall()' "$BACKUP_FILE"
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/candidate.yml" up -d --no-deps --no-build --wait --wait-timeout 90 backend
```

Startup applies the additive HUD migration. Verify health/live and health/ready
through the existing local binding; verify migration identities `[1,2,3,4,5,7]`,
foreign keys, existing destinations/nodes/ingest identity and absence of newly
created control commands. In the panel, check existing status/preview, pair HUD,
check freshness/source/forwarding, revoke it, and confirm admin API isolation.
Inspect bounded redacted logs. Only an explicitly approved test stream should
be used for output verification; backend restart does not prove stream continuity.
Do not release the administrative freeze until deciding to accept or roll back.

Rollback if startup/migration/health fails, existing data or output/preview
regresses, pairing isolation fails, status reads create a control command, or
secrets appear in returned payloads/logs. Keep backend stopped and preserve the
candidate database separately before restoring the selected pre-update backup.
The `.env`, encryption/session keys, secret files and remote runtimes remain
unchanged. Do not use `scripts/rollback.sh`: it stops the entire Compose project.

```sh
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/candidate.yml" stop backend
FAILED_DB_DIR="/srv/app/backups/hud-failed-${RELEASE_SHA}"
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)' "$FAILED_DB_DIR"
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend -c 'import os,runpy; os.umask(0o077); runpy.run_module("scripts.backup",run_name="__main__")' --output "$FAILED_DB_DIR" --retain 1
BACKUP_FILE="$(cat "$ROLLOUT_RECORD/backup-path.txt")"
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" run --rm --no-deps --entrypoint python backend scripts/restore.py "$BACKUP_FILE" --confirm RESTORE_ADOJAPAN_RESTREAM
docker compose -p adojapan-restream -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" -f "$ROLLOUT_RECORD/previous.yml" up -d --no-deps --no-build --wait --wait-timeout 90 backend
```

Verify old health, existing panel/status/output/preview and the original schema
after restore. The backup restores the pre-release state; any subsequent changes
would be lost, which is why the freeze is required. Preserve the failed snapshot
for investigation, with its normal secret protections. A failed/corrupt migration
may require a raw volume snapshot by the verified storage procedure before the
SQLite backup helper can run; do not continue blindly after a backup failure.

Restoring the pre-HUD database removes HUD devices and grants. Old HUD cookies
then authorize nothing; the old backend does not expose the HUD routes. Users
must pair again after a later HUD rollout. Never restore the failed HUD database
as a convenience, since doing so can revive device grants or undo revocations.
For an image-only rollback that deliberately retains HUD tables, revocations
must be preserved; before a future re-enable, revoke old HUD devices through the
approved HUD admin workflow. Do not rotate shared administrator/encryption keys
to revoke a HUD device. No merge, deployment or production connection is
authorized by this document itself.
