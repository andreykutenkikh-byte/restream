# AdoJapan Restream

AdoJapan Restream is a small, self-hosted service that accepts one authenticated RTMP stream
from OBS and copies it to manually configured RTMP/RTMPS destinations. Stage 1 is optimized for
a modest host shared with other services: no transcoding, no recording, and no heavy frontend
toolchain.

Production domain: `restream.adojapan.ru`. Stage 1 prepares deployment assets but does not
change production.

## Branch and release workflow

`main` is the stable release branch. Changes are developed on feature branches, validated by a
draft pull request, reviewed, and merged only after approval. CI runs for every pull request and
again for pushes to `main`. A merge does not authorize production deployment: the audit and
production gate below remain a separate explicit change.

## Architecture

```text
OBS -- RTMP /live/<ingest-key> --> MediaMTX
               |                         |
               | internal HLS remux      | authenticated internal RTMP read
               v                         v
       authenticated FastAPI       FastAPI process manager
         same-origin proxy           |                |
               |                     v                v
               v                FFmpeg copy      FFmpeg copy
         browser preview              |                |
                                      v                v
                                  destination 1    destination 2
```

- **MediaMTX 1.19.2** accepts RTMP, remuxes the incoming H.264/AAC stream to HLS without
  transcoding, and exposes its control and HLS listeners only on the private project network.
  Every publish/read request is authorized by the backend.
- **FastAPI** serves the Russian UI and JSON API, holds server-side sessions, queries MediaMTX,
  proxies HLS to the authenticated same-origin browser, and owns FFmpeg child processes.
- **SQLite** persists the encrypted ingest configuration, encrypted destination keys,
  destination intent/state metadata, hashed sessions, schema version, and a bounded audit tail.
- **One FFmpeg supervisor per destination** uses an argument array with no shell and `-c copy`.
  A failed destination cannot stop another destination.
- **Server templates, CSS, small JavaScript, and local hls.js 1.6.16** require no runtime CDN
  or client build step. Safari uses native HLS when available.

The SQLite record says which destinations should run. Actual worker state always comes from the
in-memory process manager; stale PIDs are never trusted after restart.

## Requirements

- Docker Engine with the Compose v2 plugin
- roughly 768 MiB RAM, 0.70 CPU, and 160 PIDs available for the base local profile
- at least 576 MiB RAM, 0.60 CPU, and 160 PIDs available for the shared-host production profile
- an available local HTTP port (default `127.0.0.1:8088`)
- an available RTMP port (local default `127.0.0.1:1935`)

Production limits and ports must still pass the audit in
[`docs/production-audit.md`](docs/production-audit.md). Do not take resources from existing
services to satisfy these values.

## Local setup

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Install the pinned `uv` version and synchronize the locked production dependencies:

   ```bash
   python3 -m pip install 'uv==0.11.28'
   uv sync --locked --no-dev
   ```

3. Generate each value once. Keep the output out of shell history, chat, issue trackers, and
   Git:

   ```bash
   uv run --locked --no-dev python -m app.cli generate-session-secret
   uv run --locked --no-dev python -m app.cli generate-worker-auth-password
   uv run --locked --no-dev python -m app.cli generate-master-key
   uv run --locked --no-dev python -m app.cli hash-password
   ```

4. Put the outputs into `.env`. `SESSION_SECRET` and `WORKER_AUTH_PASSWORD` protect independent
   trust domains and must be different random values. Keep the Argon2id hash single-quoted so
   its dollar signs remain literal. For local HTTP, leave `COOKIE_SECURE=false`. Production
   requires HTTPS and `COOKIE_SECURE=true`.

5. Validate and start only this project:

   ```bash
   docker compose -p adojapan-restream -f compose.yml config --quiet
   docker compose -p adojapan-restream -f compose.yml up -d --build
   docker compose -p adojapan-restream -f compose.yml ps
   ```

6. Open `http://127.0.0.1:8088`, sign in, and copy the displayed server and key into OBS:

   - Service: **Custom**
   - Server: the displayed `rtmp://.../live` value
   - Stream key: the separate generated key

7. Follow service logs without printing environment values:

   ```bash
   docker compose -p adojapan-restream -f compose.yml logs --tail=100 backend mediamtx
   ```

