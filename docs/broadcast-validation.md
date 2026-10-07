# Broadcast validation and provenance

## Baseline

Fetched 2026-09-30. main `8136480d22ca7d4fdae82c70b4b34c1d638a9026`;
PR17 `cf84fcdb0d5e58ae07ddca9fd783c0ca3c469d61`, open Draft.
No restack was necessary. A starts at PR17; B starts at A; C starts at B.
PR15/16 native recovery is excluded. STUCK_LIVE_CAUSE=UNRESOLVED;
STALL_SWITCH_CAUSE=UNRESOLVED. Production and HK were never accessed for this work.

Baseline local Python suite: **860 passed, 21 skipped**, 63.13 seconds on Windows,
Python 3.12.13, locked uv 0.11.28. The 21 skips are the existing separate browser
and POSIX-only tests, not an adjustment of expectations. Docker and WSL are absent.
All existing container, resource, SSH, RTMP and security steps remain required in Linux CI.

## Development failures (retained, not rerun-until-green)

- Direct virtualenv Python invocation was interpreted as a script by the host launcher;
  invoking the same locked interpreter through uv works.
- Windows application control rejected the mypy executable launcher. The supported
  `python -m mypy` invocation runs the same installed version and passed after fixes.
- Initial new code had formatting and strict typing errors; corrected locally before CI.
- Playwright WebKit installation reported missing host DLL dependencies. Browser acceptance
  is recorded per engine; installing browser files alone is not a PASS.
- Official documentation browsing returned errors on some pages. The official YouTube
  discovery document was fetched directly without credentials to verify ingest fields.

No production configurations, ingest keys, admin cookies or raw media command lines are
included in evidence. Test fixtures use synthetic credentials and reserved/loopback addresses.

## A local control plane gate

897 Python tests passed; 23 skipped (8 separately enabled browser cases and 15 existing
POSIX-only cases). Ruff format/lint, strict mypy across all existing packages, locked
dependency check, repository safety policy, JavaScript syntax and all 89 existing frontend
tests passed. New control-plane Chromium scenario passed; WebKit failed at launch because
`icutu77.dll`, `libegl.dll`, `sqlite3.dll` are missing on this host. This is not an iPhone PASS.
Linux CI retains the unmodified Docker/resource/media/SSH/security gates and required
Chromium + WebKit job; the new browser scenario runs in that job too.

