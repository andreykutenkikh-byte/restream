# AdoJapan Restream

AdoJapan Restream is a small, self-hosted service that accepts one authenticated RTMP stream
from OBS and copies it to manually configured RTMP/RTMPS destinations. Stage 1 is optimized for
a modest host shared with other services: no transcoding, no recording, and no heavy frontend
toolchain.

Production domain: `restream.adojapan.ru`. Stage 1 prepares deployment assets but does not
change production.

## Architecture

```text
OBS -- RTMP /live/<ingest-key> --> MediaMTX
                                      |
                                      | authenticated internal RTMP read
                                      v
                         FastAPI process manager
                           |                |
                           v                v
                      FFmpeg copy      FFmpeg copy
                           |                |
                           v                v
                       destination 1    destination 2
```

- **MediaMTX 1.19.2** accepts RTMP and exposes its control API only on the private project
  network. Every publish/read request is authorized by the backend.
- **FastAPI** serves the Russian UI and JSON API, holds server-side sessions, queries MediaMTX,
  and owns FFmpeg child processes.
- **SQLite** persists the encrypted ingest configuration, encrypted destination keys,
  destination intent/state metadata, hashed sessions, schema version, and a bounded audit tail.
- **One FFmpeg supervisor per destination** uses an argument array with no shell and `-c copy`.
  A failed destination cannot stop another destination.
- **Server templates, CSS, and small JavaScript** require no client build step.

The SQLite record says which destinations should run. Actual worker state always comes from the
in-memory process manager; stale PIDs are never trusted after restart.

## Requirements

- Docker Engine with the Compose v2 plugin
- roughly 768 MiB RAM and 0.7 CPU available for the committed container limits
- an available local HTTP port (default `127.0.0.1:8088`)
- an available RTMP port (local default `127.0.0.1:1935`)

Final production limits and ports must be approved after the audit in
[`docs/production-audit.md`](docs/production-audit.md). Do not take resources from existing
services to satisfy these values.

## Local setup

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Create a local Python environment and install the project helpers:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -e .
   ```

3. Generate each value once. Keep the output out of shell history, chat, issue trackers, and
   Git:

   ```bash
   python -m app.cli generate-session-secret
   python -m app.cli generate-master-key
   python -m app.cli hash-password
   ```

4. Put the outputs into `.env`. Keep the Argon2id hash single-quoted so its dollar signs remain
   literal. For local HTTP, leave `COOKIE_SECURE=false`. Production requires HTTPS and
   `COOKIE_SECURE=true`.

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
- required session secret, Fernet master key, admin login, and Argon2id password hash;
- project SQLite path and internal MediaMTX API/RTMP URLs;
- destination limit and bounded reconnect timings;
- log level, trusted proxy addresses, session lifetime, and secure-cookie mode.

There are no fallback values for cryptographic secrets or admin credentials. Startup fails with
a clear configuration error when they are missing or malformed. Never replace the master key
without an explicit key-rotation/migration procedure: existing destination secrets would become
unreadable. `TRUSTED_PROXIES` accepts explicit IP/CIDR entries only; determine the production
Docker bridge source during the audit and never use a trust-all wildcard.

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

Destination states:

- `stopped`;
- `waiting_for_input` (enabled, but no incoming signal);
- `connecting`;
- `live`;
- `reconnecting` with bounded exponential delay;
- `failed` after incompatible media or too many quick failures.

Starting a destination while ingest is offline does not launch FFmpeg. The supervisor waits with
negligible CPU use and starts automatically when H.264 video with optional AAC audio appears.
When input disappears, the owned process receives graceful termination and returns to waiting.

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

## Isolation and resources

Compose creates project-scoped database, log, and backup volumes; an internal backend/MediaMTX
network; a dedicated MediaMTX ingest bridge for its published RTMP port; and a separate backend
egress network. MediaMTX's API is not published. The web port binds to loopback for the existing
reverse proxy, and local RTMP also binds to loopback until an audited production change explicitly
selects the external address. The MediaMTX auth hook accepts only direct private/loopback clients,
rejects forwarded requests, and the proxy example blocks `/internal/` outright.

Both containers drop Linux capabilities, prevent privilege escalation, use read-only root filesystems,
have PID/CPU/RAM limits, bounded JSON log rotation, healthchecks, finite failure restarts, and
graceful stop periods. No Docker socket or directories from other projects are mounted. Scripts
guard the repository root and fixed project name before acting.

Committed starting limits:

| Service | CPU | RAM | PIDs | Published ports |
| --- | ---: | ---: | ---: | --- |
| backend + up to 2 copy workers | 0.45 | 512 MiB | 96 | `127.0.0.1:8088 → 8000` |
| MediaMTX | 0.25 | 256 MiB | 64 | `127.0.0.1:1935 → 1935` |

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

Install development dependencies, then run:

```bash
python -m pip install -e '.[dev]'
ruff format --check .
ruff check .
mypy app
pytest
python scripts/check_repository.py
git diff --check
```

Validate the container definition with populated non-production test values:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml config --quiet
docker compose -p adojapan-restream --env-file .env -f compose.yml build
docker compose -p adojapan-restream --env-file .env -f compose.yml up -d
docker compose -p adojapan-restream --env-file .env -f compose.yml ps
docker compose -p adojapan-restream --env-file .env -f compose.yml stop
```

