# Disaster recovery

This document separates reproducible source from runtime state. The source repository is public;
plaintext production data and credentials must never be committed to it. A private Git repository
may contain only authenticated public-key-encrypted backup artifacts such as `.age` files. Versioned
object storage with retention or object lock is preferred for operational backups.

## Recovery inventory

| Class | Required material | Recovery rule |
| --- | --- | --- |
| Rebuilt from Git | Exact release commit and immutable tag, Compose and Dockerfiles, `uv.lock`, native relay installer/runtime sources, pinned MediaMTX version and SHA-256, slate generation, service templates, tests, and runbooks | Rebuild only from a reviewed release that passed CI. Do not depend on an unmerged workstation checkout. |
| Control-plane state | A verified `adojapan-restream-*.db` artifact created by `scripts/backup.py`, `/opt/adojapan-restream/.env`, the exact file selected by `BOOTSTRAP_WORKER_SECRET_FILE`, and the dedicated reverse-proxy site configuration | Never copy the live `/srv/app/data/restream.db` file as a backup. Use the [volume-aware rollback procedure](../README.md#backup-and-restore), or the tmpfs export below for DR, then encrypt the resulting artifact before it leaves the host. The `MASTER_ENCRYPTION_KEY` from the protected environment is required with the database; without it, encrypted destinations and relay commands are unrecoverable. |
| Default relay recovery | No old relay credential is required | Revoke the lost node, bootstrap a fresh VPS, configure YouTube, and replace the Moblin SRT URL. The new node identity and SRT URL are expected to change. |
| Inputs for a future exact-relay-continuity procedure | `/etc/moblin-relay/secrets.json`, `/etc/moblin-relay/node.json`, `/etc/moblin-relay/.managed-by-adojapan`, `/etc/moblin-relay/.node-id`, `/etc/moblin-relay/release`, `/etc/moblin-relay/install-manifest.json`, `/etc/adojapan-relay-agent/node.token`, `/etc/adojapan-relay-agent/preview-reader.token`, and `/var/lib/adojapan-relay-agent/commands.json` | File copying alone is not a supported restore. The agent journal and control-plane command queue require one coordinated, quiesced recovery point with no pending or leased command. `node.json` contains the public SRT address and is valid on a replacement host only when that address still routes to it; otherwise bootstrap fresh and update Moblin. Never restore the same node token while the old host can still run; revocation and fresh enrollment are safer. |

Do not back up logs, containers, downloaded MediaMTX binaries, the generated slate, or runtime
configuration below `/run`. They are reproducible or nonessential and may contain volatile data.

DNS ownership and the authoritative records for the panel domain are external recovery dependencies.
Keep registrar/DNS access outside the VPS and document the required records in the protected
operations inventory. TLS certificates and ACME account material are not stored in this source
repository; after a control-plane rebuild, use the existing approved certificate mechanism to issue
a new certificate before exposing the restored service.

## Off-server acceptance gate

Disaster recovery is complete only when all of the following are true:

1. A consistent SQLite backup and the required control-plane files are encrypted with an approved
   public-key recipient before upload, without persistent plaintext staging files.
2. The encrypted artifacts are uploaded to versioned off-server storage using a credential scoped
   only to that backup destination.
3. The recovery private key is held outside the VPS and outside Git, in an approved password manager
   or offline custody.
4. Retention, recovery point objective, ownership, and failure alerts are defined.
5. Integrity and decryption are verified by an isolated restore drill from the exact release tag.

If Git is selected as the backup transport, use a separate private disaster-recovery repository and
commit only ciphertext. Never place a database, `.env`, master key, YouTube key, SRT passphrase,
relay token, SSH credential, or decrypted archive in any branch of this source repository.

## Encrypted Git transport

[`scripts/dr_backup.py`](../scripts/dr_backup.py) implements the safe packaging and publication
boundary for a Git transport. It:

- accepts a transactionally consistent SQLite export only from a Linux `tmpfs` mount;
- checks SQLite integrity and rejects symlinks, empty files, oversized inputs, and permissive modes
  on the database, environment, and bootstrap secret;
- streams a normalized tar/gzip archive directly into `age`, with no plaintext tar file;
- embeds checksums and the full deployed Git commit inside the encrypted archive;
- atomically installs a mode-`0600` `.age` artifact;
- refuses an encrypted artifact above 95 MiB so GitHub's per-file limit cannot turn an otherwise
  normal commit into a predictably rejected push;
- refuses the source repository, HTTPS credentials, an unconfirmed DR repository, a dirty
  worktree, or a repository that tracks anything other than the expected `.age` artifacts; and
- stages exactly one ciphertext file, commits it, and pushes it with non-interactive SSH.

The local safety attestation does not query GitHub and is not evidence that a remote is private.
An operator must first verify the repository's **Private** visibility in GitHub. The runner then
records that completed check only in the private worktree's local `.git/config`:

```bash
git -C /srv/adojapan-restream-dr config --local \
  adojapan-restream.dr-private-confirmed true
```

Do not put a README, manifest, recipient, public key, or restore instructions in that repository.
Its tracked tree must contain only files ending in `.tar.gz.age`. Keep this source repository as the
runbook and software source.

### One-time setup

1. Create a separate **private** GitHub repository, for example `restream-dr`, without initial
   files. Do not reuse the public `restream` remote.
2. On a trusted workstation, generate an age X25519 identity. Keep the private identity outside the
   VPS and outside every Git repository, in the designated password manager or an offline encrypted
   recovery file. Put only its public recipient line in a root-owned recipient file on the VPS.
3. Give the VPS a dedicated write-enabled deploy key scoped only to `restream-dr`. Use an SSH Git
   remote; do not embed a personal access token in its URL.
4. Clone the empty private repository to `/srv/adojapan-restream-dr`, select branch `main`, configure
   a non-personal commit identity, visually re-check **Private** visibility, and set the local
   attestation above.
5. Install `age`, Git, Docker Compose, and Python 3.12 from the approved system package sources.

The public age recipient may be present on the VPS. The age private identity must not be: a server
that is compromised must not be able to decrypt its historical off-server backups. Losing that
private identity makes the backups unrecoverable, so keep at least two controlled copies.

### Creating one recovery point

First create the consistent database copy in RAM. `/run` must be `tmpfs`; the backup publisher also
checks the final database path and fails closed if it is on persistent storage. The container UID is
the committed backend UID:

```bash
install -d -m 0700 /run/adojapan-restream-dr
install -d -o 10001 -g 10001 -m 0700 /run/adojapan-restream-dr/sqlite
docker compose -p adojapan-restream --env-file .env \
  -f compose.yml -f compose.production.yml run --rm --no-deps \
  --volume /run/adojapan-restream-dr/sqlite:/srv/app/dr-export \
  backend python scripts/backup.py --output /srv/app/dr-export --retain 1
```

Then run the publisher as root. These arguments contain paths and the public age recipient only;
secret values are read from files and never placed in shell history or process arguments:

```bash
python3 scripts/dr_backup.py \
  --database-backup /run/adojapan-restream-dr/sqlite/adojapan-restream-YYYYMMDDTHHMMSSZ.db \
  --environment /opt/adojapan-restream/.env \
  --bootstrap-secret /PROTECTED/PATH/bootstrap_worker_secret \
  --proxy-config /PROTECTED/PATH/restream-site.conf \
  --recipient-file /etc/adojapan-restream-dr/age-recipients.txt \
  --repository /srv/adojapan-restream-dr \
  --source-repository /opt/adojapan-restream \
  --consume-database-backup
```

`--consume-database-backup` deletes only the named tmpfs SQLite export after the encryption attempt.
The command prints no values or manifest; success is exactly
`Encrypted disaster-recovery snapshot pushed successfully`. A failed push leaves the encrypted
commit locally for diagnosis, but recovery from VPS loss is not established until the commit is
visible from another machine.

Schedule this exact two-stage operation with a root-owned systemd service and timer only after all
paths have been resolved on production. Use a non-overlapping lock, a daily schedule with
`Persistent=true`, and failure notification. Do not put secret values in the unit or its
environment file. After each run, verify from a separate machine that the new commit exists.
Git history retains old blobs even after a path is deleted. Monitor repository size and move the
same encrypted artifacts to versioned object storage well before either the 95-MiB artifact guard or
the selected repository-size budget is reached; do not rewrite backup history silently.

### Restore drill

At least quarterly, clone the private repository onto an isolated recovery host, check out a
selected immutable source release, decrypt one selected artifact there, verify every SHA-256 from
the encrypted `manifest.json`, run `PRAGMA integrity_check` on `control-plane/restream.db`, and start
the restored control plane without public ingress. Confirm that the environment and exact bootstrap
secret permit decryption and that a freshly bootstrapped relay can enroll. Destroy the plaintext
drill workspace when the evidence has been recorded.

Do not restore a recovered control plane while the original control plane is writable. Do not
restore old relay tokens to a replacement relay. Revoke the lost node, bootstrap a new node, enter
the YouTube configuration again if needed, and replace the Moblin SRT URL.

There is no supported exact-relay-continuity snapshot procedure yet. Until a quiesce, snapshot, and
restore drill is implemented and verified, use the default revoke-and-bootstrap recovery path.

## Current status

Source, deployment code, tests, and this runbook are versioned in the public source repository. The
encrypted packaging/publish command is implemented and tested, but no private DR remote, age
recipient, timer, alert, or successful restore drill is configured by the repository itself.
`scripts/backup.py` alone still creates only a same-host rollback copy and does not satisfy the gate.

Before claiming recovery from VPS loss, the owner must choose the private `restream-dr` repository,
choose whether the age recovery identity is held in the password manager or as an offline encrypted
file, define recovery point/retention objectives and an alert destination, install the scheduled
job, observe an off-server push, and complete the restore drill.
