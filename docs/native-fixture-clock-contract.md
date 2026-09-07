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
observed IDR, and phase/rate are checked again afterwards. The old case must fail by the
specific 15-second reader timeout while input/output and partial frames grow;
other errors are failures of the counterfactual. The repaired case must pass
the unchanged 90-frame capture and full format/GOP/PTS/DTS/A/V/decode validator.
There are no reader retries or reduced probe settings. Observation after reader
completion never extends the reader's deadline. Work and owned cleanup are
bounded; only fixed reasons and numeric diagnostics reach CI logs.

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

A passing paired gate establishes a controlled failure class. It cannot prove that scheduler
delay was the exclusive cause of each historical run whose artifacts are gone.

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
