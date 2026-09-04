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
fragment before it sends the same-origin pairing request. The raw token is never stored
by the browser or server.

Successful pairing creates a separate read-only `stream_monitor` session. The
`__Host-adojapan_hud_session` cookie is `Secure`, `HttpOnly`, `SameSite=Strict`, has
`Path=/`, no `Domain`, and expires after 30 days. Only its digest is stored. This session
cannot access administrator pages, node APIs, relay mutations, YouTube settings, SRT URL
reveal, bootstrap operations, or other credentials.

An administrator can see paired devices in the same card and select **Отозвать доступ**.
Revocation takes effect on the next HUD request. Pairing and revocation audit entries
contain only the device ID.

## What the HUD shows

The compact view shows the current server, LIVE/SLATE/NONE source, input bitrate and
trend, YouTube forwarding state, heartbeat freshness, available standby server, and a
plain-language reason. **Подробнее** expands safe host CPU/RAM and diagnostic states.
The API does not return IP addresses, SSH data, SRT URLs, YouTube URLs or stream keys,
node tokens, or internal paths.

Health levels are deliberately conservative:

- `unknown`: the bounded evaluator is warming up;
- `green`: the active stream, heartbeat, bitrate, and YouTube forwarding are coherent;
- `yellow`: degradation is sustained, but immediate switching is not justified;
- `red`: sustained serious degradation and a switch may be recommended;
- `black`: the active route or media progress is lost;
- monitoring offline: the page cannot reach the HUD API. This does **not** claim that
  the video stream itself has stopped.

Bitrate evaluation uses a spike-resistant EMA and stable baseline built only from valid
LIVE samples after warm-up. Named thresholds, consecutive-sample hysteresis, recovery
hysteresis, standby warm-up, and a recommendation cooldown prevent flapping. Telemetry
is memory-only and bounded to 72 samples for each of at most 32 routes.

If more than one LIVE route exists, the state is `ambiguous`; the HUD reports that it
cannot safely identify the active route and does not invent a target.

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
a live/alert state, every five seconds while idle, and every ten seconds while hidden.
Network failures use bounded exponential backoff up to 15 seconds. Requests have a
timeout, never overlap, are generation-guarded, and stop on page unload.

iOS requires a user gesture before Web Audio can play. **Включить звуковые
предупреждения** unlocks an in-memory oscillator and plays a short test tone. Alerts
then sound only on a health transition, with a cooldown; **Заглушить на 60 секунд** is
also memory-only. iOS may throttle or suspend a background WebView, so background audio
and polling are not guaranteed.

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
