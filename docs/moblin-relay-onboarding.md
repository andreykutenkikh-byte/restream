# Moblin Relay onboarding

The **Servers → Connect server** workflow provisions a complete native Moblin Relay. The browser
does not expose an install-profile selector: new interactive bootstrap jobs are always created as
`moblin_relay`. The older generic Docker Node Agent profile remains an internal compatibility path
and is not the default server experience.

## Operator flow

1. Prepare a fresh supported VPS and allow inbound **UDP 8890** in the hosting-provider firewall.
2. Open **Servers → Connect server** and enter the public address, SSH login, SSH password, and,
   preferably, the expected SSH host-key fingerprint. Port 22 is the default; a different SSH port
   is under **Additional settings**.
3. Wait for the bounded installer to finish. If the account is not root and does not have
   passwordless sudo, enter the sudo password only when prompted.
4. Open **Broadcast**, select the new relay server if more than one is present, and use the two
   normal actions:
   - configure YouTube (the first setup requires the exact `rtmps://` URL from YouTube Studio and
     its stream key; later key rotations require only the new key);
   - reveal and copy the generated SRT URL into Moblin or OBS.
5. Start the relay only when the intended YouTube broadcast is ready. A successful install leaves
   `moblin-relay.service` inactive and disabled, so provisioning cannot start an accidental
   broadcast.

YouTube RTMPS data is never entered in Moblin. Moblin receives only the complete SRT URL returned
by the panel. A YouTube Video ID, when used for comments, is configured separately in Moblin and is
not a stream key.

## Supported clean hosts

The native profile accepts only `amd64` hosts running Ubuntu 22.04/24.04 or Debian 12/13. It needs
systemd, a public IPv4 address, root or sudo access, enough CPU/RAM/disk for FFmpeg, outbound HTTPS
to the pinned MediaMTX GitHub release and `https://restream.adojapan.ru`, and a free local port set
used by the relay. A dual-stack DNS name is accepted when its complete address set passes the SSRF
policy; the installer deliberately selects its public IPv4 address for SSH and SRT.

The installer checks local listener collisions before claiming any managed path. It does not open
UDP 8890 itself because it deliberately does **not** change:

- the host or provider firewall;
- Docker or another container runtime;
- Amnezia;
- routes, NAT, IP forwarding, network interfaces, or DNS;
- SSH server configuration.

External reachability of UDP 8890 therefore remains a hosting prerequisite and must be verified
from outside the VPS.

## Installed profile

The release-controlled bundle installs:

- a SHA-256-pinned MediaMTX binary;
- the native `moblin-relay.service` and `relayctl` runtime;
- the 1080×1920, 30 FPS, H.264/AAC 12-second fallback slate;
- server-generated SRT and preview credentials;
- the outbound-only relay control agent 1.2.6 and its Unix-socket broker;
- a root-owned `/etc/moblin-relay/node.json` containing the resolved public SRT address and fixed
  port/path metadata.

No YouTube destination is generated or guessed. The exact RTMPS URL and stream key remain absent
until the administrator saves them through the authenticated control panel. The generated SRT
passphrase and stream ID are never returned by the bootstrap API; they are revealed later through
the existing one-time relay command path.

The final state is:

| Component | Required state |
| --- | --- |
| Relay control agent | active and enabled |
| Relay broker socket | active and enabled |
| `moblin-relay.service` | inactive and disabled |
| YouTube destination | not configured |
| SRT credentials | generated and stored root-only |

## Credential and rollback boundary

The backend creates the relay identity and bootstrap job as one durable role. A permanent relay
credential is minted only after SSH/platform/resource checks reach the credential-needed state.
Its raw value exists only in backend/worker memory, the authenticated Unix-domain-socket request,
and a mode-`0600` SSH staging file. It is never a command argument, environment variable, database
value, browser response, or log field. The database stores only its digest.

Failure or cancellation revokes that credential and cancels/tombstones outstanding relay commands
and secret results. Remote rollback is constrained by the exact AdoJapan marker and node ID. It
does not delete foreign paths or uninstall packages; dependency packages and inert system accounts
may therefore remain after an early failed install. A post-credential failure retains managed
evidence for a same-node recovery instead of guessing at destructive cleanup.

Relay commands are rejected while the same node has an active bootstrap job. The first successful
relay heartbeat completes the control-plane connection, after which the fixed final check verifies
the service states and protected files.

## Release verification

Before enabling this path in production, run the Python, frontend, lint, type, repository-policy,
and bootstrap-worker suites from the exact release commit. A fresh supported VPS smoke test is
still required for every installer release because unit/fake-SSH coverage cannot prove provider
UDP policy, package-mirror availability, or real systemd/FFmpeg behavior.
