# Production audit gate

This runbook is documentation for a future, separately approved production stage. Do not execute
it merely because Stage 4A code or documentation is reviewed. Stage 4A preparation did not deploy
the control plane, connect to a production node, change DNS/firewalls/reverse proxy, restart an
existing service, or create/use real SSH, node, stream, or platform credentials.

## Phase 2 preparation snapshot

The read-only production checks on 2026-07-13 did not deploy AdoJapan Restream and did not
change DNS, either firewall layer, Nginx, Docker project lifecycle, or any existing service.
They established the following current blockers and constraints:

- `restream.adojapan.ru` has an A record for `85.198.119.5` with TTL 3600 and no AAAA or CNAME.
  That target is active: HTTP redirects to HTTPS and HTTPS returns 200 with a valid certificate.
  DNS cutover is therefore **NO-GO** until the owner confirms the current service's purpose,
  approves replacement, and accepts the rollback target.
- UFW is inactive. The host IPv4 INPUT policy is ACCEPT, the IPv6 INPUT policy is ACCEPT, and
  the provider firewall state is unknown. Firewall readiness is **NO-GO** until both host and
  provider policies are reviewed and an access-preserving change/rollback plan is approved.
- The host has sufficient observed idle headroom for the proposed bounded profile, TCP 1935 and
  loopback TCP 8088 were free, the existing containers were healthy, and the pre-existing Nginx
  and Docker process identities remained unchanged during preparation. These observations must
  be repeated in the deployment window; they are not deployment authorization.

The Phase 2 readiness gate also requires all of the following evidence to remain true at the
future change window:

1. A fresh key-only SSH session to the **control-plane deployment host** succeeds before any
   SSH/security change, and password authentication is not used for that administrative session.
   This is separate from Stage 4A's password-only onboarding of a future node.
2. The legacy n8n Traefik service remains excluded from the default n8n Compose model, has
   restart policy `no`, and publishes neither 80 nor 443; the existing Nginx continues to own
   those ports.
3. Existing container IDs, start times, restart counts, health, and the Nginx/Docker process
   identities match the saved baseline before Restream starts.
4. TCP 1935 and loopback TCP 8088 are still free, and resource/OOM checks still support the
   0.70 CPU / 704 MiB / 224 PID bounded profile including bootstrap.
5. No Restream production directory, production `.env`, container, image, network, or volume
   exists before the explicitly approved deployment step.
6. The DNS-owner decision and host/provider firewall plan are approved; either unresolved item
   keeps the deployment at NO-GO.

## Shared-host production profile

Production must load `compose.production.yml` after `compose.yml` for validation and every
lifecycle command. The effective limits must be exactly:

| Service | CPU | RAM | PIDs | Destinations | Restart policy |
| --- | ---: | ---: | ---: | ---: | --- |
| backend | 0.40 | 384 MiB | 96 | 1 | `unless-stopped` |
| bootstrap | 0.10 | 128 MiB | 64 | n/a | `unless-stopped` |
| MediaMTX | 0.20 | 192 MiB | 64 | n/a | `unless-stopped` |
| **Total** | **0.70** | **704 MiB** | **224** | **1** | all required |

The override must also make the security mode and public identity fail-closed. Its effective
values are `ENVIRONMENT=production`, `COOKIE_SECURE=true`, `MAX_DESTINATIONS=1`,
`PUBLIC_DOMAIN=restream.adojapan.ru`, `PUBLIC_RTMP_HOST=restream.adojapan.ru`,
`PUBLIC_RTMP_PORT=1935`, `PUBLIC_CONTROL_URL=https://restream.adojapan.ru`, and
`NODE_PROTOCOL_VERSION=1`. These fixed values are appropriate because this project has one defined
public identity; a production `.env` cannot replace them with development mode, insecure cookies,
or `localhost`. `SESSION_SECRET`, `WORKER_AUTH_PASSWORD`, and `BOOTSTRAP_WORKER_SECRET` remain
independently supplied secrets and must all differ. `BOOTSTRAP_WORKER_SECRET_FILE` must name a
Git-excluded, mode-`0600` file owned by UID/GID `10001:10001` and containing exactly the backend
bootstrap-secret value; Compose mounts that file read-only into the fixed non-root bootstrap worker
instead of placing the secret in its environment.

