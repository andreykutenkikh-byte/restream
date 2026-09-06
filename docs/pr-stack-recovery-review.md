# Recovery stack verification — 2026-09-05/06

This is evidence for code review, **not deployment approval**. September 5
observations are historical; September 6 updates are identified separately.
The current completion pass performs no PR merge, production deployment,
service lifecycle operation, production package installation, key rotation,
connection reset, production fault injection or agent publication. Earlier
separately authorized runtime work is not attributed to this completion pass.

## September 5 Git starting state

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

## September 5 control plane: verified subset

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
one stopped, disabled control-plane destination. The HK relay's agent then
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

## September 5 operator stop is not an incident

At 04:00:17 UTC and 04:04:03 UTC fresh HK telemetry reported active service,
running main process, listening SRT, SLATE source and active forward. At
04:04:39 UTC fresh telemetry reported inactive/stopped/closed/NONE/inactive.
The operator explicitly confirmed manually stopping relay. No restart was
attempted and the transition was not attributed to a recovery failure.
Forward telemetry alone does not independently prove a public YouTube broadcast.

## September 5 native relay audit: RUNTIME_AUDIT_UNVERIFIED

Existing key-based SSH to HK `176.98.181.225` failed directly and via the proxy.
No password was searched for or requested. Installed native paths, release or
manifest, hashes of relayctl/normalizer/renderer/unit files, and native incidents
could not be directly verified. Control-plane telemetry is not a substitute for
those hashes. The checkout SHA does not establish which copied native runtime
files are installed. Rollout was not approved by this incomplete comparison.

No production configuration, secrets, Amnezia, Docker lifecycle, firewall,
network routes or interfaces were changed. All fault-injection/media tests run
only in local tests or disposable GitHub CI fixtures.

## September 6 read-only runtime comparison

Existing key-only access directly reached HK. Eight selected nonsensitive
installed files—relayctl, normalizer, renderer, relay unit, broker, history module,
broker unit and tmpfiles definition—matched deployed `5669278` and merged PR15
`243d8418` exactly. [The runtime inventory](runtime-audit-20260906.json) records
their paths, SHA256/Git-blob hashes and ownership. MediaMTX was v1.20.1, FFmpeg
4.4.2 and Python 3.10.12. At 04:04 UTC the relay was active/enabled, on SLATE,
with active forward and health PASS. This is server-side state, not proof of
viewer delivery or of why Moblin input was absent.

The release marker was unavailable; the existing manifest returned no selected
public version field. File hashes provide the selected runtime comparison;
they are not a complete filesystem attestation. Current control-plane internal
inventory remains unverified because the explicitly designated SSH key was
rejected. Public live/ready health both returned HTTP 200. The September 5
checkout/image inventory and known bootstrap pyproject packaging drift remain
historical evidence only. No production file, service, stream or credentials
were changed by this audit, and it does not authorize deployment.

## Recovery integration and validation history

The recovery branch was a direct continuation of the original PR #15 head, so
integration preserved commit `2b3474862766b3b5e3949f0a8857dd71f18cbfd9` by
fast-forward. At the September 5 PR15 head `bf6f021`, its normalizer, renderer,
relayctl and service-unit content still matched the original recovery commit.
The September 6 integration below preserves the later deployed runtime fixes.

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
oracle also contradicted that head's recovery report. Overall CI was **failure**,
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
and cleanup all passed. PR #15 stays Draft. This was the first recorded green base,
not approval to merge or deploy.

Two later September 5 PR #16 runs, on unchanged native runtime, exposed intermittent media
failures: run 33952161676 timed out while reading a strict 90-frame final RTMP
segment after LIVE delivery was confirmed; run 33952428440 did not regain the
normalized LIVE publisher within the existing supervisor-crash recovery deadline,
while an SRT publisher remained present and downstream SLATE media grew. Publisher
presence does not establish growth of input media bytes. The surviving logs do
not determine the exclusive cause of either failure, and the original media
artifacts are unavailable. No assertion, recovery deadline or security check was
relaxed to turn these failures into passes.

