# Moblin Streamer HUD

Stage 4B.0 adds a read-only monitoring page for the private Web browser built into the
official Moblin application. It does not modify Moblin, create an iOS fork, or use a
scene Browser widget. The HUD is visible to the streamer while its private browser is
open and is not composed into the outgoing video.

## Verified Moblin contract

The implementation was checked against official Moblin commit
[`9f5793f978a41fb54966fd376d7889fb26e7abce`](https://github.com/eerimoq/moblin/commit/9f5793f978a41fb54966fd376d7889fb26e7abce).
At that revision, [`MoblinSettingsUrl.swift`](https://github.com/eerimoq/moblin/blob/9f5793f978a41fb54966fd376d7889fb26e7abce/Moblin/Various/MoblinSettingsUrl.swift)
defines the optional `webBrowser.home` setting, and the official
[`README`](https://github.com/eerimoq/moblin/blob/9f5793f978a41fb54966fd376d7889fb26e7abce/README.md)
documents both the private Web browser and the URL-encoded `moblin://?` settings link.

The generated settings document has exactly this shape:

```json
{"webBrowser":{"home":"https://restream.adojapan.ru/moblin-hud#pair=<one-time-token>"}}
```

It does not contain streams, quick buttons, an administrator session, ingest data, SRT
credentials, YouTube credentials, SSH credentials, or relay credentials.

## Pair a device

1. Sign in to the AdoJapan Restream administrator panel.
2. In **Moblin HUD**, select **Подключить Moblin HUD**.
3. Select **Добавить в Moblin**. If the custom link cannot be opened, copy the fallback
   link and open it in Moblin's private Web browser.
4. Keep the HUD page open during the broadcast.

The pairing link is valid for ten minutes and only once. Its 256-bit secret is placed in
the URL fragment, so it is not sent in the initial HTTP request. The HUD removes the
fragment before it sends the same-origin pairing request. The token is used transiently
for that exchange; application persistence stores only its digest, with no browser
local/session storage. Treat the original pairing link as a temporary secret.

Successful pairing creates a separate read-only `stream_monitor` session. The
`__Host-adojapan_hud_session` cookie is `Secure`, `HttpOnly`, `SameSite=Strict`, has
`Path=/`, no `Domain`, and expires after 30 days. Only its digest is stored. This session
cannot access administrator pages, node APIs, relay mutations, YouTube settings, SRT URL
reveal, bootstrap operations, or other credentials.

An administrator can see paired devices in the same card and select **Отозвать доступ**.
Revocation takes effect on the next HUD request. Pairing and revocation audit entries
contain only the device ID.

Reopening an already-used pairing link with a still-valid HUD cookie resumes monitoring
instead of attempting to consume the one-time token again. The HTML exposes only a
paired/not-paired boolean, never the cookie value. With no valid session, a consumed or
expired link still requires a new administrator-issued pairing link.

Saved-link reopening is supported both as a new page and as a same-document fragment
navigation. The latter erases the fragment and reuses only an already confirmed session;
it cannot revive a revoked session. For a genuinely new pairing after revocation, open
the administrator-issued link as a fresh page, not just a fragment change in the old one.

## What the HUD shows

The compact view shows the current server, LIVE/SLATE/NONE source, input bitrate and
trend, YouTube forwarding state, heartbeat freshness, available standby server, and a
plain-language reason. **Подробнее** expands safe host CPU/RAM and diagnostic states.
The API does not return IP addresses, SSH data, SRT URLs, YouTube URLs or stream keys,
node tokens, or internal paths.

Health levels are deliberately conservative:

- `unknown`: the evaluator is warming up, lacks history or lacks reliable telemetry;
- `green`: the active stream, heartbeat, bitrate, and YouTube forwarding are coherent;
- `yellow`: degradation is sustained, but immediate switching is not justified;
- `red`: serious degradation; switching needs a ready standby and sustained evidence;
- `black`: confirmed loss of the previously active media input or a confirmed critical
  server/output failure, not merely a lost heartbeat;
- monitoring offline: the page cannot reach the HUD API. This does **not** claim that
  the video stream itself has stopped.

Bitrate evaluation uses a spike-resistant EMA and stable baseline built only from valid
LIVE samples after warm-up. Named thresholds, consecutive-sample hysteresis, recovery
hysteresis, standby warm-up, and a recommendation cooldown prevent flapping. Telemetry
is memory-only and bounded to 72 samples for each of at most 32 routes.

If more than one LIVE route exists, the state is `ambiguous`; the HUD reports that it
cannot safely identify the active route and does not invent a target.

## Source loss, recovery and operator stop

| Observed state | Meaning and action |
| --- | --- |
| Fresh initial SLATE, no previous LIVE | Waiting for the source; not a lost stream. |
| Fresh LIVE | Confirm the active route and ingest measurements. |
| Previous LIVE becomes SLATE/NONE while relay works | Retain the active route; warn that input was lost. |
| Fresh SLATE with active output | May state that the server is forwarding the slate. Without this evidence, do not claim output delivery. |
| Fresh coherent inactive service, stopped process, closed listener, NONE source and inactive output | Intentional/coherent stop: idle. NONE alone does not establish a stop. |
| Missing/stale heartbeat or unavailable status | Monitoring uncertainty, not proof that media or a YouTube broadcast ended. |
| Fresh failed process/service | A confirmed server error, distinct from unavailable telemetry. |
| Multiple fresh LIVE routes | Ambiguous; do not choose or automatically switch a route. |

After confirmed input loss, recovery-grace is **120 seconds**. The warning remains
visible, but the recommendation is `watch`, with `reason_code=recovery_grace`.
When fresh SLATE/output telemetry supports it, the message is:
«Входящее видео пропало. Сервер передаёт заставку. Ожидаем восстановления связи».
After the grace expires, persistent loss recommends `reconnect` without a ready standby,
or `switch_recommended` with one. Restored LIVE cancels the pending manual recommendation
immediately; normal health recovery hysteresis can still take additional good samples.
RED without a standby remains `watch`.

The existing media-stall threshold (over 30 seconds of zero/missing input bitrate)
also uses this grace even if the source label still says LIVE. Its timer starts at
the first zero/missing observation; confirmation is required before treating it as a
stall. The wording states that telemetry does not confirm media arrival, not that a
particular SRT connection is broken. Expiring learned bitrate statistics never erases
an ongoing failure or restarts its clock. Fresh positive media clears obsolete loss
severity immediately to a warming-up state; current lower bitrate can independently
become yellow/red instead of remaining falsely black.

The main control-plane ingest API has no independent native service/process/listener
states. Its missing input is therefore not treated as proof of manual stop; only actual
available telemetry is used. Native relay coherent-stop checks use the real reported
states shown above.

The value comes from the existing native recovery constants, not an assumed recovery
phase exposed by the agent:

```text
6s confirmed stall
+ 2 × 30s retry cooldown
+ 3 × (0.1s confirmation + 2 × 0.2s metric reads + 1s polling + 0.5s API request)
+ 15s downstream recovery window + 5s agent heartbeat + 2s visible HUD poll
= 94s; round upward by one 30s native retry period → 120s
```

Some operations overlap; this conservative observation window is a HUD policy, not a
guarantee about network recovery. Sources are the constants in
`deploy/moblin-relay/moblin-relay-normalize`, the output gate in its `self-test`, and the
existing five-second heartbeat contract in `relay_agent/client.py`. No agent protocol
or release is changed. The current API does not carry recovery phase/exhaustion, so the
HUD never invents «reset succeeded» or claims that retries are exhausted.

If fresh active-route telemetry disappears entirely, retain its context for at most
120 seconds after the last actual fresh observation, then show unknown monitoring state,
not idle or “broadcast ended.” Polling an old heartbeat and updates from a standby do
not renew this window. A backend restart has no previous session history to reconstruct.
Each route's real heartbeat/measurement identity is ingested once; time-only polls still
advance freshness and grace thresholds without adding EMA samples or hysteresis streaks.

## Recommendation confidence

The active route is assessed from its measured input bitrate and server heartbeat. A
standby is eligible only when its server is fresh, available, portrait-capable,
configured, free of errors and pending commands, and healthy for at least ten seconds.
Deterministic selection is based on safe server readiness, not claimed mobile network
performance.

> Рекомендация текущей версии основана на качестве активного входящего потока и
> готовности резервного сервера. Она ещё не является прямым сравнительным измерением
> мобильного маршрута до всех серверов.

The API therefore returns only `active_path_measured` or
`standby_server_readiness`. It always marks the route from the phone to a standby as not
measured and never returns the reserved future value `end_to_end_measured`.

## Polling and sound

The page uses small same-origin HTTPS requests: approximately every two seconds during
a live/alert state and every five seconds while idle. Hidden documents use a slower
ten-second cadence; pagehide pauses polling until pageshow, including the BFCache event
path. Visibility changes reschedule the appropriate cadence. Requests
have a timeout and retain their ownership until they settle, so pause/resume cannot
create overlapping requests. Network failures use bounded backoff up to 15 seconds.
Revoke/logout is terminal; a temporary network error is not a media-loss recommendation.

iOS requires a user gesture before Web Audio can play. **Включить звуковые
предупреждения** unlocks an in-memory oscillator and plays a short test tone. Alerts
then sound on any worsening transition to yellow/red/black, including green→red,
green→black and yellow→black. A more serious escalation is not suppressed by a recent
weaker warning's cooldown. The first render, unknown→green, unchanged severity and
recovery remain silent. **Заглушить на 60 секунд** and enabled/muted state are memory-only.
iOS may throttle or suspend a background WebView, so background audio and polling are
not guaranteed.

## Browser regression checks

The required `hud-browser` CI job launches Chromium and WebKit against the actual
`create_app` over isolated loopback HTTPS with a temporary SQLite database. It loads the
ordinary `/moblin-hud` HTML and script tags, not a CommonJS substitute. It is separate
from the existing native media/preview/security job, which remains required.

```bash
uv sync --locked --group browser
uv run --locked --group browser python -m playwright install --with-deps chromium webkit
ADOJAPAN_HUD_BROWSER_SMOKE=1 uv run --locked --group browser pytest tests/browser -q
```

The optional locked browser dependency group keeps browser engines out of production
images and ordinary development sync. Default unit-suite execution skips this dedicated
smoke; the CI flag makes missing dependencies/engines a failure, never a silent skip.
Engine and OS dependency installation follows the
[official Playwright CI procedure](https://playwright.dev/python/docs/ci).
No traces containing session material are uploaded. Browser results are recorded in
the review report; desktop WebKit is not physical iPhone/Moblin acceptance.

## Current limitations

- The HUD is visible only while Moblin's private Web browser is open. Standard Moblin
  does not guarantee a persistent transparent overlay over the camera view.
- A scene Browser widget must not be used because it would be part of the broadcast.
- The HUD is read-only and never starts/stops relay, reveals stream URLs, changes
  profiles, or switches servers.
- There is no automatic switching and no direct phone-to-all-nodes route probe.
- No VPN behavior is assumed.
- No Moblin fork, Xcode project, custom iOS app, new service, or new media transport is
  part of Stage 4B.0.
- A future Stage 4B.2 may add genuine phone-to-node measurements; this version does not
  simulate or pre-claim them.
