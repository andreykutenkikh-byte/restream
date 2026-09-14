# Disposable local HUD beta

This beta runs the release candidate's existing FastAPI application and Uvicorn
server. The panel, one-time pairing, separate HUD cookie, evaluator, polling and
revocation use the real application. A clearly named **SYNTHETIC beta relay — no
video** submits synthetic protocol-v1 heartbeats to the real relay HTTP API.
There is no video stream, YouTube check, physical iPhone acceptance, or remote
server in this demonstration.

## Run from this candidate's repository root

Use Python 3.12+ and the project's locked uv version (`0.11.28`):

```sh
uv sync --locked
uv run --locked python -m scripts.hud_beta --port 8443 --duration 600
```

Choose a temporary password at the hidden prompt; do not reuse a real password.
Open the printed URL, normally `https://127.0.0.1:8443/`, **on this computer**.
The freshly generated self-signed certificate causes a browser warning. Proceed
for this local address in the disposable test browser; the script makes no trust
store changes. HTTPS is necessary for the real `__Host-` Secure HUD cookie.
Log in as `beta` with the prompted password, select **Подключить Moblin HUD**, and
open its one-time pairing link in a separate private browser context. Open the
HUD details and test revoke from the administrator panel.

For unattended local smoke only:

```sh
uv run --locked python -m scripts.hud_beta --port 8443 --duration 150 --synthetic-password
```

The explicit flag selects the public fixture password `local-hud-synthetic-only`.
It accepts no password argument and must never be used for deployment. Passwords,
node tokens, pairing fragments and cookie values are not printed. Access logs are
disabled in this beta wrapper; candidate security tests separately inspect logs.
Use `--port 0` to select a free loopback port and read the actual URL from output.

## Observe the bounded scenario

The default run stops after 600 seconds. Each 135-second cycle has three phases:

| Time within a cycle | Synthetic input | Expected observation while HUD polls |
| --- | --- | --- |
| 0–45 seconds | LIVE heartbeat every 5 seconds; 4 Mbit/s | Warm-up followed by normal health after enough samples |
| 45–90 seconds | No heartbeats | Telemetry ages; delayed and then unavailable data, without proof that video stopped |
| 90–135 seconds | LIVE heartbeats resume | Fresh data returns; health recovers after the normal sample requirements |

The console announces **fixture phases**, not verified stream health. Keep the
HUD visible across a full cycle; joining late may require the next cycle to see
all transitions. `--phase-seconds 60` gives more time per phase. Phase length is
bounded to 45–300 seconds and duration to 15–3600 seconds; a short duration is
only a launch smoke and cannot demonstrate the full sequence. The script does
not change application clocks, freshness thresholds, alert policy, or the
120-second recovery grace. Optional unavailable metrics remain unavailable.
The fixture's forward/process/bitrate values are synthetic, not platform or
viewer measurements. No spare relay is advertised or route speed measured.

To separately exercise panel connection loss, use the desktop browser's offline
network toggle for the HUD context, then restore connectivity. The HUD should
show its monitoring-connection warning and resume polling. This is a browser
fault injection, not a media failure.

## Isolation and cleanup

Every launch creates a fresh temporary SQLite database and ephemeral secrets.
The script constructs explicit settings and does not load `.env`, an existing
database, bootstrap credentials, or production endpoints. The server binds only
`127.0.0.1`; the fixture HTTP client verifies the generated certificate, ignores
proxy environment variables, and connects only to that same loopback server.
No host option, tunnel, firewall rule, trust-store update, SSH or installer is
provided. In-process offline media transports and a rejecting worker launcher
prevent FFmpeg/media traffic. A beta-only route allowlist permits panel reads,
login/logout, HUD pairing/revoke, and the synthetic heartbeat. Control commands,
onboarding, destination changes, ingest rotation and preview are denied. This
wrapper does not remove or change those features in the shipped application.

Ctrl+C or the duration limit shuts down Uvicorn and deletes the temporary DB and
TLS key. Nothing is deployed. A forced process kill can leave a directory named
`adojapan-hud-beta-*` in the operating system's temporary directory; remove only
the identified stopped run's directory. Its private temporary material should
not be shared. This beta is not an alternative deployment command.

## Physical Moblin check: five actions

Precondition: an operator has an iPhone with Moblin and a separately authorized
test panel URL reachable from it over HTTPS, with a certificate trusted by iOS.
This loopback beta and its self-signed desktop exception do **not** satisfy that
precondition: `127.0.0.1` on the phone refers to the phone. Preparing LAN hosting,
certificate trust or a remote test environment requires a separate setup; this
script intentionally does not expose one.

1. Log into the authorized test panel and select **Подключить Moblin HUD**.
2. Use its existing Moblin import link to set the browser homepage, or transfer
   the one-time link privately to Moblin's private Web browser.
3. Open the homepage and verify pairing completes, the fragment disappears,
   and the displayed relay/bitrate/forward/freshness values match the test input.
4. Observe the authorized normal → telemetry loss → recovery scenario; also
   check the separate monitoring warning when the phone loses panel access.
5. Revoke this HUD device in the panel and verify Moblin reports access disabled.

Record device, iOS and Moblin versions and actual results. Desktop Chromium or
WebKit results do not constitute this physical acceptance.

## Automated standalone demo check

The existing dedicated HUD browser gate also runs
`tests/browser/test_hud_beta_browser.py`. It starts the actual CLI in a subprocess
with its disposable DB and HTTPS server, drives the real administrator login and
pairing UI, and observes all three 45-second phases with ordinary browser polling.
Both Chromium and WebKit are required. Application clocks and recovery policies
are unchanged. The check also exercises cross-site cookie exclusion, separate
admin/HUD access, a browser connection outage and retry, UI revoke, unchanged
control/installation/destination/ingest rows, and duration-triggered cleanup.

```sh
uv sync --locked --group browser
uv run --locked --group browser python -m playwright install chromium webkit
ADOJAPAN_HUD_BROWSER_SMOKE=1 uv run --locked --group browser pytest tests/browser/test_hud_beta_browser.py -q -s
```

On PowerShell set `$env:ADOJAPAN_HUD_BROWSER_SMOKE='1'` before the pytest command.
Set `ADOJAPAN_HUD_BETA_ARTIFACTS=1` to keep screenshots of only the HUD normal,
telemetry-loss and recovered states under ignored `logs/hud-beta-browser/`.
Otherwise screenshots stay in pytest's temporary directory. No administrator
pairing screen, token-bearing URL, or cookie value is saved in these screenshots.
Each required engine uses a 150-second subprocess run; allow about five minutes
for this test module. A targeted `-k chromium` diagnostic runs just that engine
and does not count as the full browser gate.