Production must also supply `NODE_AGENT_IMAGE` as an immutable amd64 registry reference ending in
`@sha256:<64 lowercase hexadecimal characters>`. Record the digest, reviewed source commit, build
provenance, and vulnerability review. A mutable tag, local-only image, or CI tag is a no-go. A
digest is necessary but not sufficient: the Node Agent release workflow must have pulled that exact
`ghcr.io/andreykutenkikh-byte/restream-node@sha256:...` reference from a fresh runner with an empty
Docker configuration, no registry login, and no package permission. Preserve the successful run as
release evidence before the control-plane deployment and first real onboarding.

GHCR packages can require a one-time manual visibility change after their first publication. If the
anonymous job fails, the package owner must set `restream-node` to public and rerun the workflow;
the failure remains a no-go until the complete run passes. Do not automate this action with a PAT or
new privileged secret. Neither the production operator nor bootstrap may run `docker login ghcr.io`
or transfer registry credentials to an attached VPS.

The bootstrap worker must have no port/expose entry, Docker socket, database/application volume,
or backend/media network. Its only volume is the writable UDS volume and its only network is
`bootstrap-egress`; the backend sees the same UDS volume read-only. MediaMTX must not join the
bootstrap network. Its effective restart policy must be `unless-stopped` and its shutdown grace
must be 90 seconds; the backend and MediaMTX must also resolve to `unless-stopped`. A failure of
any of these isolation or lifecycle checks is a no-go.

The public RTMP identity is `restream.adojapan.ru:1935`; the host-side bind is separate. The
planned server is `147.45.231.225`. The base Compose file fixes the HTTP host address to loopback;
verify the effective HTTP mapping is `127.0.0.1:8088`. The reviewed RTMP bind is
`147.45.231.225:1935` and remains controlled by the approved `RTMP_BIND_ADDRESS`. A different RTMP
address or port requires a fresh port, DNS/firewall, and documentation review.

Validate the effective production model before build or start:

```bash
docker compose -p adojapan-restream --env-file .env -f compose.yml -f compose.production.yml config --quiet
```

Use a structured parser to inspect only service names, resource limits, destination count, and
published addresses. Never print the resolved production environment.

## CI evidence gate

Before a deployment can be considered, a successful GitHub Actions run for the exact reviewed
commit must exercise the effective Compose order `compose.yml`, `compose.production.yml`, then
`compose.ci.yml` for build, startup, logs, and cleanup. The last file is CI-only: it switches the
synthetic runtime to `ENVIRONMENT=test` and `COOKIE_SECURE=false`, adds the exact destination
and SSH allowlists, isolated media helpers, `ci-ssh-target`, and `ci-node-agent`, and must never
enter a production lifecycle command. The SSH target must be internal to `bootstrap-egress` with
no host port. The agent may share only its CI node-data volume and must have no inbound port. Both
fixtures must be absent when only the base and production files are rendered.

The run must safely confirm the actual backend limits of 0.40 CPU, 384 MiB, and 96 PIDs, the
bootstrap limits of 0.10 CPU, 128 MiB, and 64 PIDs, and the MediaMTX limits of 0.20 CPU, 192 MiB,
and 64 PIDs, plus runtime `RestartPolicy.Name=no`, zero unexpected restarts, and no OOM event,
without printing container environments. It must rotate ingest once offline and once with an
active publisher, terminate the previous publisher, reject the previous key, and accept the
replacement key. Stage 4A CI must prove bounded password-only SSH
bootstrap against the exact allowlisted fixture, host-key validation, single-use enrollment-file
promotion, five-second heartbeat, fixed `PING`/`SELF_TEST`, revocation, no inbound Node Agent port,
and marker-scoped rollback. The media API test must keep the first destination active while a
second is rejected with `409 destination_limit_reached`, then complete the synthetic media path
and cleanup. Even a green run is test evidence only: it does not deploy the production model,
authorize onboarding, or close the DNS and firewall NO-GO gates below.

## DNS cutover gate

