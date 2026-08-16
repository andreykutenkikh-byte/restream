# Node Agent protocol v1

The Stage 4A Node Agent is an outbound-only HTTPS client. It does not listen on a network port and
does not expose an RPC shell. Its only control-plane surface is the fixed `/node-api/v1/*`
protocol described here.

Production agents require an HTTPS origin. Plain HTTP is accepted only when
`NODE_AGENT_ENVIRONMENT=development` or `test`, for local development and isolated CI fixtures.
The client does not follow redirects, does not read proxy settings from the environment, bounds
response bodies to 64 KiB, and sends:

```text
User-Agent: AdoJapan-Restream-Node/<agent-version>
Authorization: Bearer <node-token>
```

`Authorization` is omitted only for enrollment, where the single-use enrollment credential is in
the JSON body. All request models reject unexpected fields, and node request bodies are limited to
16 KiB by the control plane.

## Versioning and credentials

Protocol version `1` is explicit in enrollment and heartbeat messages. A version mismatch fails
closed (`409 unsupported_protocol`; structurally invalid JSON may fail schema validation first).
Stage 4A does not negotiate a downgrade. Heartbeat and every command endpoint enforce the
persisted supported version; an incompatible agent stops all outbound loops and becomes
quiescent until it receives a process shutdown signal.

The control plane creates a cryptographically random, single-use enrollment credential with a
600-second lifetime and stores only its SHA-256 digest. Bootstrap writes it to
`/var/lib/adojapan-node/enrollment.token` as a regular file owned by UID/GID `10001:10001` with
mode `0600`.

On the first successful enrollment, the server consumes that credential and returns the raw
permanent node token once. The agent atomically writes
`/var/lib/adojapan-node/node.token` with mode `0600`, fsyncs the file/directory where supported,
and deletes `enrollment.token`. On later starts it uses only `node.token` and also removes any
stale enrollment file. Credential paths must be regular, non-symlink files with safe size and
permissions. Secret wrapper objects redact `str`/`repr`; credentials are never accepted from
environment variables or process arguments and are not logged.

## Enrollment

`POST /node-api/v1/enroll` has no bearer header. Its JSON body is:

```json
{
  "enrollment_token": "<single-use token>",
  "agent_version": "0.1.0",
  "protocol_version": 1,
  "hostname": "node-1",
  "os_name": "ubuntu",
  "os_version": "24.04",
  "architecture": "amd64",
  "cpu_count": 2,
  "memory_total_bytes": 2147483648,
  "memory_available_bytes": 1073741824,
  "disk_total_bytes": 21474836480,
  "disk_free_bytes": 10737418240,
  "capabilities": ["ping", "self_test", "ffmpeg", "ffprobe"]
}
```

A successful `200` response contains `node_id`, `node_token`,
`heartbeat_interval_seconds: 5`, and `command_poll_interval_seconds: 5`. Invalid, expired, or
already consumed enrollment credentials return `401`.

The control plane treats the request credential as a redacted secret and unwraps it only at the
enrollment call boundary. Each direct peer is limited to one concurrent request and five attempts
per 60 seconds; global enrollment work and the in-memory identity table are bounded, and rejected
traffic receives `429` with `Retry-After`. A random token performs only an indexed read candidate
lookup. A matching candidate is then selected and verified again while the one-time consume and
permanent credential promotion run in one writer transaction.

## Heartbeat

`POST /node-api/v1/heartbeat` requires the node bearer. The agent sends a validated snapshot every
five seconds:

```json
{
  "agent_version": "0.1.0",
  "protocol_version": 1,
  "hostname": "node-1",
  "uptime_seconds": 12345.0,
  "load_1m": 0.25,
  "cpu_percent": 4.0,
  "memory_total_bytes": 2147483648,
  "memory_available_bytes": 1073741824,
  "disk_total_bytes": 21474836480,
  "disk_free_bytes": 10737418240,
  "ffmpeg_version": "ffmpeg version ...",
  "ffprobe_version": "ffprobe version ...",
  "capabilities": ["ping", "self_test", "ffmpeg", "ffprobe"],
  "current_command_id": null,
  "control_latency_ms": 12.5
}
```

FFmpeg/ffprobe versions may be `null` when a probe fails; heartbeat continues and the corresponding
self-test becomes false. `control_latency_ms` is the bounded RTT of the previous successful
heartbeat request and is `null` until one has completed; it never includes credentials or response
content. Numeric ranges and total/available relationships are validated on both sides. A successful
response is `{"status":"ok","node_id":"...","node_status":"ready",
"server_time":"..."}`. A valid credential failure, including revocation, returns `401`; sending
heartbeats more often than once per second returns `429` with `Retry-After: 1`.

The administrative status projection uses heartbeat age: at most 15 seconds is `ready`, more than
15 through 30 seconds is `degraded`, and more than 30 seconds is `offline`.

## Command delivery

The command loop makes a bounded request:

```text
GET /node-api/v1/commands/next?wait=20
```

`wait` is limited to 0-20 seconds. A `204` means no command. A `200` returns exactly:

