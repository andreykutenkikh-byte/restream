# AdoJapan Restream

AdoJapan Restream is a small, self-hosted service that accepts one authenticated RTMP stream
from OBS and copies it to manually configured RTMP/RTMPS destinations. Its media path is optimized
for a modest host shared with other services: no transcoding, no recording, and no heavy frontend
toolchain. Stage 4A adds password-based SSH onboarding. The public **Servers** workflow now
provisions a complete native Moblin relay on each supported fresh VPS; the older Docker Node Agent
profile is retained only as an internal compatibility path. Every native relay is controlled
through an outbound-only agent and the same simplified authenticated broadcast console, without
moving the relay into Docker or exposing a management port. The main flow contains only YouTube
setup and a one-time Moblin SRT URL reveal; infrastructure controls remain in the additional
section.

Production domain: `restream.adojapan.ru`. Repository changes do not authorize deployment or
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

administrator --> FastAPI -- authenticated UDS --> isolated bootstrap worker
                                                    |
                                                    +-- bounded SSH --> remote server

remote Node Agent -- outbound HTTPS protocol v1 --> FastAPI node API

Relay Agent(s) -- outbound HTTPS protocol v1 --> FastAPI relay API
       |                                                ^
       +-- authenticated root UDS broker                |
                    |                                   |
                    +-- existing relayctl        administrator UI
```

- **MediaMTX 1.19.2** accepts RTMP, remuxes the incoming H.264/AAC stream to HLS without
  transcoding, and exposes its control and HLS listeners only on the private project network.
  Every publish/read request is authorized by the backend.
- **FastAPI** serves the Russian UI and JSON API, holds server-side sessions, queries MediaMTX,
  proxies HLS to the authenticated same-origin browser, owns FFmpeg child processes, and exposes
  the authenticated node control API.
- **Bootstrap worker** accepts one fixed installation job at a time over an authenticated
  Unix-domain socket. The public profile installs a native, pinned MediaMTX/Moblin relay bundle;
  the legacy internal profile installs the Docker Node Agent. The worker has its own egress network
  and no database, media network, Docker socket, or inbound port.
- **Node Agent** runs on an attached server as fixed UID/GID `10001:10001`, publishes no ports,
  enrolls once, sends five-second heartbeats, and accepts only `PING` and `SELF_TEST`.
- **Relay Agent** is a native, unprivileged service with an allowlisted root Unix-socket broker.
  It connects outward over HTTPS and exposes only safe status plus `start`, `stop`, YouTube
  configuration/clear, one-time Moblin SRT URL reveal, bounded LIVE input bitrate, and an on-demand
  loopback-HLS preview forwarded to the authenticated browser through a memory-only cache. It does
  not alter Amnezia, Docker, firewall, routes, interfaces, MediaMTX installation, or the existing
  relay secret store.
- **SQLite** persists the encrypted ingest configuration, encrypted destination keys,
  destination intent/state metadata, hashed sessions, digests of node credentials, safe node/job
  and relay state, schema version, and bounded audit/event tails. Relay command payloads are
  encrypted only while delivery is pending and are cryptographically erased on terminal state;
  an SRT result is encrypted, consumed once, and deleted. Raw node tokens and SSH passwords are
  not stored.
- **One FFmpeg supervisor per destination** uses an argument array with no shell and `-c copy`.
  A failed destination cannot stop another destination.
- **Server templates, CSS, small JavaScript, and local hls.js 1.6.16** require no runtime CDN
  or client build step. Safari uses native HLS when available.

The SQLite record says which destinations should run. Actual worker state always comes from the
in-memory process manager; stale PIDs are never trusted after restart.

## Requirements

- Docker Engine with the Compose v2 plugin
- roughly 896 MiB RAM, 0.80 CPU, and 224 PIDs available for the base local profile
- at least 704 MiB RAM, 0.70 CPU, and 224 PIDs available for the shared-host production profile
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
   uv run --locked --no-dev python -m app.cli generate-bootstrap-worker-secret
   uv run --locked --no-dev python -m app.cli generate-master-key
   uv run --locked --no-dev python -m app.cli hash-password
   ```

