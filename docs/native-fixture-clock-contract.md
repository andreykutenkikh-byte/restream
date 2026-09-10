# Native media fixture clock and outage checks

This records isolated test-fixture corrections and the narrowly scoped runtime
proof-carry correction below. It is not a production deployment or permission
to alter an active relay. Final exact-head CI results belong in the
PR description; an earlier green run is not acceptance of a later commit.

## Strict decoded-frame minimum after independent review

The independent review reproduced a clean-EOF false PASS: the reader requested
90 frames, but the validator required only 60 decoded frames. A correct
61-frame H.264/AAC FLV with IDRs at frames 0 and 60 passed every other format,
GOP, decode and timestamp check. An FFmpeg frame limit is an upper bound;
successful EOF and capture size do not prove that the requested count arrived.

`STRICT_SINK_REQUIRED_VIDEO_FRAMES` now supplies both the capture target and
the minimum decoded count, currently 60 + 30 = 90. Each of the 13 retained
segments passes the real validator independently. The final report also retains
all 13 decoded counts; both local and generated CI result validators reject a
short individual segment even if the total exceeds 13 × 90. These bounded
numeric fields do not change the existing 64 KiB result limit. The independent
reader and finite-media helpers load the complete self-test namespace, including
the shared constant; the generated result validator adds no global dependency.

`test-native-short-eof.py` is a mandatory Linux CI step before native onboarding.
It uses the exact staged self-test and real FFmpeg/ffprobe, with no server or
network listener. The finite local input replaces only the reader input URL;
the 90-frame target, 15-second reader deadline and stream-copy arguments remain.
For each of 61, 89 and 90 frames, the same correct finite portrait H.264/AAC file
is checked twice. The old comparison is replayed in the repository validator by
restoring only its former decoded-count guard. Both validators retain the full
format/GOP/PTS/DTS/A/V/decode checks, bounded subprocesses and capture cleanup.
The helper is CI-only and is excluded from the production installer bundle.

The existing portable FFmpeg 5.1.2 passed that actual-media regression locally:
old 61/89/90 all PASS; repaired 61/89 FAIL at the decoded-frame minimum; repaired
90 PASS. Every capture ended cleanly with exit code zero. This proves a stricter
acceptance oracle, not a cause or remedy for network reader timeouts, incomplete
probing or the cut+8 failure. The independent review's historical statuses remain:

| Run | Status |
| --- | --- |
| 33952161676 | INCONCLUSIVE |
| 33952428440 | INCONCLUSIVE |
| 3987e8f / 34049534760 | OPEN_BLOCKER |
| 71dfb24 / 34051598056 | OPEN_BLOCKER |
| c5a35439 / 34054147945 | OPEN_BLOCKER |

These open entries are acceptance-test failures with unresolved attribution,
not established production runtime causes. Owner media-risk acceptance remains
NOT_GRANTED. This correction does not alter the normalizer, watchdog, recovery
bounds, bitrate, codecs or FPS and does not authorize merge or deployment.

## Source pacing

The synthetic MPEG-TS feeder sends 10,528-byte datagrams at 9 Mbit/s. Its nominal
packet interval is about 9.36 ms. Rebasing the clock on every late wakeup loses
media time. Rebasing whenever a single packet interval is missed also develops
a speed cliff under ordinary 10–15 ms scheduling delays.