```json
{
  "id": "<uuid>",
  "command_type": "PING",
  "payload": {},
  "lease_seconds": 30,
  "attempt_count": 1
}
```

Only one command poll may be outstanding for a node, and starts are limited to one per second. A
concurrent poll for the same authenticated node receives `429 command_poll_in_progress`; a serial
poll sent too soon receives `429 command_poll_rate_limited`. Both include `Retry-After: 1`, while
polls from other nodes remain independent.

The agent accepts only `PING` and `SELF_TEST` and requires an empty payload. There is no shell,
executable, argument, URL, or script field. It acknowledges delivery with an empty JSON object at
`POST /node-api/v1/commands/{id}/ack`, then submits a completion to
`POST /node-api/v1/commands/{id}/complete`.

`PING` returns `status`, `received_at`, `completed_at`, and `agent_version`. `SELF_TEST` returns
`status`, `completed_at`, `agent_version`, and exactly these boolean checks:

- `control_https`;
- `dns`;
- `ffmpeg`;
- `ffprobe`;
- `memory`;
- `disk`;
- `data_writable`;
- `no_inbound_ports`.

`no_inbound_ports` reads the agent's Linux network namespace and fails on every non-loopback TCP
listener, including wildcard and bridge-address listeners. Loopback-only listeners such as Docker's
embedded DNS resolver are not inbound exposure. Independently, the generated Compose model has no
`ports` and does not use host networking; repository policy tests enforce both constraints.

`control_https` is a real, bounded, credential-free request to the fixed command endpoint. It must
reach the control application and receive its expected unauthenticated `401`; TLS certificate and
hostname validation are enabled in production. Development and isolated test agents may make the
same check over HTTP. The probe follows no redirects, ignores proxy environment variables, reads
no response body, and has a five-second timeout.

The server leases a command for 30 seconds and permits at most three delivery attempts before
marking it failed. Non-terminal commands also have a five-minute maximum age; periodic maintenance
requeues expired leases below the retry limit and marks exhausted or stale commands failed even
when the node no longer polls. For `SELF_TEST`, `status` is `ok` if and only if all eight checks are
true. Ack and completion transitions are idempotent. The agent maintains a bounded, private,
atomically replaced `commands.json` journal (maximum 256 entries and 256 KiB), so a redelivered
command ID returns its stored safe completion instead of executing twice. Reusing an ID with
another command type fails closed. A repeated completion is accepted only when the safe result is
byte-for-byte equivalent.

## Backoff and shutdown

Heartbeat and command failures use bounded full-jitter exponential backoff: one second initially,
30 seconds maximum by default. Network failures do not make the agent execute a fallback command.
Rejected credentials and unsupported protocol responses permanently stop both outbound loops. The
process then remains alive but quiescent, without retry traffic or a busy loop, so
`restart: unless-stopped` cannot create a revocation restart storm. `SIGTERM` and `SIGINT` release
that wait and request cooperative shutdown; the managed Compose project allows a 45-second grace
period so the 20-second long poll and bounded HTTP request can exit.

An enrollment `401` is also terminal for that one-time file: the agent preserves the rejected
enrollment file and remains quiescent instead of entering a container restart loop. A fresh
bootstrap attempt supplies a new credential and force-recreates the managed agent when rotation is
required.

Persistent local configuration, credential-file, metrics-protocol, or command-journal validation
errors use the same signal-responsive, non-busy quiescent state and one safe error code. They do
not expose the offending file contents and do not repeatedly exit under `restart: unless-stopped`.
Unexpected internal or transient control failures remain non-zero failures so the bounded external
recovery path can act.

## Revocation and retention

The authenticated administrator can revoke a node through the session/CSRF-protected admin API.
Revocation marks both the permanent credential and any unused enrollment credential revoked,
cancels queued, leased, or acknowledged commands, and makes subsequent node calls return `401`.
It does not call SSH, erase the node's local token, stop the container, or uninstall the remote
project. That remote lifecycle is outside Stage 4A.

Expired enrollment records and terminal command results have bounded database retention; terminal
command results are eligible for pruning after 30 days. Safe node events contain state/detail
labels, never raw credentials or arbitrary command output.

## Container boundary and limitations

`Dockerfile.node` runs as fixed UID/GID `10001:10001` and contains the Python client,
CA certificates, FFmpeg, and ffprobe. It contains no Docker CLI/daemon, SSH client/server, web
server, or inbound listener. The remote Compose definition publishes no ports and mounts only the
agent data directory.

Stage 4A uses the node only for enrollment, inventory, heartbeat, `PING`, and `SELF_TEST`. It does
not send real video, publish to YouTube, hot-switch an active stream, accept arbitrary remote
commands, onboard with SSH keys, or uninstall itself. See [Node onboarding](node-onboarding.md)
and [Bootstrap security](node-bootstrap-security.md).

The one-time enrollment exchange does not yet have a replay-bounded response-recovery handshake.
If the server commits enrollment but the `200` response is lost before the agent atomically stores
`node.token`, the consumed enrollment token cannot be retried; an administrator must start a fresh
bootstrap/enrollment attempt. Stage 4A intentionally does not make the raw permanent token
recoverable from the control plane.
