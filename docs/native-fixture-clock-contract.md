# Native media fixture clock and outage checks

These are isolated test-fixture corrections, not a production deployment or
permission to alter an active relay. Final exact-head CI results belong in the
PR description; an earlier green run is not acceptance of a later commit.

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
It requires the already-installed pinned MediaMTX binary and fails explicitly
if that prerequisite is missing. Earlier setup failures and cancellation do
not trigger this check. It creates its own
loopback listeners, temporary configuration and owned media processes. It does
not invoke service lifecycle, self-test main or credential-writing routines.

Old and repaired feeders receive the same 22 ms scheduler-delay injection. A
continuously drained observer establishes two steady 60-frame / two-media-second
GOP intervals. One unchanged native reader is launched just after an observed
IDR, and phase/rate are checked again afterwards. The old case must fail by the
specific 15-second reader timeout while input/output and partial frames grow;
other errors are failures of the counterfactual. The repaired case must pass
the unchanged 90-frame capture and full format/GOP/PTS/DTS/A/V/decode validator.
There are no reader retries or reduced probe settings. Observation after reader
completion never extends the reader's deadline. Work and owned cleanup are
bounded; only fixed reasons and numeric diagnostics reach CI logs.

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
deadline never slides when more bytes arrive. Other fault tests retain their
4.5-second transition deadline. Natural expiry still requires its separate
8–13-second assertion or the existing exact-source confirmed-reset proof.
The three-second capture-growth/gap checks and all 12/15-second recovery and
90-frame media checks remain in force.

Failure-only diagnostics retain bounded observation windows and last-growth
intervals for input media, SRT transport, normalized output and downstream sink.
A last unchanged sample pair is not described as a whole outage without media.
Missing/regressed counters, changed identities and invalid timing yield unknown
evidence. Raw counters, identities, URLs, secrets and exception text are excluded;
the existing 2 KiB checkpoint and 64 KiB result limits remain unchanged.
