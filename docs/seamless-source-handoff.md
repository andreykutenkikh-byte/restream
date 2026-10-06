# Persistent publisher source handoff candidate

Stack base: PR20, `64e2c72c44e1d94522d8725a60864e2ea6db2c5e`.
This is a local/CI implementation candidate. No merge, deployment, production/HK login,
real YouTube credential, OAuth, broadcast creation, VPN or DNS change is part of it.
Standalone HUD production rollout remains **PAUSED**.

## Two operator actions

In the existing `/broadcasts` panel or session-scoped `/stream-operator`, select the
running output's **Переключиться на …** button, then choose:

| Mode | Automatic action | Source action | Actual final path |
| --- | --- | --- | --- |
| **Сменить сервер отправки / EGRESS_ONLY** | Prepare target, validate A/V, lease the other supplied ingest slot, confirm sending, revoke old egress | None | OBS/Moblin → A → B → YouTube |
| **Полностью перейти / FULL_ROUTE** | Same egress sequence, then await and verify direct input before committing session ingress | Reconnect to the prepared target source URL | OBS/Moblin → B → YouTube |

The mode, source instructions and measured topology appear in both interfaces. UNKNOWN
stays UNKNOWN. A full ingress change affects the session's other outputs as before.
The monitor has no switch/cancel authority. The same output owns the encrypted canonical
credential. Standby receives no key; node/output/slot leases, generation fences, expiry,
stop acknowledgment/safe expiry and durable controller reconciliation remain unchanged.
Managed agents are trusted: YouTube does not cryptographically enforce our lease generation
against a compromised VPS that has copied a key. No automatic failover is enabled.

## Compressed media implementation

`app/broadcast/selector.py` is a small standard-library FLV remux selector, without a new
media dependency. Each active output has one persistent FFmpeg copy publisher and at
most two local FFmpeg copy readers. Inter-node forwarding remains encrypted SRT.
Readers use authenticated **loopback RTMP**, avoiding an extra RTP timestamp conversion.
Their bounded startup analysis is 3 seconds / 4 MiB; this is reported as preparation,
not hidden as part of a subsecond selection claim.

Before requesting direct selection the existing runtime still checks source/session path,
profile, resolution, advancing A/V, at least 90 packets and repeated readiness. The selector
additionally requires byte-identical AVC/AAC decoder configuration, AAC-LC, at least 90
warm packets of each type, an actual length-prefixed IDR NAL (not just a keyframe flag),
strictly advancing DTS and first A/V PTS within 100 ms. This is deliberately conservative:
two individually supported encoders with different SPS/PPS are not interchangeable here.
An incompatible direct input cannot displace healthy forwarded media.

The active queue and in-flight write drain before the new timeline is committed. A common
DTS offset preserves the relative A/V timing and video composition offset, and retains
elapsed no-input time rather than fabricating media. FLV integer-millisecond collisions
may be mapped by at most 1 ms, with an explicit counter; larger regressions fail closed.
No transcoding, slate, frame repetition or active packet deletion is used by this pipeline.
Preselection packets of an unused alternative are counted separately from active loss.

Limits: two readers/output; 256 active packets / 8 MiB; 2 MiB per tag; 32 packets / 2 MiB
while collecting an IDR boundary; 16 retained events; five bounded reader restart attempts.
Queue overflow is an error, never a drop-and-continue policy. Blocking publisher I/O does
not hold the control lock. Lease shutdown terminates the publisher first, then readers;
child processes are reaped and pipes closed, including during publisher retry backoff.
The publisher identity includes logical source and destination, not the physical input URL.
Publisher crashes necessarily break its connection and follow the existing bounded retry.

