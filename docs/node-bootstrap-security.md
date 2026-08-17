# Node bootstrap security

Stage 4A bootstrap is a narrow, password-based SSH installer. It is deliberately separated from
the web/backend process because SSH passwords, privileged remote commands, and target-network
access form a different trust domain from the media control plane.

This is an implementation description, not permission to connect to a production host.

## Trust boundaries

```text
authenticated browser
  | session + CSRF, bounded JSON
  v
FastAPI backend
  | authenticated HTTP over project UDS only
  v
isolated bootstrap worker
  | outbound password-only SSH to one validated numeric IP
  v
marker-owned remote Compose project
  |
  +-- outbound HTTPS Node Agent --> FastAPI /node-api/v1/*
```

The backend and bootstrap worker share only the named Unix-domain-socket volume mounted at
`/run/adojapan-bootstrap`; the backend mount is read-only and the worker mount is writable. The
worker has no TCP/UDP port, no backend/database/media network, no main database or backup volume,
and no Docker socket. It joins only the dedicated `bootstrap-egress` network. MediaMTX cannot
reach that network. Both images seed the mount point as UID/GID `10001:10001`, mode `0700`, so
the named volume has the same ownership regardless of which container Docker creates first.

Every UDS request carries an independent `BOOTSTRAP_WORKER_SECRET`. Compose injects it into the
worker from `/run/secrets/bootstrap_worker_secret`, not in the worker environment. The Compose
secret uses the mode-`0600` file selected by `BOOTSTRAP_WORKER_SECRET_FILE`, owned on Linux by the
worker's fixed UID/GID `10001:10001` and excluded from Git and the Docker build context; its contents
must exactly match the backend's independently named `BOOTSTRAP_WORKER_SECRET` environment value. A mismatched value fails authenticated worker
readiness. Production startup and validation require the session secret, MediaMTX worker password,
and bootstrap worker secret to be present and mutually independent.

The worker container runs as fixed non-root UID/GID `10001:10001`, is read-only except for its UDS
volume and `/tmp`, drops all capabilities, enables `no-new-privileges`, has no published/exposed
port, and is bounded to 0.10 CPU, 128 MiB RAM, and 64 PIDs. Only one bootstrap job can run at a
time.

## Password lifecycle

The administrator enters an SSH password in a password input. The browser clears that input and
its request object after submission. The backend validates the request as `SecretStr`, forwards it
over the authenticated UDS, overwrites its request field in a `finally` block, and never stores it
in SQLite.

The worker keeps the request only in the active in-memory job. Secret fields are excluded from
representations. Raw SSH exceptions, commands, stdout, stderr, and credentials are never exposed
through the worker API; only stable safe error codes/messages are returned. When a job reaches a
terminal state, all password/token references and any queued sudo password are cleared. Terminal
safe metadata (never secrets) is retained in memory for 1,200 seconds, longer than the backend's
930-second recovery horizon. Restarting the worker destroys the in-memory job; restarting the
backend coordinator also fails and best-effort cancels a surviving worker job. Either restart
requires the administrator to enter the SSH password again.

If the login is neither root nor passwordless sudo, the worker first tries the already supplied
SSH password for sudo. If that fails, the job pauses. A separately entered sudo password is
accepted only in `needs_sudo_password`, held in a queue of size one, written only to the remote
process stdin via `sudo -S -p ''`, and cleared after use. It is never appended to a command, process
argument, environment variable, database row, log, or UI response.

The initial create request contains no enrollment credential. After SSH/sudo/resource/Docker work
finishes, the worker pauses in `needs_enrollment_token`. The backend monitor then issues a fresh
ten-minute, digest-only enrollment grant and sends its raw value through an authenticated UDS
endpoint. The worker holds it only in a one-item in-memory queue, stages it immediately, and clears
the reference. Concurrent browser and monitor polls are serialized, and the worker exposes only a
non-secret received flag so a lost response cannot cause a second token to invalidate the one it
already accepted. Cancellation and worker restart drain an unconsumed token.

## Target and SSRF policy

Addresses are normalized before resolution. Production rejects whitespace/control/shell
characters, URL-shaped input, credentials, single-label/local names, `.local`, `.lan`, `.internal`,
`.home.arpa`, Docker host aliases, cloud metadata names/addresses, documentation ranges, and every
non-global, private, loopback, link-local, multicast, unspecified, or reserved IPv4/IPv6 result.
IPv4-mapped, 6to4, Teredo, and well-known NAT64 addresses are checked through to their embedded
IPv4 address.

