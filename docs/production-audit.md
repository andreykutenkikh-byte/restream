# Production audit gate

This runbook is documentation for a future, separately approved production stage. Do not
execute it as part of Stage 1.

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
5. One temporary local RTMP/RTMPS output receives the stream.
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
20. Run a short synthetic RTMP ingest test.
21. Run one short synthetic RTMP/RTMPS output test without a real platform key.
22. Capture CPU and RAM during the media test.
23. Confirm no OOM event occurred.
24. Stop only this project and confirm other services remain healthy.
25. Record the reviewed rollback command and responsible operator.

Any failed or undocumented item is a no-go.