4. Put the outputs into `.env`. `SESSION_SECRET`, `WORKER_AUTH_PASSWORD`, and
   `BOOTSTRAP_WORKER_SECRET` protect independent trust domains and must be three different random
   values. Write the exact bootstrap secret, without a trailing diagnostic line, to the mode-`0600`
   file named by `BOOTSTRAP_WORKER_SECRET_FILE`. On a Linux host, set its owner to the bootstrap
   worker's fixed UID/GID `10001:10001`; the non-root worker receives that file read-only because its
   root filesystem is read-only. Both copies must come from the same generated value. Keep the
   Argon2id hash single-quoted so its dollar signs remain literal. For local HTTP, leave
   `COOKIE_SECURE=false`. Production requires HTTPS and `COOKIE_SECURE=true`.

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
   docker compose -p adojapan-restream -f compose.yml logs --tail=100 backend bootstrap mediamtx
   ```

Stop containers while preserving data:

```bash
docker compose -p adojapan-restream -f compose.yml stop
```

## Configuration

`.env.example` documents every supported variable:

- environment, public web domain, public RTMP host/port, and local bind addresses;
- required, independent session, MediaMTX worker, and bootstrap-worker secrets, the ignored
  bootstrap-worker secret-file path, Fernet master key, admin login, and Argon2id password hash;
- project SQLite path and internal MediaMTX API/RTMP/HLS URLs;
- the bootstrap UDS path, node protocol version, public control origin, and Node Agent image;
- destination limit and bounded reconnect timings;
- log level, trusted proxy addresses, session lifetime, and secure-cookie mode.

There are no fallback values for cryptographic secrets or admin credentials. Startup fails with
a clear configuration error when they are missing or malformed. `WORKER_AUTH_PASSWORD` and
`BOOTSTRAP_WORKER_SECRET` must each be at least 32 characters; production requires all three trust
secrets to differ. Production also requires `NODE_AGENT_IMAGE` to be an immutable registry
reference pinned by SHA-256 digest. Never replace the master key without an explicit
key-rotation/migration procedure: existing destination secrets would become unreadable.
`TRUSTED_PROXIES` accepts explicit IP/CIDR entries only; determine the production Docker bridge
source during the audit and never use a trust-all wildcard.

## Login and sessions

Stage 1 has one administrator and no public registration or email recovery. Passwords are
verified against an Argon2id hash. The random session bearer is stored only in an `HttpOnly`,
`SameSite=Lax` cookie; SQLite stores a keyed digest, not the bearer. A separate same-origin CSRF
token protects every mutating operation. Production marks both cookies `Secure`.

Repeated failed login attempts receive a small in-memory rate limit. Logout deletes the
server-side session and both browser cookies.

## Server onboarding

The authenticated **Servers** page now provisions a complete native Moblin Relay on a supported
fresh Ubuntu/Debian VPS. The administrator supplies only SSH connection data; the installer pins
MediaMTX, generates SRT/preview secrets, installs the vertical 1080×1920 runtime and outbound relay
agent, and leaves the broadcast service inactive and disabled. It never changes Docker, Amnezia,
firewalls, routes, or network interfaces. See
[`docs/moblin-relay-onboarding.md`](docs/moblin-relay-onboarding.md) for the current operator flow
and safety boundary.

The legacy Stage 4A generic Node Agent bootstrap remains as an internal compatibility profile.

Like the public relay workflow, its bounded request contains only a public server address, SSH
port, username, password, and optional expected SHA-256 host-key fingerprint. There is no OS,
package-manager, or Docker-install selector: the worker detects those properties from the server.
The backend sends the job over an authenticated Unix-domain socket to the isolated bootstrap
worker. The password exists only for that in-memory job: it is not stored in SQLite, rendered back
to the browser, placed in environment variables or arguments, or included in logs. A worker or
backend-coordinator restart requires password re-entry.

Bootstrap supports Ubuntu 22.04/24.04/26.04, Debian 12/13, AlmaLinux 8/9, Rocky Linux 8/9,
RHEL 8/9, and CentOS Stream 9 on amd64. It reads `/etc/os-release`, verifies the matching
apt/dpkg or dnf/rpm capabilities, checks public-target/SSRF and DNS-rebinding policy, verifies or
TOFU-pins the SSH host key, verifies resources and sudo, and installs only the marker-owned
`adojapan-restream-node` project. Unknown distributions, unsupported releases, package-manager
mismatches, and non-amd64 hosts fail closed. The Node Agent then uses a
single-use enrollment file to obtain a permanent file-only token atomically and connects outward
to protocol v1. Revocation stops future control-plane authentication but deliberately does not SSH
back to the server or uninstall it.

The bootstrap installer never invokes host firewall tools, edits existing firewall rules, or
writes Docker daemon/firewall configuration. A supported Docker installation is inspected without
reconfiguration or daemon restart. If Docker is absent, installing and starting Docker Engine and
creating the project-scoped bridge may create Docker-managed `iptables`/`nftables` rules for
bridge networking, NAT, and isolation. Those standard Docker-managed rules are an expected system
effect and are distinct from direct firewall management by AdoJapan. The Node Agent publishes no
host ports and never uses host networking. On SELinux hosts, SELinux remains enabled; the single
agent data bind uses Compose private relabel `Z` with `create_host_path: false` and no host policy
or manual relabel command.

- [Moblin Relay onboarding and operator flow](docs/moblin-relay-onboarding.md)
- [Legacy generic Node Agent onboarding](docs/node-onboarding.md)
- [Node Agent protocol v1](docs/node-agent-protocol.md)
- [Bootstrap security boundaries](docs/node-bootstrap-security.md)
- [Disaster recovery boundary](docs/disaster-recovery.md)

Legacy Stage 4A generic Docker nodes remain control-plane groundwork only: they do not receive real
video, publish to YouTube, or hot-switch streams. Existing and newly installed native Moblin relays
share the protocol in [`docs/native-relay-control.md`](docs/native-relay-control.md). SSH-key
onboarding and generic-node remote uninstall remain future work.

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

Compose creates project-scoped database, log, backup, and bootstrap-socket volumes; an internal
backend/MediaMTX network; a dedicated MediaMTX ingest bridge for its published RTMP port; a backend
egress network; and a separate bootstrap-only egress network. The backend mounts the bootstrap UDS
volume read-only, while the worker mounts only that volume read-write. MediaMTX's API and HLS port
`8888` are not published. The backend reaches HLS only through the internal network. The web port
binds to loopback for the existing reverse proxy, and local RTMP also binds to loopback until an
audited production change explicitly selects the external address. The MediaMTX auth hook accepts
only direct private/loopback clients, rejects forwarded requests, and the proxy example blocks
`/internal/` outright.

All three local containers drop Linux capabilities, prevent privilege escalation, use read-only
root filesystems, have PID/CPU/RAM limits, bounded JSON log rotation, healthchecks, and graceful
stop periods. Development keeps bounded `on-failure:5` restarts, production overrides all three
core services to `unless-stopped`, and CI explicitly disables restarts. The bootstrap worker gets
a 90-second shutdown grace so its bounded SSH cleanup can finish. It has no published port,
database/media network, or application storage. No Docker socket or directories from other
projects are mounted. Scripts guard the repository root and fixed project name before acting.

Committed starting limits:

| Service | CPU | RAM | PIDs | Published ports |
| --- | ---: | ---: | ---: | --- |
| backend + up to 2 copy workers | 0.45 | 512 MiB | 96 | `127.0.0.1:8088 → 8000` |
| bootstrap worker | 0.10 | 128 MiB | 64 | none (UDS only) |
| MediaMTX | 0.25 | 256 MiB | 64 | `127.0.0.1:1935 → 1935` |
| **Total** | **0.80** | **896 MiB** | **224** | HTTP/RTMP only |

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
or switch the profile back to development. `SESSION_SECRET`, `WORKER_AUTH_PASSWORD`, and
`BOOTSTRAP_WORKER_SECRET` remain separate required secrets and must contain independent values.
The backend receives its value from `.env`; the read-only bootstrap worker receives the exact same
value through the mode-`0600` file selected by `BOOTSTRAP_WORKER_SECRET_FILE`. On Linux this source
file is owned by UID/GID `10001:10001`, matching the worker's fixed non-root identity. The file is
excluded from Git and Docker build contexts.
`PUBLIC_CONTROL_URL` is fixed to `https://restream.adojapan.ru`, `NODE_PROTOCOL_VERSION` is fixed
to `1`, and `NODE_AGENT_IMAGE` must be supplied as an immutable SHA-256 digest reference.

