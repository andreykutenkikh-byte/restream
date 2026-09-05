# Read-only production comparison — 2026-09-05

This is evidence for code review, **not deployment approval**. No merge,
deployment, service lifecycle operation, package installation, key rotation,
connection reset, fault injection or agent publication was performed.

## Git starting state

Discovery used refreshed remote refs, PR metadata, CI runs and review comments.
The known starting refs were still current:

| Ref | SHA |
| --- | --- |
| main | `8136480d22ca7d4fdae82c70b4b34c1d638a9026` |
| PR #15 | `27b447b6ce39fa7a84777d52268e85f698a9c924` |
| recovery | `2b3474862766b3b5e3949f0a8857dd71f18cbfd9` |
| PR #16 | `0184ecc5ec6c877fe8fda2954679503e29ded108` |
| HUD old base | `27b447b6ce39fa7a84777d52268e85f698a9c924` |

Both PRs were open Draft. Recovery was a direct child of PR #15's old head.
It was integrated by fast-forward, preserving its SHA and branch; subsequent
PR #15 fixes use ordinary commits and pushes. No duplicate cherry-pick or
force push was used for PR #15.

## Control plane: verified subset

Existing operator-configured SSH-key access reached proxy `147.45.231.225`.
Checkout `/opt/adojapan-restream` was clean at the main SHA above. Both public
health endpoints returned HTTP 200. Three existing containers were running,
healthy, with zero recorded restarts:

| Component | Runtime image ID |
| --- | --- |
| backend | `sha256:27a400451c217d812624085fa48c74d0895c1a3fae1c155883a9035c82a80990` |
| bootstrap | `sha256:0e82f22890baf553c35158f20bf0035cec69d6e81f5b5bf25645ee34071fd6fd` |
| MediaMTX | `sha256:889acc879fb8c2785baed11f6620ce8ba745ab0bda025069483776de84640bbf` |

Runtime versions: app 0.1.0; FastAPI 0.139.0; Uvicorn 0.51.0;
cryptography 46.0.7; control-plane MediaMTX image reference 1.19.2.
Revision labels were absent, so image IDs were not treated as commit attestations.

Read-only SQLite access (`mode=ro`, `query_only`) showed schema version 5 and
one stopped, disabled control-plane destination. The HK relay's current agent
reported version 1.2.5, protocol 1, node ready. The other registered node had
failed installation and no heartbeat. No credential-bearing columns were read.

Selected runtime Git-blob hashes matched checkout for backend `app/main.py`,
`app/db.py`, `app/services/relays.py`, `pyproject.toml`, and `uv.lock`; bootstrap
`bootstrap_worker/api.py`, `bootstrap_worker/installer.py`, and `uv.lock` matched.
Bootstrap runtime `pyproject.toml` differed: blob
`326daac4c9c2fb80f90ae38596c5ee53a9ecbace` instead of
`57813e63452cb42318811834b86464af696d3deb`. The runtime blob exists in earlier
Git history and differs only by omission of `relay_agent*` from setuptools
package discovery. It was recorded, not overwritten. This is a selected-file
comparison, not full image or native runtime attestation.

## Operator stop is not an incident

At 04:00:17 UTC and 04:04:03 UTC fresh HK telemetry reported active service,
running main process, listening SRT, SLATE source and active forward. At
04:04:39 UTC fresh telemetry reported inactive/stopped/closed/NONE/inactive.
The operator explicitly confirmed manually stopping relay. No restart was
attempted and the transition was not attributed to a recovery failure.
Forward telemetry alone does not independently prove a public YouTube broadcast.

## Native relay: RUNTIME_AUDIT_UNVERIFIED

Existing key-based SSH to HK `176.98.181.225` failed directly and via the proxy.
No password was searched for or requested. Installed native paths, release or
manifest, hashes of relayctl/normalizer/renderer/unit files, and native incidents
could not be directly verified. Control-plane telemetry is not a substitute for
those hashes. The checkout SHA does not establish which copied native runtime
files are installed. Future rollout remains unapproved pending that comparison.

No production configuration, secrets, Amnezia, Docker lifecycle, firewall,
network routes or interfaces were changed. All fault-injection/media tests run
only in local tests or disposable GitHub CI fixtures.

## Recovery integration and validation history

The recovery branch was a direct continuation of the original PR #15 head, so
integration preserved commit `2b3474862766b3b5e3949f0a8857dd71f18cbfd9` by
fast-forward. Its normalizer, renderer, relayctl and service-unit content remains
unchanged in the current PR #15 checkout. No runtime behavior was reverted.

