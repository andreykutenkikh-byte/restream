# Disaster recovery

This document separates reproducible source from runtime state. The source repository is public;
plaintext production data and credentials must never be committed to it. A private Git repository
may contain only authenticated public-key-encrypted backup artifacts such as `.age` files. Versioned
object storage with retention or object lock is preferred for operational backups.

## Recovery inventory

| Class | Required material | Recovery rule |
| --- | --- | --- |
| Rebuilt from Git | Exact release commit and immutable tag, Compose and Dockerfiles, `uv.lock`, native relay installer/runtime sources, pinned MediaMTX version and SHA-256, slate generation, service templates, tests, and runbooks | Rebuild only from a reviewed release that passed CI. Do not depend on an unmerged workstation checkout. |
| Control-plane state | A verified `adojapan-restream-*.db` artifact created by `scripts/backup.py` (normally under `/srv/app/backups` inside the backup container), `/opt/adojapan-restream/.env`, the exact file selected by `BOOTSTRAP_WORKER_SECRET_FILE`, and the dedicated reverse-proxy site configuration | Never copy the live `/srv/app/data/restream.db` file as a backup. Use the [volume-aware backup procedure](../README.md#backup-and-restore), then encrypt the resulting artifact before it leaves the host. The `MASTER_ENCRYPTION_KEY` from the protected environment is required with the database; without it, encrypted destinations and relay commands are unrecoverable. |
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

There is no supported exact-relay-continuity snapshot procedure yet. Until a quiesce, snapshot, and
restore drill is implemented and verified, use the default revoke-and-bootstrap recovery path.

## Current status

Encrypted off-server backup is not configured by this repository. `scripts/backup.py` creates a
transactionally consistent but same-host SQLite copy for rollback; it does not satisfy the gate
above. Selecting the remote store, encryption recipient and key custodian, retention policy, and
restore schedule is a separate production operation and must be completed before claiming recovery
from VPS loss.