| Service | CPU | RAM | PIDs | Production setting |
| --- | ---: | ---: | ---: | --- |
| backend + one copy worker | 0.40 | 384 MiB | 96 | `MAX_DESTINATIONS=1` |
| bootstrap worker | 0.10 | 128 MiB | 64 | UDS only, one active job |
| MediaMTX | 0.20 | 192 MiB | 64 | no additional published port |
| **Total** | **0.70** | **704 MiB** | **224** | one destination |

The public RTMP identity is `restream.adojapan.ru:1935`; it is distinct from the host-side bind.
The planned server is `147.45.231.225`. The base Compose file hard-codes the HTTP host address to
loopback, so HTTP stays on `127.0.0.1:8088` for the existing reverse proxy and cannot be opened by
an environment override. The RTMP bind address remains controlled by the separately approved
`RTMP_BIND_ADDRESS`; the reviewed host mapping is `147.45.231.225:1935`. These values describe a
future deployment and do not authorize one.

The backend's internet-capable network is required only for user-configured destinations; remote
nodes initiate their HTTPS requests through the public reverse proxy. The bootstrap worker uses a
different egress network for its bounded SSH workflow. MediaMTX shares only the private control
network with the backend and joins a separate ingress bridge solely so Docker can publish its
single RTMP port.