The inspected [MediaMTX path implementation](https://github.com/bluenviron/mediamtx/blob/v1.19.2/internal/core/path.go)
does not make source replacement automatically continuous for existing readers.
The FLV parsing/remux contract follows
[FFmpeg's FLV demuxer](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/libavformat/flvdec.c)
and [muxer](https://github.com/FFmpeg/FFmpeg/blob/n5.1.9/libavformat/flvenc.c).

## Reproduce locally

Install the repository's pinned `uv` and locked dependencies; provide FFmpeg/ffprobe
and MediaMTX **1.19.2** binaries. From the repository root:

```sh
uv sync --locked --group browser
uv run --locked python -m scripts.broadcast_handoff_panel \
  --mediamtx /path/to/mediamtx --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --directory logs/handoff-panel-fresh --seconds 1800
```

Open `https://127.0.0.1:8766/lab` and accept the disposable local certificate only for
this loopback fixture. Login: `lab` / `local-handoff-synthetic-only` (public test values).
Open **Панель Orchestrator**. The helper creates one running `Synthetic relay-a` output,
sets its synthetic credential once, starts the synthetic source on A and prepares B/C
routes. Two stopped outputs belong to the existing multicast fixture; select the running
output for this walkthrough. YouTube endpoints are substituted with local RTMP sinks.
The helper forbids output/credential/OAuth/node/bootstrap mutations and expires within
one hour; its default is 30 minutes. Use a fresh directory for each run.

For EGRESS_ONLY, choose B and watch `EGRESS_SWITCH_COMPLETED`; A remains ingress.
For FULL_ROUTE, choose a different target and wait for `AWAITING_DIRECT_SOURCE`.
Return to `/lab`, choose that node and press **Переподключить источник**. This stops the
only source, waits one second and reconnects. Watch `COMPLETED` and measured direct
topology, then repeat on C. **Разрешить оператору переключение** creates the existing
explicitly scoped one-use pairing link. Use it in a separate browser context; revoke
through **Устройства оператора** when finished. Logout of the admin panel.

The helper uses the real panel/controller/media runtime, not mocked heartbeat success.
The automated lab gives the detailed decoded seam measurements:

```sh
uv run --locked python -m scripts.broadcast_handoff_lab \
  --mediamtx /path/to/mediamtx --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --directory logs/handoff-media-fresh --focused
uv run --locked python -m scripts.broadcast_switch_lab \
  --mediamtx /path/to/mediamtx --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --directory logs/handoff-regression-fresh
```

Windows uses the same commands with Windows paths and PowerShell backticks instead of
shell backslashes. Local validation used Python 3.12.14, FFmpeg 9.0.2 and MediaMTX 1.19.2.
The required Linux jobs run the existing Docker image with `--network none`, two CPUs,
3 GiB memory, 256 PIDs, dropped capabilities and a non-root UID. Only `report.json` is
uploaded; local configs, transport URLs, tokens, cookies and raw stderr are not artifacts.

## Measurement boundaries and acceptance

The new lab repeats two reconnect transitions and two overlap transitions with the same
output and encrypted binding. A persistent RTMP decoder spans each seam, decoding both
video and audio with errors fatal. It records monotonic arrival time, decoded PTS and
checksums. The selected IDR's independently decoded checksum identifies the first direct
video at that receiver; audio is correlated at the mapped IDR PTS plus the preserved A/V
offset. Generic first-frame-after-decision timing is not used as direct identity proof.

Preparation, first direct A/V reader arrival, decision-to-first-correct receiver video/audio,
and maximum receiver video/audio gaps are separate fields. Physical source absence is
last advancing old-ingress bytes to first advancing new-ingress bytes, sampled nominally
every 50 ms. It is a transport observation, not proof that every incoming byte is a frame.
No incompatible clocks are subtracted to invent a clean server overhead number.

Overlap requires **both decoded video and audio arrival gaps ≤1000 ms**. Reconnect has
real source absence and does not use that threshold. Both retain portrait 1080×1920/30,
H.264/AAC, GOP ≤60, ≥90 decoded frames, original segment PTS/DTS/A/V/decode gates.
The original multicast/independent output, B→D relocation, failed-target rollback,
controller restart, partition/expiry/fencing gates remain required. Additional checks
exercise actual incompatible direct audio, source returning to the old node, killed input
reader and killed publisher. Unit tests cover configuration mismatch, unaligned direct,
90-packet warmup, actual IDR parsing and blocked-output queue/lock behavior. HTTPS browser
tests retain duplicate-click, reload, operator/monitor isolation and revoke in both engines.

Historical PR20's ~11.3 s receiver gap included a restarted ffprobe's analysis/buffering.
It is not directly comparable to this persistent decoder's seam measurements and is not
all proven server overhead. A single source reconnect can still interrupt several seconds
of video/audio despite keeping the publisher; the selector cannot generate absent frames.
Synthetic receiver continuity does not verify YouTube playback, chat or viewer count.

Development failures are retained: the first overlap spike passed, but reconnect exposed
equal/nonadvancing AAC DTS through RTSP and decoder startup errors even before source
stop. Moving the compressed reader and continuous decoder to RTMP removed that extra
clock conversion. The first RTMP run failed because 200 ms analysis did not discover video
dimensions; the explicit bounded 3 s analysis fixed that cause. A Windows cleanup failure
also exposed a broken-pipe close after process exit; cleanup now handles that error and
writes the safe report before teardown. No media threshold was relaxed to hide these runs.
The first extended failure run passed all four seams and both process-crash cases, then
failed because its new old-source check queried ingress on the session table. The check
now reads `broadcast_sources`, which owns ingress; runtime behavior was not changed.
The first panel walkthrough also caught CSP blocking its helper's inline script; the
helper now serves a same-origin external script, and the full walkthrough passes.

## Separate physical acceptance

After a new explicit authorization, record OBS/Moblin/iOS versions and encoder settings.
Prepare per-node SRT profiles beforehand: H.264/AAC, 1080×1920/30, keyframes every 2 s.
For Moblin, stop its current sending if required, select the saved target profile, reconnect
and wait for observed direct A/V. A settings import is not a proven active-LIVE command.
For OBS, stop **OBS sending**, select the prepared target SRT URL in stream settings and
restart sending. Neither action ends the YouTube event. EGRESS_ONLY requires no source
changes. DNS cannot move an already open TCP/SRT connection.

Check the unlisted event's auto-stop setting and actual primary/backup ingest endpoints
in advance; never derive backup by editing a hostname. During two switches capture a
separate viewer's video/audio timeline, event URL/ID, chat and observed viewer count.
Do not call YouTube insert/bind/complete as part of switching. YouTube aggregate health
does not prove slot selection; no immediate-select-backup API is assumed. Physical
OBS/Moblin handoff and real YouTube viewer continuity remain **NOT_RUN / NOT_VERIFIED**.