PR #15 follow-up `bf6f0216c10784f2c9073bff7d563bf002fc215b` adds diagnostic evidence
only: event-scoped fixed normalizer markers, bounded process counts/first-seen
times, and reader input/output/frame progress. Raw logs, exception text, URLs,
identities and credentials are excluded. The root-owned, single-link, no-follow
checkpoint and strict consumer validation remain enforced. Normal runtime code,
recovery timings and strict media assertions are unchanged.

This exact head's [CI run 33953963213](https://github.com/andreykutenkikh-byte/restream/actions/runs/33953963213)
completed **SUCCESS**, including native self-test cleanup at 389.476 seconds,
post-onboarding limits and CI fixture cleanup. Its local full suite passed 1370
tests with 19 Windows/POSIX skips; dependency, format/lint/type, frontend, syntax
and Compose policy gates also passed. This was the last recorded September 5
`PR15_GREEN_HEAD` / run.
A successful diagnostic run does **not** establish that the preceding
intermittency is fixed; this remains an explicit final-review limitation.

## September 6 integration, fixture repairs and remaining blocker

Merge `243d8418fe0969f0f27b5ed0270731d6f45ad65d` integrates the deployed
recovery/observability history while preserving PR15's CI fixes. The new native
bundle includes `history.py`, required by the broker import; an internal dependency
closure check prevents another incomplete fresh installation. The runtime keeps
the 2/2.5/2-second watchdog thresholds, carried-forward six-second exact-source
stall proof, protected recovery credentials and bounded local history.

A separate reproducible defect was found in the synthetic MPEG-TS feeder clock:
it rebased on every late scheduler wakeup and accumulated sub-packet lateness.
The corrected loop preserves that phase and reanchors after a complete packet
interval is missed or after an intentional pause, without a catch-up burst.
Four deterministic tests exercise the real feeder loop. This proves the fixture
defect and repair, not exclusive causality of the historical native failures.
The old strict reader discarded both pipes, so its timeout did not demonstrate
pipe backpressure. Supervisor, output/reader deadlines and strict assertions
were not relaxed.