The authoritative nameservers are `ns1.nsadv.ru` and `ns2.nsadv.ru`, which identifies the DNS
service path but not the zone owner. Do not change the current A record until the active service
at `85.198.119.5` has a named owner and explicit cutover approval. Export the current zone or at
least record `A 85.198.119.5`, TTL 3600, and the absence of AAAA and CNAME.

With owner approval, reduce the A-record TTL to 300 at least one old TTL before cutover;
preferably do this 24 hours before the window. Confirm both authoritative servers return TTL 300,
then wait the previous 3600 seconds plus a safety margin. Validate the new HTTPS virtual host on
`147.45.231.225` with a hosts override or `curl --resolve` before public DNS changes. Keep AAAA
absent unless IPv6 has its own reviewed service and firewall path.

Read-only DNS verification commands:

```bash
dig restream.adojapan.ru A +noall +answer
dig @ns1.nsadv.ru restream.adojapan.ru A +noall +answer
dig @ns2.nsadv.ru restream.adojapan.ru A +noall +answer
dig @1.1.1.1 restream.adojapan.ru A +noall +answer
dig @8.8.8.8 restream.adojapan.ru A +noall +answer
dig restream.adojapan.ru AAAA +noall +answer
dig restream.adojapan.ru CNAME +noall +answer
```

The approved cutover changes only the A record to `147.45.231.225` with TTL 300. Verify the two
authoritative servers before the system, `1.1.1.1`, and `8.8.8.8` resolvers; then verify the
HTTP-to-HTTPS redirect, certificate name/chain, login, liveness/readiness, and existing-service
health from multiple external networks. Keep the old host available for 24-48 hours and observe
both hosts for at least two new TTLs after recursive resolvers converge.

Roll back only the A record to `85.198.119.5` with TTL 300 on a wrong certificate, sustained 5xx,
failed application health/login, unavailable RTMP, data inconsistency, unexpected routing, or
impact to the service that currently owns the name. Re-run the same resolver and HTTPS checks,
and retain both hosts until the maximum previously observed TTL has expired. If the application
holds mutable data, use an approved maintenance/read-only window and reconciliation plan; DNS
rollback alone cannot merge diverged state.

## Firewall design gate

Do not apply rules during preparation. The reviewed design must cover both the provider and host
firewalls, IPv4 and IPv6, Docker-published-port behavior, and an out-of-band recovery path. It
should allow 80/443 publicly only when HTTPS is ready, allow SSH only from an approved trusted
source or VPN, allow 1935 only from the approved OBS source or VPN, and keep 8088 and 1936
unreachable from the public network. Preserve established connections and essential ICMPv6.
Because Docker can bypass ordinary host INPUT rules, enforce any host restrictions in the
appropriate pre-Docker forwarding chain and test them before a default deny. Never flush existing
rules blindly. Save restorable IPv4/IPv6 rulesets, keep provider console access, open a second
verified SSH session, and define a timed rollback before applying a future firewall change.

For Stage 4A attached nodes, the approved boundary is narrower and explicit: the AdoJapan
installer must not invoke firewall tools, edit user rules, or change Docker daemon/firewall
configuration. Standard Docker-managed netfilter rules created when an absent Docker Engine is
installed/started or when the isolated project bridge is created are expected system effects and
are not classified as direct firewall changes by AdoJapan. Existing supported Docker daemons are
not reconfigured or restarted; the Node Agent publishes no host ports and never uses host
networking. This interpretation does not waive the separate provider/host firewall review for the
control-plane production server.

## Capture the baseline

Save every result in a dated audit directory. Do not include environment values, database
contents, stream keys, cookies, or credentials.

```bash
uname -a
cat /etc/os-release
uptime
free -h
df -h
swapon --show
ps aux --sort=-%mem
ps aux --sort=-%cpu
docker ps
docker stats --no-stream
docker network ls
docker volume ls
ss -lntup
```

Identify the OS and architecture, load average, RAM/swap/disk headroom, OOM history, current
reverse proxy, active containers, networks, volumes, and every occupied TCP/UDP port. Verify
that TCP 1935 and the chosen loopback HTTP port are free. If 1935 is occupied, keep its owner
running and choose a documented alternative. Resolve `restream.adojapan.ru` and confirm the
resulting address is intentional.