The follow-up changes repair test execution and the CI oracle:

- Linux CI's non-root test runner now has a narrowly mocked root-owned fixture;
  tests still reject unsafe ownership, mode, links and file types.
- FFprobe/FFmpeg output pipes are drained together under a whole-process deadline.
  The media-only installer budget is 660 seconds inside the unchanged 900-second
  job limit, based on measured analysis time and bounded remaining subprocesses.
- A 6–8 second disconnect requires the exact publisher, continuous counter
  evidence and fresh ordered reset events; the natural 8–13 second path remains.
- Supervisor-crash checks can reuse fresh delivery evidence already observed by
  the preceding gate for the same crash. Both the proof and current state must
  remain fresh; stale, late, wrong-identity or regressed evidence is rejected.
  The 12-second supervisor and 15-second output recovery bounds are unchanged.
- Lifecycle verification derives the installed release from the installer
  constant. It verifies the current no-reset-on-FFmpeg-failure contract,
  proof-matched persistent resets, the ordered delivery ledger, strict final
  RTMP media segments, credential checks and cleanup.
- Diagnostics expose fixed stage/reason codes, bounded elapsed times, optional
  boolean predicates and self-test line numbers. They do not print full result
  JSON, transport URLs, publisher identities or exception text.

At commit `fe977c4eb439aadb990d273a078ccc067343896c`,
[CI run 33949185465](https://github.com/andreykutenkikh-byte/restream/actions/runs/33949185465)
passed all 1249 Linux Python tests, real RTMP/preview checks, native bootstrap,
the actual native media self-test and fresh heartbeat readiness. The self-test
reached final cleanup at 376.871 seconds. The run then failed its separate
lifecycle oracle: the first shell comparison expected release `2026.09.04.1`
instead of the installer's `2026.09.05.1`. Further stale assertions in that
oracle also contradicted the current recovery report. Overall CI was **failure**,
not success; post-onboarding runtime-limit checks did not run, while cleanup did.

The corrected lifecycle oracle is in
`f83ba125b570e34d362ee184ddc550ef77fa784c`. Local verification on this head:
1302 Python tests passed, 19 Windows/POSIX-only skips; one existing Starlette
deprecation warning. Lock, locked sync, installed dependency compatibility,
format, lint, type checking and repository policy passed. Frontend tests (44),
JavaScript syntax (4 files) and shell syntax (15 files) passed on unchanged
frontend/shell sources. Exact-head
[CI run 33950361321](https://github.com/andreykutenkikh-byte/restream/actions/runs/33950361321)
completed **SUCCESS**, independently verified from both the run and every job step.
Native media self-test completed in 370.677 seconds; public bootstrap, heartbeat,
credential isolation, password non-persistence, revoke, post-onboarding runtime limits
and cleanup all passed. PR #15 stays Draft. This is the recorded `PR15_GREEN_HEAD` and
`PR15_GREEN_RUN`, not approval to merge or deploy.

## HUD baseline, before corrections

Before rebasing PR #16, a real isolated browser launch reproduced the old defect: Chrome
152.0.7977.76 loaded the real `/moblin-hud` over loopback HTTPS (HTTP 200), then
ordinary script execution raised `exports.initializeHud is not a function`.
Neither pairing nor status polling started. This is a verified failing baseline,
not browser acceptance. The temporary app/browser processes were stopped and
the temporary database/certificate were removed. No production or Moblin change
was made.

## HUD stack update

Only after the complete PR #15 success, remote refs and worktree cleanliness were
checked again. The HUD old base was
`27b447b6ce39fa7a84777d52268e85f698a9c924`; both old local and remote HUD heads were
`0184ecc5ec6c877fe8fda2954679503e29ded108`. Exactly five HUD commits were rebased onto
`f83ba125b570e34d362ee184ddc550ef77fa784c`, without conflicts. The rebased stack head was
`813c34b4b267fb8490f5d1050d6ecab1a5e1e19f`. `git range-diff` marked all five commits
semantically unchanged. Recovery was neither duplicated nor removed from the stack.

## HUD corrections and regression coverage

The ordinary-script factory now exports its initializer; a document-owned instance
prevents duplicate initialization across repeated script evaluations. An authenticated
HUD page exposes only a boolean and can reopen an already-consumed pairing link without
replaying its one-time token. Page suspension is separate from terminal revoke/logout;
polling resumes on pageshow and retains the request slot until aborted work settles.

The evaluator retains the last confirmed active route across source loss. It distinguishes
initial SLATE (waiting), LIVE, source loss with running relay, coherent native stop,
unknown telemetry, actual process failure and multiple-LIVE ambiguity. Direct main-server
ingest status is not misrepresented as independent service/process/listener telemetry.
Missing input alone cannot establish an intentional stop on either path.

Recovery grace is 120 seconds, derived from the existing bounded native timings; see
[the HUD contract](moblin-streamer-hud.md#source-loss-recovery-and-operator-stop).
Within grace, input loss remains visible but requests observation, not premature manual
switching. Afterwards fresh persistent loss requests reconnect or a ready standby.
Restored positive LIVE cancels stale loss recommendations. Missing heartbeat and HUD
API errors never prove that a YouTube broadcast ended. No recovery phase/exhaustion
telemetry or agent protocol was invented. Standby confidence remains server readiness,
not a measured phone-to-target route; switching remains manual.

Audio requires a user gesture, covers direct severity jumps, respects mute, and allows
stronger escalation through a weaker warning's cooldown. First render, unknown-to-green,
unchanged levels and recovery stay silent. The HUD remains scoped read-only, without
video/HLS preview, external CDN, browser secret storage or streaming credentials.

Regression locations cover the requested twenty cases:

| Cases | Checks |
| --- | --- |
| 1–5: waiting, source loss, coherent stop, heartbeat uncertainty | `tests/integration/test_moblin_hud_api.py` initial/source-loss/stop/stale tests |
| 6–10: brief recovery, persistent grace, reserve/no-reserve, restored LIVE | API recovery and parametrized persistent-loss tests; real browser loss/recovery flow |
| 11–15: ambiguity, restart, duplicate heartbeat, standby isolation, elapsed thresholds | API ambiguity/restart/sample/time tests and `tests/unit/test_relay_quality.py` |
| 16: monitoring API failure | Actual browser request fault/retry plus frontend poller tests |
| 17–18: audio jumps and silent initial/recovery state | `tests/frontend/moblin-hud.test.js` transition and user-gesture tests |
| 19–20: consumed-link cookie reuse and suspended-page return | Actual HTTPS browser session plus frontend document/lifecycle tests |

The SQLite v6→v7 migration test populates all fourteen legacy tables, including encrypted
placeholders and administrative/runtime records. It verifies exact legacy rows, schemas
and sequence state after each of two migration executions, plus foreign-key and integrity
checks. HUD pairing/session persistence remains digest-only and isolated from admin auth.

## Actual browser evidence and final gate

Local validation includes locked dependency synchronization and installed-package
compatibility, Ruff format/lint, mypy, the complete Python suite, repository safety,
79 frontend tests, syntax checks for six JavaScript and fifteen shell files, and four
Compose configuration variants plus the production-model policy with synthetic values.
The focused native recovery/bundle/probe/installer suite passed 431 tests (three
POSIX-only cases run in Linux CI). The Windows full suite's POSIX skips and two default
browser skips are not acceptance substitutes: Linux CI and the required browser job
must run those paths. No local Docker daemon or production service was started.

The local green run used Chrome 152.0.7977.76 against the real isolated HTTPS application,
not a CommonJS-only test. Ordinary HTML/script loading, pairing, cookie establishment,
duplicate-script identity, LIVE/SLATE/NONE, grace expiry, LIVE return, consumed-link reuse,
persisted lifecycle events and real back-navigation, held-request overlap prevention,
network retry and revoke passed. Maximum simultaneous status requests was one. No page
errors occurred. Only resource errors from the explicitly injected network/revoke fault
windows were expected; normal flow had no console errors. Captured real access logs and
request URLs contained no fixture tokens. Temporary app/browser processes were stopped.

The required `hud-browser` CI job independently runs the locked Python Playwright fixture
in both Chromium and WebKit; missing dependencies/engines fail that job. Browser testing
is separate from the unchanged required media/preview/security job, not a replacement.
No session-bearing traces are published. Desktop WebKit is not physical iPhone/Moblin
acceptance. Local Chrome proof alone does not establish final CI success.

The PR descriptions record the final exact HEADs, CI run URLs and results. A final-review
declaration requires both complete exact-head runs to succeed, including native media
and both browser engines. Both PRs stay Draft, and even full code CI does not remove the
native runtime audit limitation or authorize deployment. No merge, deployment, agent
publication or production/runtime mutation is part of this work.