The repaired loop preserves phase while allowing at most three datagrams
(31,584 bytes, less than one 30 fps frame's transport budget) between pacing
waits. An explicit counter prevents a fourth immediate send. A long delay or
intentional pause reanchors the clock. This is bounded catch-up, not a claim of
zero bursts or a guaranteed bandwidth limit in every sliding time window.
Packet order and content are preserved; no source media is dropped or replayed.

Deterministic tests execute the actual feeder loop, including threshold edges,
per-send work, prolonged stalls and pauses. The separate real-media CI gate
compares old and repaired pacing with complete finite MPEG-TS captures, source /
sent / received byte identity, portrait H.264/AAC probing, frame timestamps and
cleanup. The helper-only Annex-B filter places SPS/PPS before buffering-period
SEI without reencoding or suppressing decode errors.

The original eight-second video-clock gate is retained. The added four-second
scheduling-cliff check measures the complete transport clock: finite mux padding
is not video duration. Its nominal first-to-last-send time excludes the first
immediately sent datagram. Both video and transport ratios are reported; the
transport acceptance remains 0.95–1.05, with byte integrity checked separately.

## Strict-reader counterfactual

`test-native-reader-clock.py` runs only in the disposable CI SSH fixture after
the native onboarding attempt, including a failed attempt. The original native
failure stays fatal; this independent check neither retries nor replaces it.
The disposable SSH image separately verifies the canonical v1.20.1 archive SHA
and retains only its executable, license and digest manifest under the fixed
root-owned, read-only `/usr/local/lib/adojapan-ci/reader` directory. It installs
no service. Before execution the helper validates the bounded regular files,
ownership, permissions, binary SHA and exact version. There is no `/opt` fallback:
native install rollback cannot remove this independent prerequisite. Earlier
setup failures and cancellation do not trigger this check. It creates its own
loopback listeners, temporary configuration and owned media processes. It does
not invoke service lifecycle, self-test main or credential-writing routines.

Old and repaired feeders receive the same 22 ms scheduler-delay injection. A
continuously drained observer establishes a complete eight-media-second source
period using five IDRs. Every adjacent GOP must still contain 60 frames and two
media seconds. One unchanged native reader is launched within 200 ms after an
observed IDR, and phase/rate are checked again afterwards. Both cases use one
shared healthy transport-clock predicate, 0.95–1.05, over the complete measured
first-to-last dispatch interval (excluding the first datagram's immediate credit).
The old clock must violate it and the repaired clock must pass it; the separate
finite feeder gate still requires whole-source sent/received byte and SHA identity.
Case-specific rate bands remain fixture preconditions, not alternative healthy
clock predicates. Missing or untrustworthy clock evidence is a fatal failure.

The old reader outcome is measured, not required to time out. A successfully
completed old capture must pass the same production validator as the repaired
capture, including at least 90 actually decoded frames and every existing
format/GOP/PTS/DTS/A/V/decode check. Only that outcome is
`VALID_SEGMENT_COMPLETED`. A specific 15-second reader timeout with input/output
initialization and growing partial frames is `EXPECTED_READER_TIMEOUT` for the
old case only. Fixed reader timeout is always fatal. `SHORT_EOF`,
`DECODE_OR_FORMAT_FAILURE`, `TIMESTAMP_OR_AV_SYNC_FAILURE`,
`FIXTURE_PRECONDITION_FAILURE` and `CLEANUP_FAILURE` remain fatal in either case;
exit status, progress frames and file size never substitute for full validation.
The repaired case must pass the unchanged 90-frame capture within 15 seconds
and the full production validator. Running it after an old-case failure provides
diagnostics only: aggregate failure is preserved.
There are no reader retries or reduced probe settings. Observation after reader
completion never extends the reader's deadline. Work and owned cleanup are
bounded; only fixed reasons and numeric diagnostics reach CI logs.

### Evidence for separating clock regression from reader timeout

The former assertion `old strict reader timeout not reproduced` conflated three
claims: old pacing violates the clock contract; fixed pacing satisfies it under
the same injection; and old pacing necessarily makes this reader exceed 15 seconds.
The first two do not imply the third. Historical safe diagnostics show:

| Measurement | PR15 old, run 34134227064 | PR16 old, run 34134862611 | PR16 fixed |
| --- | ---: | ---: | ---: |
| Capture-interval transport ratio | 0.296512 | 0.296277 | 1.003465 |
| Launch after observed IDR, seconds | 0.012393 | 0.012098 | 0.006119 |
| Next observer IDR after launch, seconds | 4.054012 | 7.714677 | 2.307945 |
| First reader parsed IDR, seconds | 4.013 | 7.680 | 2.278 |
| Probe start → end, seconds | 0.635 → 8.714 | 0.860 → 12.321 | 0.260 → 3.652 |
| First → last progress, seconds | 9.469 → 14.014 | 13.108 → 14.718 | 4.155 → 5.225 |
| Progress frames | 90 | 61 | 90 |
| Reader timeout | No | 15 seconds | No |
| Full production validation | NOT_RUN | NOT_RUN: timeout | PASS |

PR15's old capture cannot retrospectively be called `VALID_SEGMENT_COMPLETED`:
the former assertion aborted before validation, which was restricted to fixed.
There is no retained decoded count for that old capture. Both runs used identical
self-test, reader/feeder helpers, fixture image definition and resource limits;
both build logs report FFmpeg 5.1.9, with the same pinned MediaMTX 1.20.1 check.
The relevant HUD workflow changes add frontend/browser checks, not media commands.
The prepared source is shared within each pair; identical generated bytes across
different runs were not established.

At a ratio of 0.2965, three media seconds correspond to about 10.1 transport
seconds. That is only a sanity check, not a timeout prediction. The phase gate
observes another continuously running reader and does not fix admission, queued
media, or time until this reader's next IDR. In the second run, probing ended
3.607 seconds later and first progress appeared 3.639 seconds later. Those
observations are consistent with the different outcomes, but do not identify an
exclusive scheduler/buffering cause. Queue occupancy was not measured.

The oracle correction's bounded local paired experiment (FFmpeg 5.1.2,
MediaMTX 1.20.1) retained the same 22 ms injection, phase rules, prepared source
and production reader/validator. There was one attempt, not a retry sequence:

| Case | Full-interval clock | Reader outcome | Progress | Decoded | Full validation |
| --- | --- | --- | ---: | ---: | --- |
| Old single | FAIL, 0.286503 | EXPECTED_READER_TIMEOUT | 61 | Not measured: timeout | Not run |
| Fixed | PASS, 0.998061 | VALID_SEGMENT_COMPLETED | 90 | 90 | PASS |

Fixed's last progress was at 5.047 seconds. The pair took 98.1 seconds within
the unchanged 132-second budget; owned processes/listeners, temporary media and
the scoped local timer were cleaned up. Source MP4/TS SHA identities were
verified before and after the pair. Identical Windows-only adapters selected
the existing binaries/font, accepted Windows log-address formatting and used
QPC rather than the local coarse monotonic clock. None changes Linux CI or
runtime. This run observed a timeout, not actual-media old valid completion;
focused regressions separately exercise old valid completion through the real
production validator with controlled probe/decode results. Those regressions
are not represented as an actual FFmpeg capture.

The deterministic exact-loop mutation uses 1,001 identical sends and 22 ms
injection: old/fixed wall intervals are 31.358222222 / 9.380222222 seconds for
9.358222222 media seconds (ratios 0.298429616 / 0.997654640). Placing that actual
old-loop measurement in the mandatory fixed slot fails the shared predicate.
Focused cases also require 61/89-frame EOF, malformed/GOP/timestamp/A/V failures,
missing decode evidence, invalid clock/phase/sink and cleanup failures to remain
fatal. No production validator is copied or weakened.

The existing mandatory finite-source clock gate already independently detects
the regression: old/fixed transport ratios were 0.745855/1.001975 and
0.478560/1.002044 in PR15, versus 0.746113/1.001565 and 0.478176/1.003347 in PR16
for its respective 3 ms and 10 ms injections. Each pair passed source/sent/received
byte identity. Deterministic tests execute the actual feeder loop; a mutation
restoring old pacing in fixed acceptance must fail. The reader gate supplements
that proof under its unchanged 22 ms injection and retains actual-media acceptance.

This correction establishes a sounder test expectation and the old clock's
controlled regression class. It does not resolve or reattribute any historical
native media/recovery failure listed above. `KNOWN_MEDIA_FAILURES=OPEN` and
`OWNER_MEDIA_RISK_ACCEPTANCE=NOT_GRANTED`, even after a green final CI.

Per-GOP arrival time is not the transport clock: encoded packet sizes and
buffering can delay one IDR and advance the next relative to a uniform media
timeline. Actual local FFmpeg 5.1.2 / pinned MediaMTX tests using a high-resolution
clock recorded complementary fixed-clock intervals of 2.222995 / 1.761156 seconds
(apparent rates 0.900 / 1.136), while the pair averaged 1.004. The unchanged strict
reader nevertheless captured all 90 frames. Independent MPEG-TS packet-position
probes also show nonuniform GOP byte spans with continuous frame PTS, while a
complete four-GOP source loop approaches eight transport seconds. The magnitude
varies between generated fixtures; it is not a fixed per-GOP correction factor.

The rate bands remain 0.25–0.31 for the old clock and 0.95–1.05 for the repaired
clock, applied to one complete source period. Post-capture measurement uses the
two preceding and two following GOPs, with the preceding observations preserved
immutably before they can leave the 512-frame deque. All observed GOP structure
and timestamp checks remain mandatory, including later already-observed IDRs.
The 45-second phase gate, 200 ms launch phase, 15-second reader, 90-frame target,
and wait for only two following IDRs are unchanged. The overall work budget is
132 seconds: the extra two GOPs at each phase gate add at most
`4 / 0.25 + 4 / 0.95 = 20.211` seconds, covered by 22 additional seconds over the
previous 110. This adds bounded measurement work, not time for a failing reader;
owned-process cleanup retains its separate ten-second maximum.

Pinned Linux [CI 34049534760](https://github.com/andreykutenkikh-byte/restream/actions/runs/34049534760)
on `3987e8f` subsequently passed the complete independent counterfactual: the old
clock reached its expected 15-second timeout with 61 growing frames at transport
rate 0.296917; the repaired clock delivered 90 frames by 5.222 seconds at rate 1.0,
with the full format/GOP/PTS/DTS/A/V/decode gate and owned cleanup passing. Native
rollback no longer prevents this independent test. This was not a full green CI:
the main native scenario failed separately, as documented below.

A local paired execution with MediaMTX 1.20.1 and FFmpeg 5.1.2 passed the corrected
measurement: the old clock timed out at the unchanged reader deadline with 72
growing frames (transport rate 0.285732); the repaired clock captured all 90
frames and passed the unchanged format/GOP/PTS/DTS/A/V/decode validator
(transport rate 1.003710, last frame at 4.741 seconds). Both fixtures cleaned up
their processes/listeners and temporary media. This Windows reproduction used
QPC as its high-resolution monotonic clock: Python 3.12's local GetTickCount64
clock has a 15.625 ms resolution and is not equivalent to Linux's clock. A scoped
timer-resolution request was restored on exit. Neither adaptation is a runtime
or CI code change. Pinned Linux exact-head CI is still required for acceptance.

The paired source is eight seconds: 240 video frames and 375 AAC frames share
that boundary. The earlier four-second source has half an AAC frame at its end.
FFmpeg 5.1's copied-loop offset uses the longest track endpoint, so padding can
disturb video timestamps at the seam. The helper first converts each finite MP4
to Annex-B MPEG-TS with both SPS/PPS filters, then continuously remuxes that TS
without an explicit bitstream filter. This is not raw TS concatenation: FFmpeg
still advances the loop timestamps. Both positive conversion/remux commands
use error-level logging and `-xerror`; their bounded private stderr must be empty.

The earlier helper applied its explicit filter chain to a looping input. In
FFmpeg 5.1.9, the CLI sends EOF to that output filter at the first input loop,
then seeks back without resetting the filter. Subsequent video packets are
rejected, while unfiltered audio continues. The chain has a reset callback;
the CLI loop does not call it. This mechanism concerns the explicit CLI output
filter, not the MPEG-TS muxer's separately owned automatic converter.
([CLI loop and stream-copy output](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/fftools/ffmpeg.c),
[filter EOF/reset lifecycle](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/libavcodec/bsf.c))

Before reader cases, a bounded original-command regression requires exactly
120 complete video packets and only the specific paired filter-after-EOF
errors; generic errors do not count. Prepared four/eight-second loop probes
then require video beyond the first cycle, unchanged codec/profile and GOPs,
and the respective seam-only discrepancy / continuous frame spacing. The
original MP4s must report 30 fps. Only the deliberately discontinuous four-second
loop may have a different guessed frame rate; its packet timestamps remain the
oracle. The valid eight-second loop must still report 30 fps. Portable FFmpeg
5.1.2 reproduced 126/246 packets and seam errors of 10.678 ms / 11 microseconds;
the pinned CI binary must establish those properties independently.
This does not loosen the observer's two-millisecond tolerance, reset timestamps
or discard bad frames. The paired reader uses the same prepared eight-second TS
for both clocks and the original MP4 for its unchanged expected video signature.
Observer failure diagnostics identify only a fixed reason and bounded numeric
frame/PTS deltas, never the underlying log text.

A passing paired gate establishes a controlled clock-regression class, not an
inevitable old-reader timeout. It cannot prove that scheduler delay was the
exclusive cause of each historical run whose artifacts are gone.

## Native long-source header and clock correction

The main native fixture uses muxer-owned automatic conversion, not the explicit
filter chain above; the explicit-BSF EOF defect does **not** explain its reader
timeouts. A separate actual FFmpeg 5.1.2 probe did expose a source-header defect:
automatic MP4-to-TS conversion reported `non-existing SPS 0 referenced in
buffering period`. The synthetic encoder now repeats SPS/PPS before each IDR's
CBR SEI using `repeat-headers=1`; CBR, GOP, profile and encoded-picture settings
remain unchanged. This changes only generated test media, not relay encoding.

The long LIVE source is now 72 seconds, the next multiple of 24 after the old 60:
24 is the common period of 12-second SLATE cycles and 8-second AAC/video alignment.
This retains the existing long-window intent and avoids a half-AAC-frame loop
tail. It is an asset duration, not an increased recovery or capture deadline.

Actual local FFmpeg 5.1.2 generation, automatic remux through 72.2 seconds and full
video/audio decode passed: 2166 video packets, 60-frame GOP, 1080x1920 Main/level 4.0,
yuv420p, 30/1, all video PTS steps within 2 ms of 1/30, seam step 0.033356 seconds,
empty remux/probe/decode stderr. No input/clock assertion was removed. This proves
the corrected synthetic source; it is not exclusive attribution of historical
15-second reader failures. Exact-head pinned-Linux recovery/media CI remains
required before acceptance.

## Source-cut time is not last-media time

SRT and downstream media buffers can continue delivering useful video after the
sender is killed. The unchanged watchdog starts its 2-second joint-idle or
2.5-second output-idle proof after observed media growth ends, not at the network
cut. Serial metrics requests and state publication take additional time.

An actual-class regression demonstrates why a universal 4.5-second cut-to-SLATE
oracle is invalid: after output growth at cut + 2 seconds, valid 190 ms metrics
reads and 50 ms polling can correctly reach output-idle rejection at 4.77 seconds,
before child shutdown and observer publication. This is not a runtime watchdog
threshold change or a promise that total pipeline drain is capped at two seconds.

Only hard sender shutdown uses the existing eight-second lower natural-SRT-expiry
boundary: a fully completed fresh sample must show SLATE while the same SRT
publisher is still attached, strictly before cut + 8 seconds. This absolute
deadline never slides when more bytes arrive. Direct normalizer/child fault tests
retain their 4.5-second transition deadline. Natural expiry still requires its separate
8–13-second assertion or the existing exact-source confirmed-reset proof.
The three-second capture-growth/gap checks and all 12/15-second recovery and
90-frame media checks remain in force.

Failure-only diagnostics retain bounded observation windows and last-growth
intervals for input media, SRT transport, normalized output and downstream sink.
A last unchanged sample pair is not described as a whole outage without media.
Missing/regressed counters, changed identities and invalid timing yield unknown
evidence. Raw counters, identities, URLs, secrets and exception text are excluded;
the existing 2 KiB checkpoint and 64 KiB result limits remain unchanged.

## Paused sender and corroborated fallback

A feeder pause acknowledges the end of new sends, not the disappearance of
already-buffered output. Exactly two pause cases now require both bounds: SLATE
within 4.5 seconds of the last observed normalized-output byte growth, and a
fixed absolute cutoff from the acknowledged pause. The same-session case keeps
its eight-second resume boundary; the persistent-stall case keeps its nine-second
completed-reset deadline. No new growth can extend either absolute cutoff.
Completed, ordered metrics observations and the same attached SRT publisher are
mandatory. Missing metrics, changed normalized identity or regressed counters
fail closed. Byte growth is transport evidence, not a decoded-video assertion;
the separate unchanged capture, decode, six-second no-early-reset and fresh
pre-reset checks remain mandatory. Failure-only flow diagnostics cover both
pause stages with the existing allowlist, correlation and size bounds.

An independent actual-watchdog regression also exposed a runtime proof-loss
case: a legal 190 ms successful output scrape can reach the output-fallback
branch before the next ingest corroboration. The old reason-only guard then
discarded an already-observed continuous joint-stall interval and restarted the
six-second confirmation. In this deterministic model the old confirmation was
at 11.085 seconds; carrying the corroborated interval allows confirmation and
both fresh pre-reset checks by 8.925 seconds. This is a reproduced failure class,
not proof that it exclusively caused the earlier CI failure.

The normalizer now preserves that proof for output-fallback only when the
existing exact-source, joint counters, idle interval and three-observation guards
all hold. Six-second confirmation, fresh pre-reset checks, cancellation on growth,
identity change or metrics failure, attempt limits and cooldown are unchanged.
Output-fallback without corroboration and FFmpeg/PTS errors alone still cannot
authorize a reset. This candidate normalizer intentionally differs from the
previously audited deployed file; it has not been deployed to production.

## Hard-cut failure: distinguish transport growth from packet delivery

The same `3987e8f` Linux run failed the existing eight-second hard-cut gate at
`outage-normal`. Thirty-nine complete metrics samples covered cut+0.163–7.828 s,
with a maximum observation gap of 0.205 s. Unique SRT transport bytes were flat;
ingest-path growth ended in the observed 1.776–1.980 s interval. Normalizer and
sink RTMP connection bytes still grew in the 7.625–7.828 s interval. No watchdog
marker was recorded. Those counters include protocol framing and audio/video;
they do not establish seven seconds of buffered video or an exclusive cause.

Two bounded local probes used actual SRT → RTSP → normalizer commands, repeated
child replacements and packet traces. One also crossed the current 72-second
source loop and ran three unchanged 90-frame readers. Both stopped ordinary
video/audio delivery at about two seconds after the cut, with only terminal
packets on natural closure. They did not reproduce the CI tail or demonstrate
uptime-scale padding from `first_pts=0`. No resampler, watchdog, eight-second
pre-expiry gate or recovery deadline is changed on that hypothesis.

Failure-only diagnosis now reuses the existing `capture.flv` and its existing
file-size observations. A bounded FLV header walk separates AVC/AAC data tags,
excluding configuration/end/script tags, and records only counts, relative
observed-append times and PTS spans. Payloads are skipped, never logged. The scan
runs only after a failure, with a 65,536-tag / three-second cap; unsafe, malformed
or uncorrelatable evidence is unknown. A partial final tag is excluded and marked
explicitly: counts cover the completed prefix, so zero is not proof of no packet
in that partial tail. These are capture-branch observations, not exact normalizer
emission timestamps or a fresh decode test. The path's media-byte counter is also
extracted from the already-fetched metrics response, separately from connection
bytes. No extra network request, media reader or runtime FFmpeg option is added.

Failure checkpoints use compact JSON to retain this allowlisted evidence under
the unchanged 2 KiB loader limit. Atomic writing, correlation, root-only files,
the 64 KiB result limit and all pass/fail media/recovery assertions are preserved.
New diagnostic evidence still requires exact-head Linux execution; this is not
a claim that the unresolved hard-cut failure has been repaired.

## Reconnect reader: identify incomplete input probing

The next Linux [run 34051598056](https://github.com/andreykutenkikh-byte/restream/actions/runs/34051598056)
on `71dfb24` failed earlier, at the unchanged persistent-stall LIVE reader. Its
15.013-second deadline expired with zero output frames. The FLV
`Before avformat_find_stream_info` milestone was observed at 1.063 seconds, but
neither its matching `After` nor input/output initialization was observed.
All final flow flags were true. This localizes the observed wait after RTMP
opening and inside input probing; aggregate byte growth does not prove that
both usable audio and video packets reached this reader. The independent
pinned-Linux reader counterfactual passed again. Neither result closes the
earlier hard-cut failure or constitutes a full green CI.

FFmpeg's [FLV stream-info analysis](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/libavformat/demux.c#L2242)
can examine up to 90 media seconds while stream information remains incomplete.
Codec identifiers alone do not establish dimensions, pixel format, audio
parameters or first DTS. This limit is not a wall-clock delivery deadline.
The unchanged 15-second capture remains fatal; reducing probing without
establishing the missing evidence would not explain this failure.

The existing bounded debug-log drain now retains only exact SPS/PPS/IDR/non-IDR
parser-event counts and relative first/last observations before input
initialization. Counts saturate at 255 and are observations, not decoded frames.
Crossing the existing 1 MiB inspection limit is marked explicitly; no raw log
text or decoder address is retained. Missing events mean not observed, not
proven absent. This does not positively identify missing audio, and it changes
neither the reader's arguments nor the capture/validation requirements.

A subsequent bounded local probe included the previously missing topology:
the always-available 12-second SLATE path, native AAC-normalizer command,
RTMP forward, separate sink and long-lived RTSP recorder. The 72-second source
continued across a same-session pause, an exact isolated SRT reset and a final
hard cut. Readers were admitted on actual output/sink growth, not a preliminary
90-video-packet wait. All four unchanged 90-frame/15-second readers and full
format/GOP/PTS/DTS/A/V/decode validators passed. The persistent recovery reader
completed in 3.742 seconds (probe 0.203–2.117); active source byte-clock ratios
were 1.000382, 0.999950 and 0.999965. Final-cut ordinary audio/video stopped at
about two seconds, and the actual watchdog rejected the stalled source at
4.056 seconds. Owned cleanup passed. This FFmpeg 5.1.2 / MediaMTX 1.20.1
Windows loopback result does not reproduce or explain the Linux CI failures;
it does not authorize another pacing, resampler or deadline change.

## Repeated failure on the restacked HUD head

PR15 `a4feee1` passed full Linux CI 34053170259, including all 13 strict readers.
The own-HUD-only restack `c5a3543` then failed native `stuck-live` in
[CI 34054147945](https://github.com/andreykutenkikh-byte/restream/actions/runs/34054147945),
despite its actual Chromium/WebKit browser job passing. The unchanged native
reader timed out at 15.008 seconds with zero output frames. Probing started at
0.936 seconds; SPS/PPS and an IDR were observed at about 7.177 seconds, then six
non-IDR parser events between 7.592 and 8.634 seconds. All final flow flags
were true. The independent reader-clock test and cleanup passed again.

Those seven decoder observations do not establish source frame rate: FFmpeg's
stream-info path can stop invoking the H.264 decoder after enough pictures
establish its delay, while timestamp/audio analysis continues. Decoder work,
interleaving and diagnostic drain scheduling all affect observation times.
No cause is assigned to source pacing from these timestamps alone.

The fixture now retains a fixed-size sending-clock summary for its current
unpaused episode: successful datagram count, first-to-last dispatch span,
transport ratio, last-dispatch age, largest dispatch gap and schedule phase
discarded by the existing rebase branches. The first datagram's immediate credit
is excluded from the ratio. Intentional pause/resume starts a new episode; a
short, paused, invalid or unavailable sample is unknown. Socket-write acceptance
does not prove receiver consumption or encoded-media delivery. One additional
monotonic observation follows each successful send, under the existing locking
discipline; no new thread, history buffer, pacing decision, reader argument,
deadline or runtime behavior is introduced. Both successful and failed strict
capture diagnostics can retain this bounded summary for comparison.

## HK continuity follow-up: bound the complete metrics request

The owner-authorized temporary HK test on 10 September, using FFmpeg 4.4.2
and MediaMTX 1.20.1, failed the unchanged three-second capture-growth gate at
3.221 seconds. A single observation-only repeat measured 2.207 seconds and
stopped deliberately after that gate; it was not a full native PASS. In the
repeat, input, normalized output and final RTMP sink counters were flat before
SLATE resumed, with the original SRT and sink connections retained. Source
resume occurred after the measured plateau. This localizes the repeat to
LIVE-to-SLATE, but does not retrospectively establish the first failure's cause.

A separate actual-loopback HTTP regression exposed a concrete watchdog defect:
`HTTPConnection(timeout=0.2)` limited individual blocking socket operations,
not the whole metrics request. A five-byte response delivered one byte every
120 ms was accepted after approximately 484 ms. A trickling response could
therefore keep the synchronous watchdog inside a successful read instead of
letting it evaluate stale output or missing observability.

MetricsReader now uses one monotonic deadline for connect, request, headers,
body and the final parser result. The remaining budget is applied underneath
HTTPResponse buffering, before every socket read/write. A slow partial response
is discarded and its connection closed; a later sample starts a fresh budget.
Healthy complete responses still reuse the existing connection. Socket file
reference ownership is retained for both keep-alive and Connection: close.
No background worker, retry, process, signal timer or additional metrics request
is introduced. This bounds I/O waiting, not operating-system scheduling latency;
a late result is invalid even if its bytes are otherwise correct.

The existing 2.0/2.5-second watchdog policy, six-second exact-source reset proof,
fresh pre-reset checks, 3-second continuity gate, 15-second strict reader and
90-decoded-frame minimum remain unchanged. Metrics timeout is observability
failure, not evidence authorizing an SRT reset. Actual-socket and actual-watchdog
regressions exercise the total-deadline failure class independently of media.
This fix is not yet proof that the earlier HK 3.221-second failure, or any of
the historical native cases above, is resolved. Exact-head CI and actual HK
media acceptance results must be reported separately, including any failure.

## Full HK recording: bounded offline processing budget

The next full HK test completed all 13 strict RTMP segments, including at least
90 decoded frames and the existing format/GOP/PTS/DTS/A/V checks, but its whole
auxiliary recording's GOP scan exceeded the separate 60-second process deadline.
The failed 854,729,003-byte recording was not retained by that failure's cleanup
path. Its exact cause cannot be replayed or inferred from a different file.

A bounded file-only experiment used the unchanged 72-second LIVE generator from
self-test SHA `29f4df2d4ef998c2846df35adb63170e05c571b236c97f783c0aa98fcf4b069b`.
Stream-copy looping produced a 936.021-second, 953,757,832-byte synthetic FLV:
1080x1920 H.264 Main Level 4.0, yuv420p, 30 FPS, GOP 60, AAC-LC mono 48 kHz.
On the pinned HK FFmpeg/ffprobe 4.4.2 under a 180% CPU / 2 GiB cgroup, the original
60-second GOP probe timed out after emitting only 7,812 of 28,080 frame records.
Explicit decoder-thread controls also timed out; no thread setting was changed.
Partial records were not accepted as complete validation.

One separate, finite calibration kept the original commands and parsers but
gave each measured offline process a diagnostic 300-second deadline:

| Complete scan | Actual elapsed seconds | Result |
| --- | ---: | --- |
| Original GOP helper | 212.701 | 28,080 frames, 468 keyframes, every interval 60 |
| Original decoded-video helper | 210.888 | Every frame 1080x1920/yuv420p, complete monotonic PTS |
| Original FFmpeg A/V decoder | 236.126 | Exit 0, no warnings or decode errors |

Both frame scans had empty stderr; the decoded-video scan found no resolution
regression. All processes were reaped and the two generated media files removed;
the installed runtime and production credentials were unchanged. The numerical calibration result has
SHA256 `4eac48b29e230e9ecb32b95c9c71d54bfdff01c8cbb02da9bdf46eb73c284c60`.
This proves that 60 seconds is insufficient for a valid long synthetic recording
on this target. It does not prove the deleted original recording was clean or
explain an earlier CI startup timeout. The calibration is not full acceptance.

Only the full self-test's three aggregate video-processing calls now receive
`min(300, max(60, ceil(recording_duration_seconds / 3)))` seconds. Duration must
be finite and positive; invalid metadata fails closed. The cap is applied before
division as well, avoiding overflow from oversized metadata. The full result
records the selected budget. For this fixture it is 300 seconds, about 27% above
the slowest measured scan; this is a bounded allowance, not a load guarantee.

Helper defaults and quick aggregate probes remain 60 seconds. The installer
quick-test limit remains 660 seconds. Audio-only and packet scans remain 60 and
30 seconds; the 15-second strict reader, 90-frame minimum, three-second continuity
gate, startup/recovery policies, media arguments and all validity assertions are
unchanged. A process timeout still kills/reaps the probe and fails the test; no
partial output or negative result is converted into a PASS. A complete target
run with the changed candidate is required before accepting this correction.