A final head `0b2197500896f4ec2f56b24d930f76f10ae09efe`: both required CI jobs passed in
[run 36665211255](https://github.com/andreykutenkikh-byte/restream/actions/runs/36665211255).
All four Chromium scenarios also passed locally (163.71 s). Local WebKit remains unavailable.

## B local media gate

902 Python tests passed (23 unchanged category skips); Ruff, strict mypy, repository policy,
boundary checks and the updated Chromium broadcast scenario passed. The dedicated
three-node native-process lab passed with 172/165/165 decoded frames and 165 common frame
hashes. Video packet gap ≤34 ms, audio ≤22 ms, H.264/AAC 1080×1920 at 30 fps, GOP ≤60;
every stream had ≥90 frames and audio packets and passed full decoding and PTS/DTS checks.
Stopping/restarting B and rejecting its destination left source/A/C PIDs unchanged and
A/C advanced 175/174 frames during the failure window. Evidence is in
`broadcast-media-windows-evidence.json`.

Retained media development failures: run 001 used duplicate synthetic node addresses and
hit the existing database uniqueness constraint (fixture fixed); run 002 found the default
MoQ listener collision (disabled on dedicated instances); run 003 proved SRT audio/video
but RTSP publishers rejected `rw_timeout` (changed to the documented RTSP `timeout`).
Separate diagnostic spikes identified these causes. Runs 005 and 006 passed after the
corrections; 006 includes durable agent fencing and per-node direct source credentials.
The release-boundary test also caught an attempted shared middleware prefix edit; that
edit was reverted and an existing unprotected body-limiter is reused instead. No legacy
runtime hash or 90-frame threshold was changed.

## Credential correction and first B CI failure

B [run 36668310606](https://github.com/andreykutenkikh-byte/restream/actions/runs/36668310606)
failed Linux typecheck on Windows-only subprocess.CREATE_NO_WINDOW. The new lab also could
not write its bind-mounted report directory with all capabilities dropped. Chromium and
WebKit passed. Fixes use an optional platform constant and the invoking user's UID/GID;
no privileges were added, no media threshold changed, no failed run rerun.

Migration 10 implements the owner's explicit credential correction. Native lab 007 passed
with leases: 172/165/165 frames, 165 common hashes, A/C advanced 176/174 frames while B failed.
New security tests cover expiry, revocation, removal, cold restart, stale replay, duplicate
intent, and secret-free durable state. A fresh controller plan is required on cold restart.

B run 36669838998 passed all original test/container/media/SSH gates, but its separate
broadcast lab failed before source readiness on Debian FFmpeg 5.1.9. Cause verified in
the upstream n5.1.9 libavutil/parseutils.c: av_find_info_tag does not percent-decode streamid.
The FFmpeg 9 local spike decoded the encoded colons and slashes, masking that difference.
SRT query generation now leaves those grammar separators literal; random URL-safe tokens
stay scoped and encrypted. The failed run and its report remain available. No threshold
or production binary was changed.

B final source `dbc74b30ae538e1efd3ebcc22017aa9e284ee282` passed
[run 36670498556](https://github.com/andreykutenkikh-byte/restream/actions/runs/36670498556):
all original container/SSH/media gates, Chromium/WebKit and the dedicated Linux broadcast lab.
Its base is `0b2197500896f4ec2f56b24d930f76f10ae09efe`; checkout logs prove synthetic merge
`7c2429d8f77a4559fbdd0399b992734cf180b92e`, tested tree
`2fbb0414dec622e99fbce9d923282b606f975119`. Linux lab FFmpeg 5.1.9 recorded 121/172/173
frames, 112 common hashes and A/C advancing 182/179 frames during B failure, unchanged PIDs.
Final local B suite was 905 passed, 23 skipped after the lease correction.

A source `0b2197500896f4ec2f56b24d930f76f10ae09efe`, base
`cf84fcdb0d5e58ae07ddca9fd783c0ca3c469d61`, was tested as synthetic merge
`b8713bd631ffdb0642db1a218795522fc9744b6a`, tree
`58267947857bac3e8eba9a1b19a6696ba5e3b8e1`. No stack commits were rewritten or rebased;
C had uncommitted work while B fixes were added, then fast-forwarded to B's final head.

## C switching gate

The four-node real-media lab, measurement definitions, retained development failures and
physical acceptance boundary are in [route-switch-acceptance.md](route-switch-acceptance.md).
C reuses B's runtime and authority; schema 11 adds operator scope and measured publisher
metadata. Source/merge/tree identity for C belongs in the final PR report so recording it
does not create a new untested commit solely to change its own SHA.

Local suite: 916 passed, 25 skipped (10 separately enabled engine cases and 15 existing
POSIX cases). JavaScript: all 89 tests passed. Ruff, strict mypy for Linux, repository
policy, migration preservation and release-boundary checks passed. A full-suite collection
failure exposed equal unit/integration test basenames; the new unit file was renamed.
The required tests were retained.

Final native C run 005 passed after resource-reservation and partition-deadline fixes:
179/171/171 multicast frames, 171 common hashes, unaffected A/C advancing 231/230 frames;
B→D retained other publisher PIDs and decoded 122 frames. A→B and B→C both measured 0 ms
mock RTMP receiver egress gap (overlap), source progress gap 3406/3406 ms, conservative
phone receiver gap 5188/5250 ms. The independent watchdog stopped a real publisher after
3063 ms on a synthetic 3000 ms grant, removed its runtime key and rejected cold stale replay.
Full local Chromium run 009 passed all five scenarios in 186.13 seconds, including the
standalone beta helper after the diagnosed Windows transport fix. Local WebKit/Docker
remain host limitations; the unchanged mandatory Linux CI jobs cover both.

Final requirement review added selected-output batch controls (individual controls retained)
and deduplicated output/inter-relay/YouTube status events. The updated Chromium batch case
passed, including selection surviving a render and an unchecked output remaining unchanged.
The two added event tests bring the full Python gate to 916 passes. CI run 36674635482's
original test and Chromium/WebKit jobs passed; its diagnosed expiry-test race and unchanged
deadline fix are recorded in the route acceptance document. A new commit, not a blind rerun,
receives the final full CI gate.