Stop containers while preserving data:

```bash
docker compose -p adojapan-restream -f compose.yml stop
```

## Configuration

`.env.example` documents every supported variable:

- environment, public web domain, public RTMP host/port, and local bind addresses;
- required, independent session and MediaMTX worker secrets, Fernet master key, admin login,
  and Argon2id password hash;
- project SQLite path and internal MediaMTX API/RTMP/HLS URLs;
- destination limit and bounded reconnect timings;
- log level, trusted proxy addresses, session lifetime, and secure-cookie mode.

There are no fallback values for cryptographic secrets or admin credentials. Startup fails with
a clear configuration error when they are missing or malformed. `WORKER_AUTH_PASSWORD` must be
at least 32 characters; production also rejects it when it equals `SESSION_SECRET`. Never replace
the master key without an explicit key-rotation/migration procedure: existing destination secrets
would become unreadable. `TRUSTED_PROXIES` accepts explicit IP/CIDR entries only; determine the
production Docker bridge source during the audit and never use a trust-all wildcard.

## Login and sessions

Stage 1 has one administrator and no public registration or email recovery. Passwords are
verified against an Argon2id hash. The random session bearer is stored only in an `HttpOnly`,
`SameSite=Lax` cookie; SQLite stores a keyed digest, not the bearer. A separate same-origin CSRF
token protects every mutating operation. Production marks both cookies `Secure`.

Repeated failed login attempts receive a small in-memory rate limit. Logout deletes the
server-side session and both browser cookies.

## Ingest and destination states

Incoming signal states:

- `offline` — signal is absent;
- `connecting` — MediaMTX is establishing the publisher;
- `live` — stream is available;
- `unstable` — stream exists but reports instability;
- `error` — status cannot be determined safely.

The authenticated incoming-signal card can play that exact stream before any destination is
started. MediaMTX performs an on-demand HLS remux only: no preview FFmpeg process, transcoding,
recording, or quality ladder exists. Browser requests use only
`/api/ingest/preview/index.m3u8` and same-origin media assets. The backend supplies internal reader
credentials, retains MediaMTX's HLS session server-side, and never returns the ingest key, worker
password, Docker address, or upstream session cookie. The local hls.js 1.6.16 handles
Chromium/Firefox; Safari uses native HLS. Preview audio starts muted.

The status API derives incoming bitrate from monotonic growth of MediaMTX's cumulative received
byte counter. The first sample is unknown; subsequent samples use a bounded smoothed rolling rate.
The sampler resets on offline, key/session changes, counter rollback, or stale samples and stores no
history in SQLite. Resolution and codecs come from MediaMTX when available; FPS remains unknown
unless a reliable source provides it.

Destination states:

- `stopped`;
- `waiting_for_input` (enabled, but no incoming signal);
- `connecting` (FFmpeg is running, but outgoing media has not yet been confirmed);
- `live` only after machine-readable FFmpeg progress proves positive outgoing media time or
  sustained output-byte growth;
- `reconnecting` with bounded exponential delay;
- `failed` after incompatible media or too many quick failures.

Starting a destination while ingest is offline does not launch FFmpeg. The supervisor waits with
negligible CPU use and starts automatically when H.264 video with optional AAC audio appears.
When input disappears, the owned process receives graceful termination and returns to waiting.
FFmpeg start and progress are each bounded by a 15-second timeout. A process that merely stays
alive, opens a TCP socket, or stops reporting outgoing progress never becomes or remains `live`;
it enters the existing reconnect/backoff path. Process start time and confirmed transmission start
time are tracked separately, and fast-failure counters reset only after confirmed stable transfer.

## Destination safety

Only `rtmp://` and `rtmps://` URLs are accepted. Validation rejects whitespace, control and shell
characters, embedded credentials, unknown/ambiguous hosts, excessive values, local names, and
non-public IP ranges. DNS is resolved during save and again immediately before FFmpeg launch to
reduce DNS-rebinding risk. Users can never supply FFmpeg arguments.

Destination keys and the ingest key are authenticated-encrypted with the configured Fernet key.
Destination keys are never returned by the API or rendered in HTML after saving. FFmpeg uses an
argument array with shell execution disabled. Central redaction removes credentials, sensitive
query parameters, bearer values, and known stream keys from the bounded diagnostic tail.
MediaMTX stays at error-only logging because its authenticated path contains the ingest key;
ordinary publish/read connection lines are therefore never written to Docker logs.

