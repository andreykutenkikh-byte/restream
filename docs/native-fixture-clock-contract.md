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
disturb video timestamps at the seam. Before the reader cases, a fast actual
continuous-copy remux checks both clips: the original four-second clip must
exhibit the seam-only discrepancy, and the eight-second clip must pass the same
frame-spacing and GOP checks through its first loop. This does not loosen the
observer's two-millisecond tolerance, reset timestamps or discard bad frames.
Observer failure diagnostics identify only a fixed reason and bounded numeric
frame/PTS deltas, never the underlying log text.

This establishes a controlled failure class. It cannot prove that scheduler
delay was the exclusive cause of each historical run whose artifacts are gone.

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
