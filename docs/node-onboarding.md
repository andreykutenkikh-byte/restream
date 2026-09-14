# Legacy generic Node Agent onboarding (Stage 4A)

> **Current public workflow:** **Servers → Connect server** provisions a complete native Moblin
> Relay, not the generic Docker Node Agent described below. Supported systems, installed files,
> operator steps, credential handling, and the clean-host release gate for that workflow are in
> [Moblin Relay onboarding](moblin-relay-onboarding.md). The generic profile remains an internal
> compatibility path and cannot be selected by the browser.

Stage 4A lets the single authenticated administrator attach one or more supported Linux servers
through the legacy generic profile. The control plane performs a bounded SSH bootstrap, installs
an outbound-only Node Agent, waits for one-time enrollment, and runs a fixed installation check.
The remote node is not part of the media path.

This document describes the implemented workflow. It is not authorization to run it against a
production server.

## Before adding a server

The target must meet all of these requirements. Distribution and package manager are detected
automatically; the administrator does not choose them in the UI.

| Distribution | Supported releases | Auto-install adapter |
| --- | --- | --- |
| Ubuntu | 22.04, 24.04, 26.04 | Debian family / apt |
| Debian | 12, 13 | Debian family / apt |
| AlmaLinux | 8.x, 9.x | RHEL family / dnf |
| Rocky Linux | 8.x, 9.x | RHEL family / dnf |
| Red Hat Enterprise Linux | 8.x, 9.x | RHEL family / dnf |
| CentOS Stream | 9 | RHEL family / dnf |

Every supported release currently requires `amd64`/`x86_64`. Unknown distributions, Alpine,
Arch, unsupported major releases, non-amd64 systems, or a mismatch between the detected family and
its package database/systemd capabilities are refused without package changes. `apt-get` or `dnf`
is required only for an absent-Docker auto-install; a supported existing Docker remains eligible
for read-only inspection when that install command is unavailable.

The remaining host requirements are:

- at least 1 online CPU;
- at least 700 MiB of currently available memory;
- at least 8 GiB free on `/`;
- a public IP address or public DNS name and a reachable SSH port;
- outbound HTTPS access to Docker's official repository only when Docker must be installed, and to
  the configured AdoJapan control origin while the agent is running; a supported existing Docker
  installation does not require Docker-repository reachability during bootstrap;
- either `root`, passwordless `sudo`, or a separately supplied working `sudo` password;
- enough host headroom for the agent container ceiling: 0.25 CPU, 256 MiB RAM, and 128 PIDs.

The production control plane also requires `NODE_AGENT_IMAGE` to be an immutable registry
reference ending in `@sha256:<64 lowercase hexadecimal characters>`. A mutable tag is rejected
before production startup. Building or publishing that image is a separate release action, and the
digest cannot be approved until the release workflow proves an anonymous pull of that exact
reference from a fresh runner with an empty Docker configuration.

For the first release, the package owner may need to publish once, manually set the GHCR
`restream-node` package visibility to public, and rerun the failed anonymous-pull verification. Do
not enable production onboarding until the complete workflow passes. The operator does not run
`docker login ghcr.io` on the target VPS; bootstrap sends no PAT, registry token, Docker config, or
other registry credential over SSH. It supplies only the approved exact image reference.

## Administrator flow

1. Open **Servers** while signed in.
2. Choose **Add server** and enter the address, SSH port, username, and SSH password. There is no
   distribution or package-manager field. Supplying
   the server's OpenSSH SHA-256 host-key fingerprint is strongly recommended. If it is omitted,
   the first presented key is accepted using TOFU and pinned to that node record.
3. Submit the form. Mutating requests require the existing session and CSRF token. Bootstrap
   creation is limited to five attempts per client in ten minutes, and only one bootstrap job can
   be active at a time.
4. Follow the bounded progress steps: SSH connection, system check, resource check, Docker check,
   agent installation, panel connection, and final check.
5. If the account needs password-based `sudo`, enter that password only when the workflow pauses
   in `needs_sudo_password`. The endpoint rejects it in every other state.
6. A successful job ends with the enrollment file consumed and the agent container running. Its
   first heartbeat moves the node to `ready`. The server card then shows safe inventory and health
   fields; the administrator can queue the protocol-level `SELF_TEST` from that card.

Reloading **Servers** discovers the single persisted active job and restores its progress dialog,
including the sudo-password prompt and cancellation action. A temporary worker-socket outage keeps
the job active and blocks a second install. The page retries transient polling five times with
bounded backoff, then asks the administrator to reload; the backend keeps synchronizing through the
worker deadline, and a reload rediscovers the same job after communication returns.