The separate real-media clock check captures complete finite MPEG-TS and verifies
byte identity, ordering, portrait-video PTS and cleanup. Complete capture avoided
terminal-PES truncation but did not alone fix the source-baseline failure.
Diagnostic `68a1514` exposed `non-existing SPS 0 referenced in buffering period`:
FFmpeg 5.1's automatic Annex-B conversion placed SPS/PPS at IDR after the SEI that
referenced SPS. The helper-only `h264_mp4toannexb,dump_extra=freq=keyframe` filter
puts key-packet extradata before that SEI without reencoding, dropping NALs or
suppressing errors. Docker live-tmpfs staging uses exec stdin and verifies the
written bytes. These changes are isolated fixture corrections. See the upstream
[FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-bitstream-filters.html#dump_005fextra)
and [Docker copy limitations](https://docs.docker.com/reference/cli/docker/container/cp/#corner-cases).

Despite those repairs, PR15 `71c78132553b5bb6ff64e9c8187f565e5e6a6eae`
[run 34012815959](https://github.com/andreykutenkikh-byte/restream/actions/runs/34012815959)
failed native E2E after 173.598 seconds: the strict recovered-LIVE RTMP reader
last reported 71 frames against a 90-frame target before its 15-second timeout following a
forced bridge kill. The source/output health predicates passed. The recorded
`reset-slate` stage was stale: SLATE and subsequent LIVE recovery had already
passed. Periodic progress is not an exact count of frames in the deleted partial
capture. This is a newly observed unresolved capture failure, not only an old-log
limitation, and the pacing repair cannot be claimed to have fixed it.

Follow-up `df9307f9bd07ac69e67e2f79822bf70d512d4e97` adds typed numeric reader
startup/progress evidence and the accurate `reset-live` stage. Its full
[CI run 34013753666](https://github.com/andreykutenkikh-byte/restream/actions/runs/34013753666)
passed: 1556 Python tests, 44 frontend tests, real media/security/native checks
and cleanup. The real clock comparison measured old/fixed rates 0.739/0.991;
all thirteen strict captures reached 90 frames in 3.477–5.081 seconds. This is
the first verified September 6 `PR15_GREEN_HEAD` / run. The follow-up is diagnostic;
its successful run does not establish the cause or resolution of the preceding
71-frame timeout. That failure remains a blocker to an unconditional
`PR15_AND_PR16_READY_FOR_FINAL_REVIEW` declaration.

### Second September 6 media failure and fault-injection correction

Restacked HUD head `e75c5795267548ebdf50ef255efb34638228afa4` passed its actual
Chromium/WebKit browser job (four tests, 19.61 seconds), 1647 Linux Python tests
and 87 frontend tests in
[run 34014962860](https://github.com/andreykutenkikh-byte/restream/actions/runs/34014962860).
The complete run nevertheless failed: native `outage-normal` at 241.44 seconds,
trace 6043 → 4825 → 1293, was the source-stop → SLATE predicate missing its
unchanged 4.5-second deadline. Last predicate values were not retained. This is
distinct from the earlier 90-frame reader timeout. CI cleanup passed; the
post-onboarding limits step was skipped, not accepted.

Review found a concrete fault-injection ordering flaw: the outage clock started
before waiting for feeder cleanup, while the independent SRT helper could still
drain buffered media. PR15 `ce7f33598c9d6b5129a4a15d2b8e885559ab72bf` cuts and
verifies the actual SRT sender first, then performs feeder/publisher cleanup.
Five deterministic tests execute the actual nested shutdown function, comparing
old/new order with modeled 30 ms, 500 ms and 1.5 s cleanup delays and preserving
owned processes on failure. This proves the ordering flaw, not its unobserved
duration in that failed CI run.

The same follow-up records existing allowlisted predicate flags on generic
observer timeouts. It caches the actual predicate result without re-evaluating
stateful predicates; incomplete/stale observations cannot fabricate success,
and diagnostic failure cannot replace the original exception. Eight new cases
verify these boundaries and secret-free checkpoint projection. No runtime file,
4.5/12/15-second deadline, same-SRT requirement, 8–13-second expiry rule or
strict media assertion was weakened.

This new exact PR15 head passed
[full CI 34015805255](https://github.com/andreykutenkikh-byte/restream/actions/runs/34015805255):
1569 Linux Python tests, 44 frontend tests, real clock rates old/fixed
0.743/0.993, native cleanup at 350.339 seconds and all thirteen strict captures
at 90 frames in 3.322–5.126 seconds. Post-onboarding limits and cleanup passed.
It is the latest verified PR15 base. A complete green run verifies that head's
coverage; it does not retrospectively prove the cause of every prior timeout.

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

After HUD corrections, exact head `9e00caede3f6ab7a78290d34428aed2f240ce8ce`
passed full [CI run 33952998824](https://github.com/andreykutenkikh-byte/restream/actions/runs/33952998824):
1412 Linux Python tests, both real browser engines, media/preview/security and
native E2E (cleanup at 344.297 seconds). Only after the diagnostic PR #15 follow-up
also passed its full CI, a second clean/fresh-ref check preceded restacking all
eight own HUD commits from base `f83ba125b570e34d362ee184ddc550ef77fa784c` onto
`bf6f0216c10784f2c9073bff7d563bf002fc215b`. All eight range-diff entries were equal;
the restacked head before this evidence update was
`439c523b736fbad68544d313672ed32e9e1050c4`. The expected remote lease was the full
`9e00cae...` SHA above. The final September 5 head `5fb7b584ed17f03e352e8352137010f638e5b679`
then passed [CI run 33954755755](https://github.com/andreykutenkikh-byte/restream/actions/runs/33954755755),
including both browser engines and native E2E. That run is historical evidence.

After the full September 6 PR15 success above, nine own HUD commits were rebased
from saved base `bf6f0216c10784f2c9073bff7d563bf002fc215b` onto
`df9307f9bd07ac69e67e2f79822bf70d512d4e97`. All nine range-diff entries were equal;
the rebased head before this documentation update was
`7f291bd0662552726977be9057aa9f0d0bc69b76`. The saved local and remote HUD head,
and required explicit push lease, are `5fb7b584ed17f03e352e8352137010f638e5b679`.
The final new HUD exact HEAD must pass its own full CI; earlier green runs do
not validate this new stack. The PR description records its final SHA and run.

After the second PR15 full success, all ten own HUD commits (including the
logout correction) were rebased from `df9307f9bd07ac69e67e2f79822bf70d512d4e97`
onto `ce7f33598c9d6b5129a4a15d2b8e885559ab72bf`. All ten range-diff entries were
equal; pre-documentation head `9dec449363854ac1cdfdf6628a7f261c0f2ff7ab` has
identical native/agent/bootstrap content to that base. The explicit expected
remote lease is `e75c5795267548ebdf50ef255efb34638228afa4`. Final exact-head
results are recorded in the PR description after completion, with no reuse of
the failed old-head run as acceptance.

## HUD corrections and regression coverage

The ordinary-script factory now exports its initializer; a document-owned instance
prevents duplicate initialization across repeated script evaluations. An authenticated
HUD page exposes only a boolean and can reopen an already-consumed pairing link without
replaying its one-time token. Page suspension is separate from terminal revoke/logout;
polling resumes on pageshow and retains the request slot until aborted work settles.

The September 6 review also reproduced false logout success when its HTTP request
failed. The corrected page hides local metrics and pauses polling immediately,
but enters terminal revoked state only after server success or HTTP 401. Failed
requests remain visibly unconfirmed and retryable with one in-flight request and
the existing request deadline. Frontend regressions cover HTTP/network failures,
confirmation, duplicate clicks, timeout and a pending pairing response that must
not overtake logout. A new actual-browser case checks an
aborted logout leaves the server session valid, then confirms successful retry,
cookie removal and subsequent HTTP 401. Its two-engine result must come from the
final exact-head CI, not the earlier browser run.

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
The existing v5→v6 classification test now also removes v7 tables and its migration
marker before starting: it genuinely begins at schema 5, upgrades twice to the
current schema, preserves node/job classifications and checks empty HUD tables,
foreign keys and database integrity. Previously deleting only marker 6 left a
misleading v7 fixture after the HUD rebase.
An independent isolated check also built an actual schema-5 database using the
historical pre-v6 `app/db.py`, populated twelve legacy tables with nineteen
synthetic rows, and migrated it twice with the current code. Every legacy
column/value, sequence and old migration marker survived; new classifications,
empty HUD tables, foreign keys and integrity all passed. Its temporary database
was removed; no production database was opened for migration.

## September 5 browser evidence and final gate requirements

September 5 local validation included locked dependency synchronization and installed-package
compatibility, Ruff format/lint, mypy, the complete Python suite, repository safety,
81 frontend tests, syntax checks for six JavaScript and fifteen shell files, and four
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

The first PR #16 run on `fd6697cb19433bd985e8c4596be24c43bec5a44e`
installed Chromium 151.0.7922.34 and WebKit 26.5 and passed actual app initialization,
pairing, session checks and LIVE polling in both engines. Both then exposed the same
test-harness error: a bare-string `wait_for_function` predicate used JavaScript eval,
which the application's `script-src 'self'` CSP correctly rejected. The fixture now
uses explicit function predicates throughout. CSP was neither bypassed nor weakened;
all browser assertions and both required engines remain. A regression checks this
test contract. That original failed run is not accepted as final browser success.

The next browser run reached source-loss/recovery and the held-request checks, then
found that reopening the saved pairing link could be a same-document fragment navigation.
No script reinitialization occurs on that path. A hashchange handler now erases the
fragment and reuses the confirmed session without token replay or another controller.
The real fixture separately checks same-document and full-page cookie reuse. Revoked
or unconfirmed documents cannot gain a session from a fragment event alone. URL-failure
assertions expose only booleans, not temporary fixture token values.

The corrected pre-restack exact-head browser job
[101271120529](https://github.com/andreykutenkikh-byte/restream/actions/runs/33952998824/job/101271120529)
passed both engines (2 tests, 17.45 seconds): Chromium 151.0.7922.34 and WebKit
26.5. This is actual browser evidence, not physical iPhone acceptance, and is
not substituted for the required final restacked-head CI.

The PR descriptions record the final exact HEADs, CI run URLs and results. A final-review
declaration requires both complete exact-head runs to succeed, including native media
and both browser engines, and the documented native capture blocker to be resolved.
Both PRs stay Draft. Full code CI does not remove the current runtime-audit scope
and limitations or authorize deployment. No PR merge, deployment, agent publication
or production/runtime mutation is part of this completion pass.