Private and loopback destinations remain rejected in every production path. The CI-only
`TEST_DESTINATION_ALLOWLIST` is an exact URL match accepted only with `ENVIRONMENT=test`; setting
it in development or production fails startup. The receiver exists only in `compose.ci.yml` and
is absent from the production Compose definition.

## Isolation and resources

Compose creates project-scoped database, log, and backup volumes; an internal backend/MediaMTX
network; a dedicated MediaMTX ingest bridge for its published RTMP port; and a separate backend
egress network. MediaMTX's API and HLS port `8888` are not published. The backend reaches HLS only
through the internal network. The web port binds to loopback for the existing reverse proxy, and
local RTMP also binds to loopback until an audited production change explicitly selects the
external address. The MediaMTX auth hook accepts only direct private/loopback clients, rejects
forwarded requests, and the proxy example blocks `/internal/` outright.

Both containers drop Linux capabilities, prevent privilege escalation, use read-only root filesystems,
have PID/CPU/RAM limits, bounded JSON log rotation, healthchecks, and graceful stop periods. No
Docker socket or directories from other projects are mounted. Scripts guard the repository root
and fixed project name before acting.

Restart behavior is profile-specific for both `backend` and `mediamtx`:

| Effective profile | Restart policy | Behavior |
| --- | --- | --- |
| base `compose.yml` | `on-failure:5` | bounded retries after a non-zero process exit |
| base + `compose.production.yml` | `unless-stopped` | recovery after a process, Docker daemon, or host restart unless an operator explicitly stopped the container |
| base + production + `compose.ci.yml` | `no` | deterministic CI failures with no automatic retry |

An intentional `docker compose -p adojapan-restream stop` therefore remains effective across a Docker daemon restart in
production. Conversely, a persistently failing process can keep retrying under `unless-stopped`;
production monitoring must alert on sustained readiness failure, restart-count growth, and OOM state
so an operator can diagnose the project instead of allowing an unnoticed restart loop.

Committed starting limits:

| Service | CPU | RAM | PIDs | Published ports |
| --- | ---: | ---: | ---: | --- |
| backend + up to 2 copy workers | 0.45 | 512 MiB | 96 | `127.0.0.1:8088 → 8000` |
| MediaMTX | 0.25 | 256 MiB | 64 | `127.0.0.1:1935 → 1935` |

The prepared shared-host production profile is the fail-closed override
`compose.production.yml`. It must always be loaded after `compose.yml`; it tightens resource and
destination limits without replacing the base security, networks, volumes, healthchecks, or log
policy. It also fixes the following effective values, regardless of development defaults in the
production `.env`:

- `ENVIRONMENT=production`
- `COOKIE_SECURE=true`
- `MAX_DESTINATIONS=1`
- `PUBLIC_DOMAIN=restream.adojapan.ru`
- `PUBLIC_RTMP_HOST=restream.adojapan.ru`
- `PUBLIC_RTMP_PORT=1935`

This project has one defined public identity, so fixing it in the override removes both the
`localhost` fallback and operator drift. Production `.env` values cannot disable secure cookies
or switch the profile back to development. `SESSION_SECRET` and `WORKER_AUTH_PASSWORD` remain
separate required secrets and must contain independent values.

| Service | CPU | RAM | PIDs | Production setting |
| --- | ---: | ---: | ---: | --- |
| backend + one copy worker | 0.40 | 384 MiB | 96 | `MAX_DESTINATIONS=1` |
| MediaMTX | 0.20 | 192 MiB | 64 | no additional published port |
| **Total** | **0.60** | **576 MiB** | **160** | one destination |

The public RTMP identity is `restream.adojapan.ru:1935`; it is distinct from the host-side bind.
The planned server is `147.45.231.225`. The base Compose file hard-codes the HTTP host address to
loopback, so HTTP stays on `127.0.0.1:8088` for the existing reverse proxy and cannot be opened by
an environment override. The RTMP bind address remains controlled by the separately approved
`RTMP_BIND_ADDRESS`; the reviewed host mapping is `147.45.231.225:1935`. These values describe a
future deployment and do not authorize one.