All A/AAAA answers must be public. The worker records one numeric address, resolves again
immediately before connecting, and fails if the selected address disappeared or the test/production
classification changed. It then connects to the originally selected numeric IP, so DNS answer
reordering cannot redirect the job. This is the DNS-rebinding guard.

Test mode has one explicit escape hatch: exact `host:port` entries in
`TEST_SSH_TARGET_ALLOWLIST`. CIDRs and wildcards are not accepted, and a non-empty test allowlist
is invalid outside `ENVIRONMENT=test`. The CI service `ci-ssh-target:22` is reachable only on the
isolated test network, has no host-published port, and is absent from the production Compose model.

## Host-key and SSH policy

SSH uses AsyncSSH directly; there is no shelling out to `ssh`, `sshpass`, an SSH agent, or a user
SSH config. Authentication is password-only: client keys, agent use, keyboard-interactive,
host-based, GSSAPI, and public-key authentication are disabled.

The server key is verified before user authentication. Supported keys are Ed25519, ECDSA
NIST P-256/P-384/P-521, and RSA SHA-2. The stored/displayed identity is an OpenSSH SHA-256
fingerprint. If the administrator supplies an expected fingerprint, comparison is constant-time
and a mismatch fails closed. Without one, the first verified key is TOFU and its fingerprint/trust
mode is recorded with the node. A key or algorithm change within the connection is rejected.
After authentication the worker pauses before **any** remote command until the backend has
transactionally persisted that verified fingerprint and acknowledged it over the authenticated
UDS. A wrong password still exposes the already verified fingerprint for a safely pinned retry.

## Fixed remote workflow

The worker does not accept a user command, script, package, Compose body, destination, path, or
Docker image. The backend supplies only a release-controlled, digest-pinned Node Agent image and
control origin. The command sequence is fixed in code:

1. detect root/passwordless-sudo/password-sudo capability;
2. collect `/etc/os-release`, architecture, package-manager/systemd capabilities, SELinux mode,
   CPU, available memory, and free root disk without network access or a preinstalled HTTP client;
3. classify an allowlisted OS ID/version as Debian family + apt or RHEL family + dnf only after
   the matching package-database and systemd capabilities are present. The allowlist is Ubuntu 22.04/24.04/26.04,
   Debian 12/13, AlmaLinux 8/9, Rocky Linux 8/9, RHEL 8/9, and CentOS Stream 9 on amd64. `ID_LIKE`
   is recorded but is never sufficient to select an adapter. Unknown or mismatched systems fail
   closed;
4. verify official Docker package ownership, Engine server response, Compose v2, and the active
   Docker service without repository access or daemon restart. Only when Docker is proven absent,
   require the corresponding `apt-get` or `dnf` command, select that adapter, install minimal
   HTTPS/key tooling, perform a bounded fixed-host repository probe, verify Docker's fixed
   signing-key fingerprint, verify that all five Docker CE packages exist in the selected
   repository, install them, and start the newly installed service. Partial/foreign/conflicting
   installations are never repaired, upgraded, removed, or restarted;
5. inspect marker and reserved Compose-project ownership, then stage files in a UUID-scoped
   mode-`0700` `/tmp` directory;
6. request a just-in-time one-time credential and install the fixed Compose project with controlled
   modes/ownership;
7. validate Compose, start only project `adojapan-restream-node`, wait for enrollment, verify that
   the enrollment file was consumed, and verify the agent container is running;
8. remove the temporary directory and close SSH.

The installer never runs an arbitrary payload from the browser or node API. Shell snippets are
repository-owned constants and all variable path/value insertions are quoted. Remote output is
bounded where parsed and is not returned to the administrator.

The dnf adapter writes only an allowlisted Docker repository with `gpgcheck=1`; it never disables
TLS/signature checks, runs `dnf update`/`dnf upgrade`, or removes Podman/runc/another runtime.
AlmaLinux remains the detected OS, but its Docker repository compatibility profile is the matching
hardcoded RHEL major (`rhel/8` or `rhel/9`). This uses AlmaLinux's RHEL ABI compatibility and does
not represent AlmaLinux as an officially documented Docker Engine platform. Rocky, RHEL, and
CentOS Stream retain their separate allowlisted mappings, and no repository coordinate is accepted
from the browser or SSH target.

