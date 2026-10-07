# Route switch acceptance

Only synthetic local/CI fixtures are authorized. Real YouTube, physical iPhone,
production/HK mutation, deployment and merge were not performed.

## Reproducible laboratory

`python -m scripts.broadcast_switch_lab --mediamtx <binary> --ffmpeg <binary>
--ffprobe <binary> --directory <fresh-directory>` runs four isolated native agents,
their dedicated MediaMTX instances, an in-process real SQLite control plane/controller,
one synthetic SRT phone and authenticated loopback RTMP destination receivers.
HTTP/browser/authentication contracts are separate HTTPS integration/browser tests.

`Dockerfile.broadcast-lab` runs the same C acceptance module on Linux. The required
`broadcast-media` CI job uses no external network, all capabilities dropped, the invoking
UID/GID, two CPUs, 3 GiB memory and 256 PIDs. All original test/container/SSH/media gates
remain required, with Chromium and WebKit in the separate browser job.

The source is H.264/AAC portrait 1080×1920 at 30 fps and GOP 60. A → B/C transport is
encrypted SRT and publishers copy independently. Each six-second capture must retain
at least 90 video frames and audio packets, strict monotonic PTS/DTS, codec/profile/FPS,
GOP ≤60, packet gaps below 100 ms, full error-free decoding and at least 90 common
decoded frame hashes for multicast. These existing thresholds were not reduced.

After the three-output isolation check, output B moves B → D with only credential B.
The source and A/C publisher PIDs stay unchanged. Then A's single logical output switches
A → B (BACKUP) → C (PRIMARY), with a single phone process moved between inputs.
Both old and new publishers are measured before each cutover. The target has no key
before its media proof. Nonempty synthetic stream/broadcast IDs and the encrypted binding
remain unchanged. No YouTube API resources are created by this lab.

## Measurement meaning

`youtube_egress_switch_gap_ms` is the positive difference between first advancing target
RTMP receiver bytes and last advancing old receiver bytes, sampled independently every
50 ms, plus decoded-media validation. Zero means receiver overlap in this lab. It is not
a YouTube player measurement or a guarantee about remote viewer latency.

Historical PR20 `phone_direct_takeover_gap_ms` measures the last old/first new received video packet using
persistent ffprobe observers across the controlled publisher replacement. It conservatively
includes ffprobe startup/output buffering. `source_switch_gap_ms` measures the last old/
first new FFmpeg publisher progress. `phone_stop_to_new_receiver_ms` also includes source
reconnect, discovery and direct-source qualification before replacement. These clocks and
sampling points are deliberately distinguished. The lab's reconnect budget is 30 seconds;
it is not a product SLA, and no physical phone was used.

The handoff candidate retains the original media, rollback, isolation and lease gates,
but preserves the publisher and therefore the existing receiver. The original lab now
asserts both identities remain unchanged. It delegates seam latency to the additional
continuous **decoded video and audio** lab; the historical ffprobe startup metric is not
reused as a current handoff result. `source_switch_gap_ms` now measures the selector's
last video write to selection, not FFmpeg process restart progress. See
[handoff acceptance and limitations](seamless-source-handoff.md).

Latest committed native evidence is [broadcast-switch-windows-evidence.json](broadcast-switch-windows-evidence.json).
The final PR description/report identifies the corresponding source and CI tested tree.
CI uploads only a secret-free report; diagnostic stream URLs, FFmpeg argv and raw stderr
are never attached. Local Windows uses FFmpeg 9.0.2; Linux uses the image's actual apt
version recorded in each report. MediaMTX is pinned to 1.19.2.

## Failure and security matrix