The backend's internet-capable network is required only for user-configured destinations.
MediaMTX shares only the private control network with the backend and joins a separate ingress
bridge solely so Docker can publish its single RTMP port.

## Health and diagnostics

- `GET /health/live` verifies the web process.
- `GET /health/ready` verifies the schema and MediaMTX control connection.
- `GET /api/system/diagnostics` is authenticated and returns only safe states, counts, and a
  redacted bounded audit tail. It never depends on YouTube or another destination.

Compose liveness does not call an external platform. Docker log files rotate at 10 MiB with three
files per container.

Production monitoring should treat consecutive non-200 responses from `/health/ready` as the
dependency-availability signal and correlate them with this project's container state, health,
restart-count changes, and OOM state. Do not alert on a raw count of MediaMTX `ERR` lines: an expected
offline or absent ingest path can use that severity. Classify fixed message categories on the server
and export only category, count, and timestamps; never export or quote an authenticated path, stream
key, destination URL, cookie, or credential.

## Backup and restore

Stage 1 does not schedule production backups. To create a consistent, bounded project-only
SQLite backup from the running backend:

```bash
docker compose -p adojapan-restream -f compose.yml exec backend \
  python scripts/backup.py --retain 14
```

Restore requires a selected `adojapan-restream-*.db` file, a stopped backend, and the explicit
confirmation phrase documented in
[`docs/deployment-and-rollback.md`](docs/deployment-and-rollback.md). Ordinary rollback preserves
all volumes.

## Tests and repository checks

Install development dependencies from the committed lock, verify that it still matches
`pyproject.toml`, then run the checks through the locked environment:

```bash
python -m pip install 'uv==0.11.28'
uv sync --locked
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy app scripts
uv run --locked pytest
uv run --locked python scripts/check_repository.py
node --check app/static/app.js
node --check app/static/preview-player.js
node --test tests/frontend/preview-player.test.js
git diff --check
```

`uv.lock` pins the complete production and development dependency graph. Change direct
dependencies only in `pyproject.toml`, regenerate the lock with `uv lock`, and commit both files.
CI rejects a stale lock before running the rest of the checks. Docker installs its production
subset from this same lock with `uv sync --locked --no-dev --no-editable`.
The Python and MediaMTX base images use exact version tags. Digest pinning is intentionally
deferred until the production audit records the target architecture and the reviewed security
update procedure; changing a digest will then be an explicit dependency update.

Validate the container definition with populated non-production test values:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml config --quiet
docker compose -p adojapan-restream --env-file .env -f compose.yml build
docker compose -p adojapan-restream --env-file .env -f compose.yml up -d
docker compose -p adojapan-restream --env-file .env -f compose.yml ps
docker compose -p adojapan-restream --env-file .env -f compose.yml stop
```

For a separately approved production change, every Compose lifecycle command must load the
shared-host override after the base file. The validation command is safe to run before that
window; the build, start, status, logs, stop, and rollback commands are shown here for exactness,
not as authorization to execute them:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --quiet
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --format json \
  | python3 scripts/validate_production_compose.py
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml build
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml up -d
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml logs --tail=100 backend mediamtx
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml stop
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml down --remove-orphans
```

Tests cover cryptography, password verification, URL/SSRF rules, private and loopback addresses,
log redaction, codec compatibility, stream-key rotation, session/CSRF enforcement, destination
limits, MediaMTX status mapping, worker transitions/backoff/termination, and the complete API
smoke flow. Tests never call a real streaming platform or use real keys.

The GitHub Actions runtime smoke must use the same file order for configuration, build, startup,
logs, and cleanup: `compose.yml`, then `compose.production.yml`, then `compose.ci.yml`. Loaded last,
the CI-only override switches the synthetic runtime to `ENVIRONMENT=test` and
`COOKIE_SECURE=false`, adds the exact test destination allowlist plus isolated receiver and
publisher helpers, and is never part of a production lifecycle command.

