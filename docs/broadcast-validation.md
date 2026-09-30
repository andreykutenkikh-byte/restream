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