| Scenario | Evidence and required behavior |
| --- | --- |
| One multicast output stopped/restarted or rejected | Native/CI: source and unrelated publishers retain PIDs and advance frames |
| B moved to D | Native/CI: credential B follows the output, no credential A/C on D, A/C uninterrupted |
| Target RTMP connection fails before cutover | Native/CI: rollback removes target key; old publisher stays alive |
| Controller crash after target lease | Native/CI: discard controller, wait durable owner lease, reconcile, never third lease |
| Target crash/negative telemetry after lease | Unit: rollback retains old; target slot waits stop acknowledgment/deadline |
| Old node unreachable at revoke | Unit: slot reserved until latest grant expires plus skew allowance; stale stop cannot free it early |
| Controller unavailable during active stream | Native/CI: publisher advances within existing lease without heartbeat |
| Lease expiry with no tick/heartbeat | Native/CI: real watchdog stops actual FFmpeg, clears destination/argv; synthetic shortened lease only |
| Cold relay restart with old fence/cache | Native/CI and unit: no local credential cache, stale envelope rejected |
| Old generation after cutover/reconnect | Native/CI: rejected, old publisher cannot resurrect |
| Duplicate switch/intent/plan | Concurrent unit/API/browser requests: one durable operation, no second publisher |
| Wrong node/output, expired/revoked grant | Unit/integration: authenticated scope, envelope binding and fencing reject |
| Secret persistence/exposure | Unit/API/browser: encrypted DB, digest-only tokens, no raw key in public status/heartbeat/audit/config/report |
| Resource contention | Unit: warm routes reserve publishers; publishing plus forwarding share the node egress budget |
| Operator/monitor isolation | HTTPS browser/API: explicit grant, one-use pairing, CSRF/origin, session scope, expiry/revoke, no admin access |
| Browser failure/reload | Chromium/WebKit gate: pending operation reload, double click, rollback, network UNKNOWN/recovery, mobile operator layout |
| Real YouTube viewer/chat/online continuity | NOT_RUN; see staged plan |
| Standard Moblin active LIVE handoff on iPhone | NOT_RUN; settings-source review does not certify device behavior |

## Retained development failures

Native switch 001 failed an egress overlap assertion that used ffprobe's first delivered
packet as the RTMP connection timestamp. Instrumentation showed analysis/buffering delay;
the receiver byte-progress observer now measures the actual ingress boundary independently.
The old observer metric remains in evidence. Neither the zero egress-overlap requirement
nor the media thresholds changed. Runs 002–005 passed; 004 added actual expiry/cold-restart,
and final 005 exercised resource reservation and the old-partition deadline correction.

Browser development runs 002–006 passed their functional assertions but failed Windows
HTTPS teardown. Diagnostic capture found CPython 3.12 Proactor `ConnectionResetError`
before server detach, with zero Uvicorn requests/tasks and nine unclosed transports.
The new test uses Uvicorn's supported selector-loop factory on Windows only and retains
the original ten-second shutdown assertion. Timeout and event-loop-policy guesses were
removed. The same explicit Windows-only loop selection is used by the shared HTTPS fixture
and standalone beta helper after run 008 reproduced the identical TLS-reset failure there.
All of run 008's browser assertions passed, but the beta process failed its final exit check.
No original assertion, deadline or Linux engine gate was weakened.

Full Chromium run 007 also exposed a pre-pairing request by the new read-only extension.
The extension now waits for authenticated HUD state and is absent when there are no
broadcast sessions. The same run's standalone legacy beta hit the Windows TLS reset and
RemoteProtocolError in its heartbeat helper; it is retained as a failure, not hidden.
Final Chromium run 009 passed all five scenarios after that transport fix and the monitor
pairing correction. Final gate provenance and Linux results are recorded in the PR/report.

C CI run 36674635482 passed multicast, B→D, both full switches, rollback and controller
restart, then failed the expiry test's immediate argv assertion. `process.poll()` can see
the child exit before the watchdog thread finishes its close/argv cleanup. The test now
waits for both process exit and cleanup within its unchanged seven-second deadline; no
media or expiry threshold changed. Its Linux measurements were 0/0 ms egress overlap,
9709.3/9698.3 ms publisher source gap and 11310.1/11296.9 ms conservative phone receiver gap.
They are longer than the Windows spike and are retained as such.

## Physical acceptance still required

Use the separately reviewed [staged deployment plan](staged-broadcast-deployment.md).
Record actual iPhone/Moblin/iOS versions, carrier/Wi-Fi paths, network congestion,
direct and forwarded video/audio continuity, UI permission lifecycle, and a real unlisted
YouTube viewer timeline. Same synthetic IDs do not prove real chat, viewers or page survival.