After `makecache`, dnf checks each exact required package only with `docker-ce-stable` enabled and
all other repositories disabled. Missing `docker-ce`, `docker-ce-cli`, `containerd.io`,
`docker-buildx-plugin`, or `docker-compose-plugin` stops before the install command with
`docker_repository_incomplete`. The apt adapter likewise uses Docker's fixed signed repository and
no convenience script. Both adapters map repository reachability, signing-key,
conflicting-runtime, unsupported-installation, and installation failures to distinct safe codes
without attaching package-manager output.

SELinux remains in the detected host mode, including Enforcing. The generated agent Compose uses a
private `Z` relabel on only `./data` and disables automatic source-path creation. Bootstrap does not
change SELinux configuration, mode, or host policy and does not run manual relabel tools.

## Ownership, idempotency, and rollback

The RPM Docker repository has its own narrower ownership contract, separate from the Node Agent
directory. A new file contains the fixed
`managed-by-adojapan-restream-node-bootstrap:v1` comment, but that comment alone never authorizes
replacement or deletion. Recovery additionally requires the exact generated-content SHA-256,
regular non-symlink root-owned repo/key files, Docker's exact approved GPG fingerprint, no foreign
Docker repository, no Docker binary/service/socket/packages/data roots, and no Node Agent managed
root. One legacy classifier accepts only the exact markerless AlmaLinux repo/key pair generated by
the earlier failed AdoJapan version under those same absence conditions. Every negative variant is
`unsupported_docker_installation` and causes no mutation.

After the current repo/key pair is committed, a package-gate or pre-install failure removes it only
when the same exact ownership and whole-Docker-absence contract still holds. If any Docker package,
daemon artifact, socket, data root, foreign file, owner, symlink, hash, or fingerprint appears,
cleanup fails closed with `docker_failed_install_recovery_unsafe`; no package is automatically
uninstalled. Minimal prerequisite packages may remain and do not authorize broader cleanup.

The only owned remote directory is `/opt/adojapan-restream-node`; the only project name is
`adojapan-restream-node`; and ownership requires regular marker file
`.managed-by-adojapan` containing exactly `adojapan-restream-node:v1`. A missing path can be
created, an exactly managed path can be updated, and every other case fails without mutation.

For a fresh install, containers, networks, or volumes carrying the fixed Compose project label (or
the fixed reserved container/network/volume names) are a conflict. That inventory is checked before
the root claim and again immediately before the first `compose up`. Lifecycle commands name only
the declared `agent` service; they never use `--remove-orphans`.

Before updating a managed install, rollback copies are stored inside the already marker-owned
directory as root-owned mode-`0600` files. The previous Compose file uses
`.compose.rollback-<job UUID>`; a pending enrollment credential can additionally use
`.enrollment.rollback-<job UUID>`, and credential rotation can use
`.node-token.rollback-<job UUID>`. On failure after apply:

- before enrollment, a newly created, correctly marked project is brought down and only its
  managed directory is removed;
- an existing managed project can have its previous Compose/credential/process state restored,
  including after enrollment, while the exact marker and staged node ID still match and every
  required rollback copy is intact;
- after enrollment, a newly created project is brought down but its managed directory and current
  evidence are retained;
- a missing/wrong/symlink marker prevents destructive cleanup or restoration.

Rollback is best effort so it cannot replace the original safe failure. Before enrollment
completes, a new-install rollback first requires the exact staged node ID and marker, then requires
Compose shutdown to succeed before deleting managed files. A managed update instead restores its
previous configuration, credential, and process state under the same exact guard. That restorative
rollback remains available after enrollment; after a verified successful restore, its temporary
managed-root rollback copies are removed. If any restore phase fails, ownership evidence,
configuration, credentials, and rollback copies are retained rather than risking partial cleanup.

Enrollment completion is the filesystem-deletion boundary for a **new** install. If cancellation,
timeout, or final-check failure occurs after its permanent node credential was issued, rollback may
still perform the exact-scoped Compose shutdown and the backend revokes that credential, but it
does not delete the fresh managed files. The exact managed root, marker, node ID, current
configuration, and credentials remain as evidence for a separately reviewed, node-specific
recovery. Successful workflow commit, a failure before files are applied, or a verified successful
managed restore removes temporary rollback copies. Do not improvise deletion of retained evidence.