## Health and diagnostics

- `GET /health/live` verifies the web process.
- `GET /health/ready` verifies the schema and MediaMTX control connection.
- `GET /api/system/diagnostics` is authenticated and returns only safe states, counts, and a
  redacted bounded audit tail. It never depends on YouTube or another destination.

Compose liveness does not call an external platform. Docker log files rotate at 10 MiB with three
files per container.

## Backup and restore

**This is an operational rollback backup, not disaster recovery.** The command below writes to a
Compose volume on the same host. Loss of that VPS or its storage can destroy both the live database
and every one of these copies. Never commit `.env`, SQLite backups, relay credentials, or encrypted
command payloads to this public repository. A recoverable production installation requires a
separately configured encrypted off-server backup; see
[`docs/disaster-recovery.md`](docs/disaster-recovery.md).

The repository includes a fail-closed `age` + private-Git publication command for that separate
off-server copy. It is deliberately not enabled by default: the recovery-key custodian and private
repository must be chosen first. The private recovery identity never belongs on the VPS, and this
public repository rejects `.age` backup artifacts as well as plaintext runtime data.

The application does not schedule production backups. To create a consistent, bounded project-only
SQLite backup from the running backend and its named database volume:

```bash
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml exec -T backend \
  python scripts/backup.py --retain 14
```

Restore requires a selected `adojapan-restream-*.db` file, a stopped backend, and the explicit
confirmation phrase documented in
[`docs/deployment-and-rollback.md`](docs/deployment-and-rollback.md). Ordinary rollback preserves
all volumes. Production database and backup storage are named Compose volumes; run restore through
a one-off Compose container as documented instead of targeting the checkout's `data/` or
`backups/` directories.

## Tests and repository checks

Install development dependencies from the committed lock, verify that it still matches
`pyproject.toml`, then run the checks through the locked environment:

```bash
python -m pip install 'uv==0.11.28'
uv sync --locked
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy app bootstrap_worker node_agent relay_agent scripts
uv run --locked pytest
uv run --locked python scripts/check_repository.py
node --check app/static/app.js
node --check app/static/preview-player.js
node --check app/static/relay-dashboard.js
node --check app/static/servers.js
node --test tests/frontend/preview-player.test.js
node --test tests/frontend/relay-dashboard.test.js
node --test tests/frontend/servers.test.js
git diff --check
```