A caller-supplied job UUID makes a lost worker `202 Accepted` discoverable without starting a
second SSH job. An immediate cancellation is persisted and delivered once that worker identity is
attached. Backend or worker restart still cancels/fails the job and requires password re-entry.

The administrator can rename a node, queue `PING` or `SELF_TEST`, inspect the resulting command,
or revoke access. `PING` and `SELF_TEST` are fixed protocol operations; neither accepts a command
line or arbitrary payload.

## What is installed remotely

The installer owns only this marker-guarded scope:

| Item | Fixed value |
| --- | --- |
| Managed directory | `/opt/adojapan-restream-node` |
| Ownership marker | `/opt/adojapan-restream-node/.managed-by-adojapan` |
| Marker value | `adojapan-restream-node:v1` |
| Compose file | `/opt/adojapan-restream-node/compose.yml` |
| Data directory | `/opt/adojapan-restream-node/data` |
| Compose project | `adojapan-restream-node` |
| Container user | UID/GID `10001:10001` |

An absent directory is created. An existing directory is touched only when it is a real directory
with the exact regular-file marker and marker value. Any foreign, symlinked, or otherwise
ambiguous path fails with `remote_directory_conflict`.

An otherwise absent root also fails closed if the reserved Compose project already has a labeled or
fixed-name container, network, or volume. The check runs before claiming the directory and again
immediately before the first agent start. Remote lifecycle operations target only the declared
agent service and never remove Compose orphans.

The generated Compose project starts exactly one agent container from the configured digest-pinned
image. It publishes no ports, drops all capabilities, enables `no-new-privileges`, uses a read-only
root filesystem and a small `/tmp`, binds only its own data directory, and uses
`restart: unless-stopped` with a 45-second stop grace period. It does not mount the Docker socket,
SSH material, registry credentials, or another project's files. The production control plane does
not build, retag, or replace this image on the target. The data bind uses long Compose syntax with
`create_host_path: false` and private SELinux relabel `Z`; the option is ignored on systems without
SELinux. Bootstrap never disables SELinux, changes `/etc/selinux/config`, runs a manual relabel, or
installs a host policy.

If a supported target lacks Docker, bootstrap installs Docker Engine and the Compose plugin from
Docker's official allowlisted repository through the apt adapter on Ubuntu/Debian or the dnf
adapter on the supported RHEL-family matrix. Both adapters verify the fixed Docker signing-key
fingerprint before configuring the repository, install only the required Docker CE packages, and
never run a general system upgrade or uninstall a conflicting runtime. An incompatible, partial,
or conflicting existing Docker installation fails closed; it is not replaced implicitly. A
supported existing installation is inspected only: package ownership, server response, Compose v2,
and active service are checked without repository access, package changes, daemon configuration, or
daemon restart.

The RPM repository mapping is a code allowlist, not SSH/browser input: AlmaLinux and CentOS Stream
use Docker's CentOS-compatible endpoint, Rocky uses Docker's Rocky endpoint, and RHEL uses Docker's
RHEL endpoint. Redirect following is disabled for repository/key fetches, HTTPS is mandatory, the
downloaded key must match Docker's reviewed fingerprint, and the generated RPM repository keeps
`gpgcheck=1`.

The UI receives only stable localized failures. Platform/package/install failures are distinguished
as `unsupported_operating_system`, `unsupported_package_manager`,
`unsupported_docker_installation`, `conflicting_container_runtime`,
`docker_repository_unavailable`, `docker_repository_key_invalid`, or `docker_install_failed`.
Package-manager output, repository contents, remote commands, and credentials are never included.

In this workflow, “the installer does not change the firewall” means that AdoJapan never invokes
`ufw`, `iptables`, `ip6tables`, `nft`, or `firewall-cmd`; never edits existing user firewall rules;
and never changes `/etc/docker/daemon.json`, Docker's firewall backend, or its iptables/ip6tables
daemon options. Installing or starting an absent Docker Engine and creating the project-scoped
bridge can still create Docker-managed netfilter rules required for bridge networking, NAT, and
container isolation. Those standard Docker-managed rules are an explicitly accepted system effect,
not direct firewall management by the installer. The Node Agent publishes no host ports and does
not use host networking.

## Enrollment and status