Rollback does not uninstall Docker, prune Docker state, delete unrelated images/volumes/networks,
directly alter firewall/SSH settings, or touch any foreign service. Its exact-scoped shutdown, managed
restore, and backend revocation remain armed after enrollment while the final container check and
job transition are still pending; only fresh-install deletion is disarmed at enrollment. Only a
successfully completed and committed workflow disables automatic rollback; subsequent remote
removal requires a future explicit uninstall design.

## Timeouts, cancellation, and restarts

Default upper bounds are 10 seconds for TCP connect, 15 seconds for SSH login, 60 seconds for an
ordinary command, 300 seconds for a package operation, 300 seconds for enrollment, and 900 seconds
for the entire job. The overall timeout cannot be configured above one hour. SSH close gets a
final five-second bound.

A temporary UDS/socket outage does not immediately fail or revoke the persisted job. The backend
keeps the singleton active, retries synchronization, and lets a reloaded page rediscover its last
safe persisted view. Communication can resume with the same worker identity; only a proven worker
restart/job loss or an outage still present after the worker's 900-second overall deadline plus a
30-second backend grace becomes terminal.

The backend supplies a caller UUID when creating a worker job. Create is idempotent only for the
same secret-free node/target identity, and an authenticated discovery endpoint recovers a lost
`202 Accepted`. A short settle window treats an immediate authenticated `404` as inconclusive when
the original DNS/create request may still be finishing. GET and cancellation use the same attach
path; cancellation intent is persisted as `cancelling` and is delivered as soon as the worker
identity becomes observable. On backend startup, a still-NULL identity is discovered and cancelled
before the persisted job is failed, so hidden SSH work is not left running.

Cancellation is cooperative at state boundaries and while waiting for a sudo password or
enrollment. The worker closes the SSH connection and performs marker-and-node-ID-scoped rollback
when files were applied. Its UUID-scoped `/tmp` staging directory is removed separately on a
best-effort basis; managed-root rollback copies follow the retention rules above. A proven worker
restart/job loss, or a backend coordinator restart which best-effort cancels the surviving worker
job, becomes a safe `bootstrap_worker_restarted` failure; secrets are cleared and cannot be resumed
from persisted state. The control-plane Compose service allows 90 seconds for graceful shutdown,
longer than the worker's 70-second ASGI shutdown bound, so a scoped rollback is not normally
interrupted by an early container `SIGKILL`.

## CI boundary and Stage 4A limitations

`ci-ssh-target` and `ci-node-agent` are CI-only fixtures. The SSH fixture is exact-allowlisted and
internal to `bootstrap-egress`; the real test agent uses a CI node-data volume and has no inbound
port. Both are absent from the effective production configuration. CI credentials and enrollment
data are disposable test values, never production secrets. CI creates its worker secret source as
a temporary mode-`0600`, UID/GID-`10001:10001` file and removes it in the unconditional project cleanup.
An additional unprivileged RPM fixture exposes only strict fake `rpm`, `dnf`, and `systemctl` argv;
it proves the RHEL-family adapter contract without Docker-in-Docker or a privileged runner. Its
`alma_native_repo_incomplete` mode models successful metadata refresh with the two Engine packages
missing and proves that the exact-package gate stops before `dnf install`. CI also passes the
generated SELinux-aware Node Agent document through the real Compose parser.

Stage 4A bootstrap does not support SSH keys, configure sshd, issue direct firewall-management
commands, expose an agent port, use host networking, send video, publish to YouTube, hot-switch
streams, or uninstall a revoked node. It does not edit existing firewall rules or Docker daemon
firewall configuration. When Docker is already supported and running, it is neither reconfigured
nor restarted. Installing/starting an absent Docker Engine and creating the project-scoped bridge
can create Docker-managed netfilter rules for bridge networking, NAT, and container isolation;
that standard Docker behavior is an accepted system effect, not direct firewall management by
AdoJapan. Repository policy and the exact CI Compose validator reject direct firewall tooling,
daemon firewall options, host ports, and host networking. See
[Node onboarding](node-onboarding.md) and [Node Agent protocol](node-agent-protocol.md).
