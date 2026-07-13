# Production audit gate

This runbook is documentation for a future, separately approved production stage. Do not
execute it as part of Stage 1.

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

1. A fresh key-only SSH session for the deployment account succeeds before any SSH/security
   change, and password authentication is not used.
2. The legacy n8n Traefik service remains excluded from the default n8n Compose model, has
   restart policy `no`, and publishes neither 80 nor 443; the existing Nginx continues to own
   those ports.
3. Existing container IDs, start times, restart counts, health, and the Nginx/Docker process
   identities match the saved baseline before Restream starts.
4. TCP 1935 and loopback TCP 8088 are still free, and resource/OOM checks still support the
   bounded profile.
5. No Restream production directory, production `.env`, container, image, network, or volume
   exists before the explicitly approved deployment step.
6. The DNS-owner decision and host/provider firewall plan are approved; either unresolved item
   keeps the deployment at NO-GO.

## Shared-host production profile

Production must load `compose.production.yml` after `compose.yml` for validation and every
lifecycle command. The effective limits must be exactly:

| Service | CPU | RAM | PIDs | Destinations |
| --- | ---: | ---: | ---: | ---: |
| backend | 0.40 | 384 MiB | 96 | 1 |
| MediaMTX | 0.20 | 192 MiB | 64 | n/a |
| **Total** | **0.60** | **576 MiB** | **160** | **1** |

The override must also make the security mode and public identity fail-closed. Its effective
values are `ENVIRONMENT=production`, `COOKIE_SECURE=true`, `MAX_DESTINATIONS=1`,
`PUBLIC_DOMAIN=restream.adojapan.ru`, `PUBLIC_RTMP_HOST=restream.adojapan.ru`, and
`PUBLIC_RTMP_PORT=1935`. These fixed values are appropriate because this project has one defined
public identity; a production `.env` cannot replace them with development mode, insecure cookies,
or `localhost`. `SESSION_SECRET` and `WORKER_AUTH_PASSWORD` remain independently supplied secrets
and must not be reused.

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
allowlist and isolated receiver, and must never enter a production lifecycle command.

The run must safely confirm the actual backend limits of 0.40 CPU, 384 MiB, and 96 PIDs and the
MediaMTX limits of 0.20 CPU, 192 MiB, and 64 PIDs without printing container environments. The API
test must keep the first destination active while a second is rejected with
`409 destination_limit_reached`, then complete the synthetic media path and cleanup. Even a green
run is test evidence only: it does not deploy the production model, authorize deployment, or
close the DNS and firewall NO-GO gates below.

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
substitute for this audit.

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

Never use real platform keys for the audit. Record findings and obtain approval before the
production change.

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
23. Confirm no OOM event occurred.
24. Stop only this project and confirm other services remain healthy.
25. Record the reviewed rollback command and responsible operator.

Any failed or undocumented item is a no-go.