## Go/no-go gate

Stop the deployment plan if CPU, RAM, swap, disk, or port headroom is insufficient. Before a
go decision, preserve the pre-deployment lists and resource measurements. Review Compose
limits against observed load; the committed values are conservative starting points, not a
substitute for this audit. Also stop if the Node Agent image digest/provenance is unreviewed, the
exact digest lacks successful anonymous-pull evidence, any bootstrap trust-domain secret is reused,
the UDS/network boundary differs from the committed model, or a CI-only SSH/agent fixture appears
in the production service set. The control plane must not build, retag, or substitute the Node Agent
image during deployment.

## Controlled rollout

Build only this repository and start only `adojapan-restream`. Do not edit another Compose
project, network, volume, database, or proxy site. Back up the existing proxy configuration,
validate the new site configuration, and use a safe reload. Then verify:

1. The pre-existing container list and services remain healthy.
2. This project's liveness and readiness endpoints are healthy.
3. HTTPS, login, logout, session flags, and CSRF behavior work.
4. A short synthetic RTMP ingest is accepted with a temporary test key.
5. One approved synthetic RTMP/RTMPS receiver with a public destination address receives the
   stream. Run only this one-direction copy (`OBS -> AdoJapan -> controlled receiver`), with
   synthetic ingest and destination keys, for a short bounded interval. Do not connect a real
   platform and never enable the CI-only local destination allowlist in production.
6. CPU, RAM, process counts, and OOM history stay within the approved envelope.
7. Stopping this project does not affect any existing service.

Never use real platform keys for the audit. Do not onboard a real SSH target as part of this
control-plane rollout unless a separate node change explicitly authorizes it. Stage 4A nodes do
not carry video, so a node heartbeat is not a media-path acceptance test. Record findings and
obtain approval before the production change.

## Required production checklist

Record an owner and evidence for every item before proceeding:

1. Save the pre-deployment container list.
2. Save the pre-deployment network and volume lists.
3. Save every occupied TCP/UDP port.
4. Capture CPU and RAM measurements.
5. Identify the existing reverse proxy and its safe validation/reload commands.
6. Verify the selected RTMP port is unoccupied; never stop its owner if it is occupied.
7. Verify DNS for `restream.adojapan.ru`.
8. Confirm sufficient RAM headroom after current services and filesystem cache.
9. Confirm sufficient CPU headroom under current load.
10. Review swap capacity and recent kernel OOM events.
11. Build only images introduced by this repository.
12. Start only Compose project `adojapan-restream`.
13. Confirm no pre-existing container restarted or stopped.
14. Save and compare the post-start container list.
15. Run health checks for every pre-existing service.
16. Check those services for new errors after startup.
17. Verify liveness and readiness of AdoJapan Restream.
18. Verify HTTPS and the dedicated host routing.
19. Verify login, CSRF protection, logout, and cookie flags.
20. Run a short synthetic RTMP ingest test with a disposable key.
21. Run one short one-direction synthetic RTMP/RTMPS output test against an approved controlled
    public-address receiver, without a real platform key; do not set
    `TEST_DESTINATION_ALLOWLIST` in production.
22. Capture CPU and RAM during the media test.
23. Confirm no OOM event or unexpected restart occurred.
24. Configure readiness/restart/OOM monitoring without raw sensitive log matching.
25. Stop only this project and confirm other services remain healthy.
26. Record the reviewed rollout and rollback commands and responsible operator.
27. Confirm the effective total is exactly 0.70 CPU, 704 MiB RAM, and 224 PIDs, including
    bootstrap.
28. Confirm the bootstrap UDS mounts, separate egress network, no ports, no Docker socket, and
    independent secret match the reviewed model.
29. Record and verify the immutable `NODE_AGENT_IMAGE` digest, build provenance, and successful
    fresh-runner anonymous pull of that exact digest without registry credentials.
30. Confirm `ci-ssh-target`, `ci-node-agent`, and both test allowlists are absent from production.
31. Confirm node onboarding remains a separately authorized operation and that Stage 4A has no
    real video, YouTube output, hot switching, SSH-key onboarding, or remote uninstall.

Any failed or undocumented item is a no-go.
