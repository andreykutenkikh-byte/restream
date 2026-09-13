# Paused ingress / stall-switch investigation

Scope: existing PR15/PR16, isolated local/CI work only. No production or HK
connection, deployment, merge, image publication, or changes to HUD/installer/DR.

## Finding and attribution boundary

The original diagnostic failed before its first-child crash target was armed:
`ORIGINAL_PREFIX_FAILURE`, `stage=stall-switch`, `target_armed=false`.
The exact waiting predicate requires no normalized publisher, no LIVE, the
original still-live SRT ingest identity, and a ready output path. Its last
evaluated sample still had `normalized=true` and `live=true`; the core, ingest,
metrics and downstream flags were healthy. The reported `8.029 s` is waiter
completion, **not the actual switch time**.

A concrete observer defect is reproduced deterministically: the old waiter
evaluates LIVE at 7.979 s, a complete SLATE sample is published at 7.990 s, then
the fixed 50 ms sleep returns at 8.029 s. The loop exits without evaluating that
timely sample. The frozen pre-fix method is retained in
`tests/unit/test_native_observer_deadline_handoff.py`; it does not depend on Git
being available during tests.

The original job did not save sample publication/receipt times, so this mechanism
is **not proven to be the cause of that historical failure**. A green repetition
must not be described as proof that the historical failure is fixed.

## Minimal observer correction

`Observer` now notifies an Event after publishing a sample under its existing
lock. The sole waiter clears the Event **before** reading the snapshot, then
waits for a publication or the smaller of 50 ms and the unchanged remaining
deadline. Publication before clear is visible in the subsequent snapshot;
publication after clear wakes the waiter. There is no post-deadline recheck or
backdating. Success also requires predicate evaluation to finish before the
original deadline.

This removes the proven polling handoff race/delay. It does not change normalizer
policy or promise that subsequent capture growth and source resume can finish
inside eight seconds for a transition that happens only just before the cutoff.
The separate `PauseSLATEEvidence` cutoff and
`resume < same_session_started + 8 s` check remain. That anchor is read directly
after `pause()` returns, just after the instrumented ACK receipt.

## Measured baseline (before the observer correction)

Original source: `592c04a35f0b1fde1eab1b05b050e593338f616e`.
Diagnostic-only source: `c491685fc351e8f9b789ba9fb62eed6bae5d17bf`.