The control plane issues a single-use enrollment token with a ten-minute lifetime. Bootstrap does
**not** issue it when the job is created. Only after SSH, privilege, resource, and Docker work has
completed does the worker pause for a just-in-time token from the backend monitor. It is sent
through the authenticated Unix-socket API, kept only in worker memory, and uploaded immediately as
`data/enrollment.token` with mode `0600`. Thus a slow preflight does not consume the token's
ten-minute lifetime. The agent exchanges it once over HTTPS, atomically writes `data/node.token`
with mode `0600`, and then deletes the enrollment file. The raw permanent token is returned only
once; the control plane stores SHA-256 digests, not raw enrollment or permanent credentials.
Credentials are never supplied through environment variables or process arguments.

The agent sends a heartbeat every five seconds. The UI derives connection status from heartbeat
age:

- at most 15 seconds: `ready`;
- more than 15 and at most 30 seconds: `degraded`;
- more than 30 seconds: `offline`.

Hostname, OS identity, and architecture supplied by the SSH preflight describe the VPS rather than
the agent container. The agent uses those non-secret overrides in enrollment and heartbeat while
continuing to collect dynamic metrics locally.

The stale-heartbeat projection does not replace `installing`, `connecting`, `failed`, or `revoked`
with an age-derived state. A successfully authenticated fresh heartbeat can recover a non-revoked
node to `ready`; revocation blocks authentication. See
[Node Agent protocol](node-agent-protocol.md) for the complete wire contract and metric set.

## Failure, cancellation, and rollback

SSH connect, authentication, individual commands, package work, enrollment, and the whole job
have explicit timeouts. A cancellation request stops at cooperative checkpoints and closes the
SSH session. A worker restart/job loss, or a backend coordinator restart which best-effort cancels
a surviving worker job, produces a safe failure in the UI; retrying requires entering the password
again.

Before applying an update to an already managed directory, bootstrap saves the previous Compose
file as root-owned mode-`0600`
`/opt/adojapan-restream-node/.compose.rollback-<job UUID>`. A pending enrollment token or permanent
token being rotated can have matching `.enrollment.rollback-<job UUID>` or
`.node-token.rollback-<job UUID>` copies in that same marker-owned directory. If a new install fails
before enrollment, rollback requires both the exact marker and staged node ID, brings down only
project `adojapan-restream-node`, and removes managed files only after that shutdown succeeds. A
managed update can restore the previous Compose/credential state and prior agent process state
under the same exact marker-and-node-ID guard. That restore remains available after enrollment;
after it succeeds completely, its temporary rollback copies are removed.

Enrollment completion ends filesystem deletion for a **new** install, not all rollback actions.
Until the job is successfully completed and committed, a later cancellation/failure can still stop
the exact fresh Compose project and the backend revokes the credential issued by the failed
workflow. Bootstrap retains that fresh managed root, marker, node ID, and current
configuration/credentials as evidence; UUID-scoped `/tmp` staging is removed separately. For an
existing managed install, the exact guarded restore described above is still attempted after
enrollment. A successful commit or verified successful managed restore removes its temporary
rollback copies; an incomplete restore retains those copies and the managed evidence for a
separately reviewed, node-specific recovery. Do not manually delete or reuse retained evidence
without that review.

Rollback is best effort and never removes Docker, prunes images/volumes, or changes foreign
directories and services.

Revoking a node is a control-plane credential action. It invalidates the node token and any unused
enrollment token, marks the node `revoked`, cancels queued or in-flight commands, and makes later
heartbeat/command calls return `401`. It deliberately does **not** SSH to the host, stop the agent,
delete `/opt/adojapan-restream-node`, or uninstall Docker. Remote uninstall is planned for a later
stage. After observing that permanent rejection, the agent stops outbound traffic and remains
quiescent until the container is explicitly stopped.

## Stage 4A limitations

- The node reports inventory and executes only `PING` and `SELF_TEST`; it carries no real video.
- There is no YouTube or other platform output from a node.
- There is no hot switching or migration of an active stream between nodes.
- Bootstrap supports password authentication only; SSH-key onboarding is not implemented.
- Revocation does not uninstall remote files or Docker; an uninstall workflow comes later.
- A lost successful enrollment response before `node.token` is persisted requires a fresh
  bootstrap/enrollment attempt; the rejected agent waits quiescently for that managed replacement,
  and Stage 4A has no credential-recovery handshake.
- Test SSH targets and test agents are CI-only fixtures, exact-allowlisted in test mode, have no
  host-published ports, and are absent from the production Compose model.
- Standard Docker-managed netfilter rules created by Docker installation/start or the isolated
  project bridge are expected; direct firewall commands, daemon firewall configuration, host ports,
  and host networking are repository-policy violations.

For the security boundaries and failure model, see
[Bootstrap security](node-bootstrap-security.md). For control-plane deployment gates, see
[Deployment and rollback](deployment-and-rollback.md).