`uv.lock` pins the complete production and development dependency graph. Change direct
dependencies only in `pyproject.toml`, regenerate the lock with `uv lock`, and commit both files.
CI rejects a stale lock before running the rest of the checks. Docker installs its production
subset from this same lock with `uv sync --locked --no-dev --no-editable`.
The Python and MediaMTX base images use exact version tags. Independently, the remotely deployed
Node Agent image is required to use a registry digest in production. Resolve and record that digest
for the reviewed amd64 release; changing it is an explicit dependency/deployment update.

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
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml build
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml up -d
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml ps
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml logs --tail=100 backend bootstrap mediamtx
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml stop
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml down --remove-orphans
```

Tests cover cryptography, password verification, URL/SSRF rules, private and loopback addresses,
log redaction, codec compatibility, stream-key rotation, session/CSRF enforcement, destination
limits, MediaMTX status mapping, copy-worker transitions/backoff/termination, bootstrap target and
host-key policy, sudo/timeout/rollback behavior, node credential promotion, heartbeat metrics, and
idempotent fixed commands. Tests never call a real streaming platform or use real keys.

The GitHub Actions runtime smoke must use the same file order for configuration, build, startup,
logs, and cleanup: `compose.yml`, then `compose.production.yml`, then `compose.ci.yml`. Loaded last,
the CI-only override switches the synthetic runtime to `ENVIRONMENT=test` and
`COOKIE_SECURE=false`, adds the exact test destination and SSH allowlists plus isolated receiver,
publisher, `ci-ssh-target`, and `ci-node-agent` helpers, and is never part of a production lifecycle
command. The SSH target is internal to the bootstrap egress network, the agent shares only its CI
node-data volume, neither publishes a host port, and both are absent from production.

A successful run for the reviewed commit is required to establish evidence that this effective
model enforces the shared-host limits. It must inspect only `NanoCpus`, `Memory`, `PidsLimit`,
`RestartPolicy.Name`, status, health, restart count, and OOM state, confirming CI restart policy
`no`, backend limits of 0.40 CPU, 384 MiB, and 96 PIDs, MediaMTX limits of 0.20 CPU, 192 MiB, and
64 PIDs, plus bootstrap limits of 0.10 CPU, 128 MiB, and 64 PIDs. The production service aggregate
is therefore 0.70 CPU, 704 MiB, and 224 PIDs. A separately
constrained, internal-only CI publisher generates
synthetic H.264/AAC so encoder work cannot contaminate backend measurements. Through the public API
the run rotates the ingest key once while offline and once with an active publisher, waits through
the bounded MediaMTX authorization horizon, confirms the active publisher is terminated, rejects
the previous key, accepts the replacement key, confirms received-byte growth and calculated bitrate,
rejects unauthenticated preview,
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
- [Moblin Relay onboarding](docs/moblin-relay-onboarding.md)
- [Legacy Node Agent onboarding and remote rollback](docs/node-onboarding.md)
- [Node Agent protocol v1](docs/node-agent-protocol.md)
- [Bootstrap security model](docs/node-bootstrap-security.md)
- [Disaster recovery boundary](docs/disaster-recovery.md)

If TCP 1935 is occupied, keep the owning service running, choose an approved alternative (for
example 1936), and update both `PUBLIC_RTMP_PORT` and the displayed documentation. Production
deployment stops if CPU, RAM, swap, disk, or port headroom is insufficient.

## Troubleshooting

**The backend exits immediately.** Read only the short error and check that all required secret
variables exist, the session, MediaMTX worker, and bootstrap worker secrets are independent, the
password hash is Argon2id, and the Fernet key was generated by the helper. In production, also
check that `NODE_AGENT_IMAGE` is digest-pinned. Do not paste `.env` into logs or support requests.

**OBS cannot publish.** Confirm OBS uses the displayed server and separate current key. Rotation
invalidates the old path immediately. Check that the configured public RTMP port matches the safe
host mapping.

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
backend logs; do not restart the Docker daemon or unrelated services.

**Server onboarding fails before SSH.** The address must resolve only to public IPs and the
optional host fingerprint must use OpenSSH `SHA256:` format. Check the safe job code; never paste
the SSH password or raw `.env` into a support request.

**A node is degraded or offline.** Heartbeats become degraded after 15 seconds and offline after
30 seconds. Check outbound HTTPS, the marker-owned remote agent project, and its bounded safe logs.
Do not expose an inbound agent port or run a command supplied through the UI.

## Stage 1 limitations

- one administrator and one incoming stream;
- at most two destinations by default (configurable, capped at ten);
- stream copy only: no transcoding, resizing, FPS conversion, overlays, titles, or recording;
- no OAuth or automatic YouTube/VK/Twitch broadcast creation;
- one source-quality authenticated preview, with no quality ladder or public registration;
- no persisted bitrate history or synthetic browser speed test;
- no production deployment in this stage.

## Legacy Stage 4A generic-node limitations

- attached nodes do not carry real video;
- nodes do not publish to YouTube or another platform;
- there is no hot switching or migration of an active stream;
- onboarding uses SSH passwords only, not SSH keys;
- revocation does not uninstall the remote project or Docker; uninstall comes later;
- a lost successful enrollment response before the permanent token is persisted requires a fresh
  bootstrap attempt; Stage 4A has no enrollment credential-recovery handshake;
- `ci-ssh-target` and `ci-node-agent` are CI-only fixtures and never production services.

## Future stages

Future stages can add browser upload-speed measurement, OBS bitrate/resolution/FPS recommendations,
actual bitrate history, richer monitoring, multiple users, SSH-key onboarding, explicit remote
uninstall, and a reviewed media-placement/switching design. The current implementation deliberately
does not simulate a speed test or expose MediaMTX preview or Node Agent ports.