A successful run for the reviewed commit is required to establish evidence that this effective
model enforces the shared-host limits. It must inspect only `NanoCpus`, `Memory`, `PidsLimit`,
`RestartPolicy.Name`, status, and health, confirming restart policy `no`, backend limits of 0.40
CPU, 384 MiB, and 96 PIDs, and MediaMTX limits of 0.20 CPU, 192 MiB, and 64 PIDs. A separately
constrained, internal-only CI publisher generates
synthetic H.264/AAC so encoder work cannot contaminate backend measurements. Through the public API
the run rotates the key once while ingest is offline and again with an active publisher, confirms
the active publisher is kicked, rejects the previous key, accepts the replacement key, then confirms
received-byte growth and calculated bitrate, rejects unauthenticated preview,
fetches an authenticated HLS playlist and media segment, verifies port `8888` has no host binding,
keeps authenticated preview requests active, and prints sanitized backend/MediaMTX CPU and RAM
usage. It then creates and starts the first destination, rejects a second with `409
destination_limit_reached` while the first remains active, and completes the real stream-copy
worker, packet/byte assertions, shutdown, offline bitrate reset, destination deletion, and cleanup
with the same three runtime files. No environment, credential, stream key, or internal HLS session
may be printed. This CI evidence does not authorize a deployment.

## Reverse proxy and production gate

`deploy/nginx-restream.conf.example` is an uninstalled example for the existing production
proxy. Do not install another global proxy. A future approved deployment must first identify the
current proxy, back up only the dedicated site file, validate it, and safely reload without
changing other domains.

The Phase 2 preparation audit did not deploy Restream or change DNS, host/provider firewall
rules, Nginx, or any existing service. DNS and firewall remain explicit no-go gates documented
in the production audit.

- [Production audit and go/no-go checklist](docs/production-audit.md)
- [Controlled deployment and project-only rollback](docs/deployment-and-rollback.md)

If TCP 1935 is occupied, keep the owning service running, choose an approved alternative (for
example 1936), and update both `PUBLIC_RTMP_PORT` and the displayed documentation. Production
deployment stops if CPU, RAM, swap, disk, or port headroom is insufficient.

## Troubleshooting

**The backend exits immediately.** Read only the short error and check that all required secret
variables exist, `SESSION_SECRET` and `WORKER_AUTH_PASSWORD` are independent, the password hash
is Argon2id, and the Fernet key was generated by the helper. Do not paste `.env` into logs or
support requests.

**OBS cannot publish.** Confirm OBS uses the displayed server and separate current key. After a
recent successful publish authorization, rotation can take about 11 seconds while the backend
drains MediaMTX's bounded HTTP-auth horizon and verifies the old path is quiet twice; do not retry
the action while it is pending. A successful response means the previous key has been revoked.
Check that the configured public RTMP port matches the safe host mapping.

**A destination stays in “Waiting for input”.** Start OBS first and confirm ingest is `live`.
FFmpeg is intentionally not launched without input.

**The incoming preview is loading.** Confirm ingest is `live` and H.264/AAC-compatible. Check only
this project's backend and MediaMTX logs. Do not publish port `8888`; the backend reaches it through
the internal Compose network and the browser must always use the authenticated same-origin API.

**A destination fails as incompatible.** Stage 1 requires H.264 video and AAC or no audio. It
will not silently transcode, change resolution, or change FPS.

**A destination URL is rejected.** The hostname must resolve exclusively to public addresses.
Internal or local destinations are server configuration, never user input.

**Readiness is unavailable while liveness is healthy.** Check only this project's MediaMTX and
backend state, health, restart count, OOM state, and secret-safe log categories; do not restart the
Docker daemon or unrelated services. A raw MediaMTX `ERR` count is not a health verdict, and
authenticated paths, stream keys, and URLs must never be copied into an alert or support report.

## Stage 1 limitations

- one administrator and one incoming stream;
- at most two destinations by default (configurable, capped at ten);
- stream copy only: no transcoding, resizing, FPS conversion, overlays, titles, or recording;
- no OAuth or automatic YouTube/VK/Twitch broadcast creation;
- one source-quality authenticated preview, with no quality ladder or public registration;
- no persisted bitrate history or synthetic browser speed test;
- no production deployment in this stage.

## Stage 2

The next stage can add browser upload-speed measurement, OBS bitrate/resolution/FPS
recommendations, actual bitrate history, richer monitoring, and later multiple users. Stage 1
deliberately does not simulate a speed test or expose MediaMTX preview ports.
