# One broadcast screen

The managed broadcast stack and this screen were integrated through PR23.
The server recovery update adds IP labels, stopped-output server selection and
optional RTMP ingress. It continues to use the existing admission, credentials,
switch transactions and leases; the unrelated hosting migration remains separate.

## Server and protocol selection

- Cards, ingress choices, dialogs and topology show public IP addresses.
- Before sending, **Выбрать для отправки** selects the current egress route
  without starting a publisher, changing the OBS ingress or changing any key.
  It requires a ready target, no active handoff in the source session, and stop
  acknowledgment or expiry of all prior publisher grants. It does not need a
  YouTube backup endpoint. The mutation uses the same admin/CSRF/origin guards.
- While sending, the existing make-before-break switch and its backup-ingest
  requirement remain unchanged.
- **Получить подключение → Протокол подключения** offers SRT and RTMP.
  SRT returns one URL; RTMP returns separate **Сервер** and **Ключ трансляции OBS**
  values. Both authenticate the same per-source/per-node credential. No secret
  rotates and no desired intent changes when revealing or changing protocol.
  The dialog explains that RTMP is unencrypted; SRT remains available.
- Media agents keep RTMP bound to loopback by default. An explicit
  `rtmp_bind_host: "0.0.0.0"` uses their configured `ports.rtmp` and advertises
  that port in an authenticated heartbeat. Migration 12 adds only a nullable
  `broadcast_media_nodes.rtmp_port`; older agents still work and clear it.
  Upgrade the backend before enabling updated agents. Production uses TCP 24002
  for this opt-in listener, preserving the legacy RTMP service on TCP 1935.
  RTSP and the MediaMTX management API remain bound to loopback.
- `scripts.broadcast_rtmp_lab` reuses the existing three-node media laboratory,
  rejects an incorrect input credential and checks decoded RTMP → relay → sink
  video/audio and isolation. It does not contact YouTube.

The original UI acceptance notes below describe its initial integration. The
RTMP configuration, migration and stopped-selection additions above supersede
the original SRT-only and UI-only scope statements.

## User path

1. After login, **Трансляция** is the main screen. Use **Получить подключение →
   Подготовить подключение**, choose the actual public media ingress, then
   **Скопировать адрес**. In OBS, choose Custom under Settings → Stream, paste
   the complete SRT URL into Server and leave the key empty. The dialog shows
   the required source video/audio profile. The address is also usable in Moblin's
   SRT mode. Its creation does not prove that a listener or incoming A/V is ready.
2. Paste the YouTube Studio stream key into **Сохранить ключ YouTube**. It becomes
   **Ключ сохранён / Изменить**. Primary and optional actual backup RTMPS endpoints
   are under advanced connection settings. Backup is requested when switching
   needs it; the UI never derives it by changing a hostname. OAuth remains in
   additional actions and is not required for this path.
3. Start the source in OBS, then **Начать отправку**. Without measured source A/V,
   the screen says it is waiting for video. **Остановить отправку** stops this
   output's publisher and never issues YouTube `transition complete`.
4. Add another managed server to the selected broadcast if necessary, then
   **Переключить передачу на этот сервер**. This explicitly requests
   `handoff_ingress=false` through the existing switch API. The current card
   follows the confirmed server role, not the click. The output key and source
   ingress URL remain the same.

The secondary **Переключить подключение OBS/Moblin** action explains the source
address change/reconnect and effects on outputs sharing that source before
confirmation. Its target URL is separate; the normal connection endpoint reads
the actual ingress until the existing controller confirms direct A/V.

Multiple outputs require an explicit choice. **Добавить ещё один эфир** creates
an independent output/key, optionally sharing an existing source; page load,
polling, reload and connection reveal never create an output.

## API and safety boundary

- `GET /api/broadcasts/ui-state` adds public-address/capability/resource/slot and
  operation eligibility to the existing measured read model. It hides superseded
  plan observations. Heartbeat alone is not proof of route media or delivery.
- `POST /api/broadcasts/prepare` wraps existing session/output storage in a single
  idempotent transaction and creates a stopped manual draft. The existing agent
  desired plan already installs source listeners for draft routes. No agent
  protocol, media engine, switch timing or continuity criterion changes.
- `POST /api/broadcasts/sessions/{id}/connection` only reads the actual source
  secret and derives the existing per-node SRT credential. No rotation or intent.
- `POST /api/broadcasts/outputs/{id}/connection` updates the encrypted canonical
  binding, never returns its key, and rejects edits during switching. Changing
  a running destination/key requires stop plus the existing stop acknowledgment
  or lease-expiry safety proof. Adding a previously missing backup is permitted
  while live only when the primary/key stay identical and no BACKUP slot exists.

All adapters use existing admin authentication, CSRF, Origin/Fetch Metadata,
body limits and no-store policy. Monitor and scoped operator cookies cannot
access them. No credentials go into URL queries, browser storage or logs.
The only sessionStorage value is non-secret pending preparation intent and its
idempotency key, removed on success. Revealed/input credentials are erased when
their dialog closes, the tab hides, or the page leaves.

## Compatibility and limits

- The legacy dashboard and existing protected video preview remain at `/legacy`,
  clearly labelled. Existing legacy APIs and node operations are retained.
  The isolated HUD beta allowlist adds only this GET page; control mutations
  remain blocked. `/servers` is **Управление серверами**; `/broadcasts` keeps
  advanced/OAuth/operator/history functions.
- Current main/legacy can continue its existing static relay behavior. Managed
  switches and the new main screen require the PR21 dependency chain. A v1
  heartbeat cannot enable a fake managed switch.
- **Managed v2 has no source-scoped protected preview in the candidate.** The
  screen explicitly marks it unavailable. Reusing a node's legacy preview could
  show another stream, so it is not presented as the selected broadcast. Preview
  failure/unavailability does not change the measured input or output status.
  No video proxy is added to the control plane.
- Platform state is the last actually recorded YouTube response, with unknown
  values shown as no data. Publisher connection is not viewer playback proof.
- No production, Urikiri, relay, DNS, SSH or hosting access occurred. Physical
  OBS/Moblin → VPS → YouTube and viewer continuity were not accepted in this UI
  pass. Existing isolated media, security and migration CI gates remain intact.

## Reproducible checks and evidence

Run the locked ordinary gates (`ruff`, `mypy`, `pytest`, repository policy and
existing JavaScript tests), then:

```sh
uv sync --locked --group browser
uv run --locked --group browser python -m playwright install chromium webkit
ADOJAPAN_HUD_BROWSER_SMOKE=1 uv run --locked --group browser pytest tests/browser -q
```

`test_transmission_browser.py` uses actual HTML/JS and a disposable HTTPS backend
in Chromium and WebKit at desktop and mobile viewports. Only node telemetry is
synthetic; public ingress addresses are metadata and are never dialled. It covers
initial setup, saved-key reload, repeat reveal, egress success, double click,
pending-operation reload, failure, capability rejection, full-route warning,
stale telemetry, absent preview and explicit multi-output selection. Existing
browser/API tests retain monitor/operator scope, revocation and legacy coverage.
Unit tests additionally prove prepare rollback/idempotency, read-only reveal,
canonical key/lease preservation, stop guards, and delayed direct-source handoff.

Closed-dialog screenshots are written only to ignored `logs/transmission-ui/`.
The test rejects any page containing a SRT URL/passphrase, pairing fragment or
synthetic key before capture. No traces, cookies, credentials or network dumps
are captured. Existing browser CI's prohibition on artifact uploads is retained;
reviewable screenshots are delivered locally, outside Git.