| Experiment | Result |
| --- | --- |
| Historical [job 103130491437](https://github.com/andreykutenkikh-byte/restream/actions/runs/34556604870/job/103130491437) | stall-switch failure; no target crash yet |
| One unchanged-source [baseline job 103713751456](https://github.com/andreykutenkikh-byte/restream/actions/runs/34556604870/job/103713751456) | PASS; not a fix |
| One instrumented [baseline job 103715424689](https://github.com/andreykutenkikh-byte/restream/actions/runs/34754093941/job/103715424689) | PASS; actual media, full original pre-crash history |

Only two new baseline attempts were used. Later runs verify the concrete Event
handoff correction; they are not reruns of unchanged code until green.

All times below are monotonic observation receipts relative to pause request.
Neither HTTP request start nor observation receipt is a media packet timestamp.

| Observation | Seconds |
| --- | ---: |
| T0 pause request | 0 |
| T1 pause ACK observed | 0.000064 |
| T2 last feeder send receipt | -0.005232 |
| Last ingest/output counter growth sample pair | 1.840978 to 2.043245 |
| T3 last observed video progress growth (approximate receipt) | 1.540742 |
| T4 video-growth age when checked | 2.539690 |
| T5 watchdog rejection, `video-stalled` | 4.080454 |
| T6 exact child kill request | 4.080649 |
| T7 child wait completed, returncode -9 | 4.083940 |
| T8/T9 normalized absent and local SLATE sample, start/end | 4.267356 / 4.269425 |
| T10 predicate evaluation / waiter return | 4.316719 / 4.316732 |
| Resume requested / ACK observed | 4.367406 / 4.367480 |

The original SRT and sink generations remained 1. Sample-to-predicate delay was
47.294 ms. Parent metric reads were available 75/75 for each role; maxima were
1.457 ms (DUT) and 0.873 ms (sink). These are **not** supervisor metric timings.
The initial success projection omitted detailed supervisor metric durations and
joint-idle rows; the follow-up summary retains these already-recorded values.

## Hypotheses tested and limits

- **A, buffered delivery:** supported as a normal mechanism in the measured
  baseline: counters still grew after feeder ACK. A six-second tail causing the
  historical deadline miss is not measured or established.
- **B, blocked stop/control path:** measured reject-to-kill was 0.196 ms and
  kill-to-reap 3.290 ms. No stop/wait delay in this run. This does not exclude an
  unmeasured historical control-loop delay.
- **C, audio/aggregate bytes masking video:** the real baseline stopped with
  `video-stalled` even though aggregate output growth was observed later than
  video growth. The aggregate tail is not proven to be audio-only. Dedicated
  video/audio negative supervisor tests use controlled progress/counter inputs,
  not a recording of a physical Moblin stream.
- **G, observer handoff:** reproduced before correction and tested after it on
  the same controlled publication schedule. The actual-media baseline exhibits
  the avoidable 47 ms delay but not the final-deadline race.

If stall-switch recurs after the handoff correction, the next single hypothesis
is **A: continued buffered delivery consumes the pause budget before the normal
2.5-second video-idle decision**. Compare the last delivery/progress receipts to
the decision, then decision-to-reap and completed SLATE samples. Do not extend
the eight-second contract or assume a slow scheduler without those measurements.

## Environment comparison

The historical failed PR15 diagnostic, unchanged-source baseline, and successful
PR16 diagnostic used identical six source hashes and FFmpeg 5.1.9-0+deb12u1.
The main job image-build log also installed that FFmpeg package version; its
final native summary did not independently save the executed binary hash.
MediaMTX is pinned to v1.20.1 with archive verification in both paths.

Both profiles specify 1.50 CPU, 1536 MiB RAM, 512 PIDs, `/run` 16 MiB and `/tmp`
256 MiB. The diagnostic explicitly asserts effective HostConfig; the main
runtime-limit script does not inspect `ci-ssh-target`, so its historical target
limits are supported by Compose configuration, not a saved effective snapshot.
The main container has prior install/clock/short-EOF/Compose work; the diagnostic
uses a fresh `network none` container. Internal scenario history before
stall-switch is preserved; quick mode's different later outage durations are
not reached yet. Prepared media/config bytes and host scheduling measurements
were not saved historically; equal recipes are not proof of byte equivalence.

The debug first-child stderr hook is armed only at `crash-death`, after the
failing stage. It was not active during the original failure. Reader timestamp
tracing does run in the earlier diagnostic history; its timing effect on the
historical failure is unproven.

## Regression and safety contracts

Regression includes pre-fix failure; timely publication and wakeup; publication
at/after deadline; timely publication but late wakeup; publication between
snapshot and wait; stale/incomplete samples; replaced ingest identity; old and
spurious notifications; and health/predicate work finishing at/after cutoff.

Bounded parent/supervisor rings retain only fixed event/reason codes, relative
monotonic timings, booleans, counters and local generation ordinals. No transport
URLs, credentials, raw stderr, environment or raw publisher identifiers are
exported. The supervisor file is root-private and written atomically outside
per-poll processing; parent evidence is frozen before later crash scenarios.

Normalizer source, 1080x1920/30, LIVE stream-copy, AAC normalization, 13 strict
captures x 90 decoded frames, 15-second reader bound, recovery/reset safeguards,
bounded retries/cleanup, crash-safe DR flock and counterfactual are unchanged.
Full local and CI results belong to their exact commit; do not transfer old HK
acceptance to new code automatically. This investigation does not close release
acceptance or authorize merge/deployment.
