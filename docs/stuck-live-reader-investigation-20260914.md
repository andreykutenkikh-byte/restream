# Strict reader #4 / stuck-live investigation

This is a separate investigation from historical stall-switch. Starting PR15
HEAD is `bdf5a552ecf3779c4ae008ac1909c228b290ba91`, tree
`32ee5ed868da9d9837c23494eda2ef764bbdd800`. Local and remote refs matched;
worktrees were clean. Main was `8136480d22ca7d4fdae82c70b4b34c1d638a9026`;
PR16 was `6995dc10bf7065fef6860db9342d95cfeac125ee`. Neither is modified.

Historical evidence: [main job 103717052883](https://github.com/andreykutenkikh-byte/restream/actions/runs/34754722280/job/103717052883)
failed at `stuck-live`. Its companion diagnostic succeeded, not full acceptance.
The original safe evidence remains in `stall-switch-evidence-20260913.json` and
[the previous report](https://github.com/andreykutenkikh-byte/restream/pull/15#issuecomment-5653022791).

## What was actually observed

The original reader reported probe start at 0.233 s, probe end at 6.614 s,
first progress frame at 7.239 s, last at 14.934 s, progress counter 86,
and timeout at 15.009 s. These are log-parser receipt times, not packet arrival
or decode times. The failed partial was deleted; full validation never ran.
The value 86 does **not** establish 86 correctly decoded frames.

`stuck-live` waits for a new authenticated ingest and healthy LIVE, then two
aggregate downstream-growth samples. Inside that wait, capture #4 runs before
the delivery ledger advances. Full media validators run later, after the whole
scenario. Captures #1 (initial SLATE), #2 (initial LIVE), #3 (same-session
recovery), and both pause/recovery histories must precede this capture unchanged.

The paced source clock measures successful local UDP sends of a fixed-rate TS
multiplex (including audio/null/table packets). Ratio 0.999998, max gap 0.010783 s,
zero discarded/rebased packets do not prove timely video delivery at the final
RTMP sink. Aggregate sink byte growth likewise does not prove video cadence.
Normalized-publisher generation is not pinned by the existing start/end Boolean
preconditions; new diagnostic ordinals observe that gap without changing gates.

## Bounded first reproduction hypothesis

Hypothesis D: progress delivery / pipe servicing / child completion could diverge
from the actual retained media. The only substantive difference in the first
new full-profile attempt is passive, opt-in evidence for capture #4 at
`stuck-live`, plus post-failure analysis after the original reader is stopped.
No reader flags, deadlines, original wait anchor, resources, neighboring workloads,
warmup, prior transitions, frame thresholds or validators change.

Support would require evidence linking delayed pipe servicing or lifecycle to
adequate media already present. Timely drains plus inadequate retained media
would narrow the problem toward input probing/video advancement (B/C), without
alone proving an upstream cause (A). A successful target does not prove a fix.
No runtime fix is included in this diagnostic commit.

Static inspection found separate unbuffered stdout/stderr drain threads,
continued draining after bounded parsing, no synchronous ffprobe/decode in the
capture path, and no obvious conventional pipe-capacity deadlock. Existing real
child flood/timeout cleanup tests pass. This does not rule out historical
scheduling stalls. Controlled subprocess tests are mechanisms, not historical
causation. There are at most two new pre-fix full-history actual-media attempts;
all automatically triggered jobs must be listed separately in the PR report.

## Opt-in and evidence interpretation

`scripts/ci_native_reader_evidence.py` only addresses Compose's existing
`ci-ssh-target` in GitHub CI. It creates an exclusive root0700 directory
`/tmp/adojapan-ci-reader-evidence` with root0600 hash-bound helper/marker files.
The marker expires after 1800 seconds. Existing 1.5 CPU, 1536 MiB and 512 PID
limits are checked and reported; no capacity is added. Self-test requires Linux,
root, a container marker, valid private files and an exact self-test/helper hash.
Absent opt-in, the helper is not read/executed. No installer changes are needed.

The helper wraps the existing child calls and original parser, rather than
replacing their algorithms. Nanosecond events are relative to the existing
diagnostic capture anchor; the original timed-wait call is a separate event.
Events are: 1 capture; 2/3 spawn call/return; 4 timed/untimed wait call (value is
timeout ns); 5 wait return; 6 timeout; 7 wait error; 8 poll (-9999 means alive);
9/10 kill call/return; 11 original reader cleanup completed; 12 retention request.
Rings are capped at 256 entries and report dropped counts. Pipe bytes/read/EOF
and maximum handler duration are observations, not network or decode timing.
Probe/NAL receipt milestones preserve the original parser's meaning (NAL
observations stop at the Input banner). Observer identities are role-local
ordinals, never real IDs. Long-lived RTSP capture growth observes DUT output,
not the final RTMP sink. Cgroup values are actual interval snapshots; missing
values and unavailable packet arrival timing are UNKNOWN. Executable hashes are
collected only after the reader's original cleanup.
Consequently the outer failure elapsed time can include post-reap hash/report
work. Compare the unchanged 15-second limit using the explicit timed-wait,
timeout, kill and reap events, not by subtracting outer diagnostic durations.

On failure, the original FAIL is recorded before retention. Only the stopped,
root-owned, regular, single-link `sink-proof-004.flv` in its private workdir may
be copied. Source mode 0600 or 0644 is accepted only there; it is tightened via
the verified descriptor after reap. Copy size is capped at 32 MiB and copy time
at two seconds. Identity changes, symlinks, missing/corrupt artifacts or write
errors never replace the original failure. Partial media is never acceptance.

The separate postmortem step runs after the unchanged onboarding step, not in
the measured path. It independently reports last progress, found packets,
decoded frames and full-validator status (NOT_RUN on reader failure). Reader
success and target-not-reached are distinct from failure; validator success must
still come from the unchanged complete scenario. Offline analysis has bounded
output and a 25-second total budget. It reaps its own children and removes only
the staging allowlist, including media, on success/error. No raw stderr,
environment, destination, publisher IDs or media are uploaded. The existing
counterfactual remains mandatory even if evidence collection itself fails.

## Scope and disposition

Windows local Docker engine is unavailable; no Docker/WSL repair is attempted.
Local regression gates are not Linux actual-media acceptance. Full-profile CI
is the reproduction environment; companion diagnostic is separate evidence.
Exact tested commit/tree, versions, container limits, all outcomes and cleanup
must be retained in the follow-up PR15 report, not inferred from this plan.

`OBSERVER_HANDOFF_FIX=PRESERVED`, `STALL_SWITCH_CAUSE=UNRESOLVED`.
At this diagnostic commit, `STUCK_LIVE_CAUSE=UNRESOLVED` and
`STUCK_LIVE_FIX=NOT_IMPLEMENTED`. No production connections or mutations,
runtime delta, PR16 restack, merge, deployment or image publication are authorized.