Tests cover cryptography, password verification, URL/SSRF rules, private and loopback addresses,
log redaction, codec compatibility, stream-key rotation, session/CSRF enforcement, destination
limits, MediaMTX status mapping, worker transitions/backoff/termination, and the complete API
smoke flow. Tests never call a real streaming platform or use real keys.

## Reverse proxy and production gate

`deploy/nginx-restream.conf.example` is an uninstalled example for the existing production
proxy. Do not install another global proxy. A future approved deployment must first identify the
current proxy, back up only the dedicated site file, validate it, and safely reload without
changing other domains.

- [Production audit and go/no-go checklist](docs/production-audit.md)
- [Controlled deployment and project-only rollback](docs/deployment-and-rollback.md)

If TCP 1935 is occupied, keep the owning service running, choose an approved alternative (for
example 1936), and update both `PUBLIC_RTMP_PORT` and the displayed documentation. Production
deployment stops if CPU, RAM, swap, disk, or port headroom is insufficient.

## Troubleshooting

**The backend exits immediately.** Read only the short error and check that all required secret
variables exist, the password hash is Argon2id, and the Fernet key was generated by the helper.
Do not paste `.env` into logs or support requests.

**OBS cannot publish.** Confirm OBS uses the displayed server and separate current key. Rotation
invalidates the old path immediately. Check that the configured public RTMP port matches the safe
host mapping.

**A destination stays in “Waiting for input”.** Start OBS first and confirm ingest is `live`.
FFmpeg is intentionally not launched without input.

**A destination fails as incompatible.** Stage 1 requires H.264 video and AAC or no audio. It
will not silently transcode, change resolution, or change FPS.

**A destination URL is rejected.** The hostname must resolve exclusively to public addresses.
Internal or local destinations are server configuration, never user input.

**Readiness is unavailable while liveness is healthy.** Check only this project's MediaMTX and
backend logs; do not restart the Docker daemon or unrelated services.

## Stage 1 limitations

- one administrator and one incoming stream;
- at most two destinations by default (configurable, capped at ten);
- stream copy only: no transcoding, resizing, FPS conversion, overlays, titles, or recording;
- no OAuth or automatic YouTube/VK/Twitch broadcast creation;
- no browser preview, quality ladder, or public registration;
- no production deployment in this stage.

## Stage 2

The next stage can add an authenticated HLS/WebRTC player, browser upload-speed measurement,
OBS bitrate/resolution/FPS recommendations, actual bitrate history, richer monitoring, and later
multiple users. Stage 1 deliberately does not simulate a speed test or expose MediaMTX preview
ports.
