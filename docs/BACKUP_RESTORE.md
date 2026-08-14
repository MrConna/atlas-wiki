# Backup and restore

Atlas data has two authoritative parts: PostgreSQL and `uploads/`. A usable
backup must contain both from the same stopped-write window. Embeddings are
stored in PostgreSQL and can also be rebuilt from page content, the pinned
model digest, and the prompt-schema version.

Backup and restore take the same non-blocking host lock. A second operation
exits with code `4`; it never waits behind a potentially destructive restore.
The lock is keyed by `COMPOSE_PROJECT_NAME` (or Compose's directory-name
default), so separate checkouts operating on the same Compose project must use
the same explicit project name. It lives in an owner-only runtime directory;
an unsafe runtime directory, custom lock parent, or symbolic-link lock is
rejected with code `3`. `ATLAS_OPS_LOCK_FILE` is intended only for controlled
automation and its parent directory must be owned by the current user with no
group/other permissions.
Run the scripts from the repository root and do not run direct database writers
during either operation.

## Encrypted backup (default)

Install [`age`](https://age-encryption.org/) or the compatible `rage` CLI. Keep
the private identity off the Atlas host and backed up separately. The recipient
is public and may be supplied by argument or environment:

```bash
export ATLAS_BACKUP_AGE_RECIPIENT='age1...'
scripts/backup.sh /secure/path/atlas-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age
```

Equivalent one-off use is `scripts/backup.sh --recipient 'age1...' OUTPUT.age`.
The script never accepts a private identity or passphrase for backup and never
prints recipient material, document content, database credentials, or keys.

The script records which application services were running, stops `api` and
`web`, verifies storage, creates and validates a custom-format `pg_dump`,
archives uploads, writes component checksums and manifest metadata, encrypts
the complete bundle in a mode-0700 temporary directory, atomically publishes
the ciphertext, and restores the prior service state. A failure or signal
removes `.partial` output and publishes nothing.

There is no automatic plaintext fallback when `age` is unavailable or a
recipient is missing. For disposable development/CI fixtures only, opt in:

```bash
scripts/backup.sh --allow-plaintext /tmp/atlas-development-backup.tar.gz
```

The plaintext mode emits a warning and must not be used for private data.

## Restore into a clean environment

Use the same or a newer compatible Atlas checkout. The target database public
schema and uploads directory must be empty. Start only the database, then point
restore at an age identity file; the file contains a secret and should be mode
`0600` (owner-readable/writable only), owned by the current user, outside the
repository, and never pasted into a command or log. Restore rejects symbolic
links, identities owned by another UID, and any group/other permission bits
before invoking `age`:

```bash
docker compose up -d db
export ATLAS_RESTORE_AGE_IDENTITY=/secure/off-host/atlas-age-identity.txt
scripts/restore.sh /secure/path/atlas-20260814T120000Z.tar.gz.age
docker compose up -d api web
```

`--identity FILE` is available for supervised runs and passes only the path,
not secret key material. A plaintext development archive requires the explicit
`--allow-plaintext` flag.

Restore decrypts or copies the bundle only into a mode-0700 temporary
directory, validates its fixed member layout and SHA-256 checksums, checks the
Alembic ancestry, safely expands uploads to a staging directory, and runs
`pg_restore --list` **before stopping services or changing the database**. A
wrong identity, corrupt ciphertext, corrupt component, unsafe tar path, or
invalid PostgreSQL custom dump therefore fails before mutation.

After those gates, restore stops previously running application services,
requires an empty target, restores PostgreSQL transactionally, upgrades to the
repository's single Alembic head, atomically publishes staged uploads, and runs
storage verification. If a failure or signal occurs after mutation begins, the
application stays stopped for diagnosis. If it occurs before mutation, prior
services are restored. On success, start services and require
`/api/v1/ready` before use.

## Exit codes and interruption behavior

The stable script-level codes are:

- `0`: success
- `2`: invalid arguments, unsafe path, existing output, or non-empty target
- `3`: missing prerequisite, key/recipient, or unavailable database
- `4`: another backup or restore holds the shared lock
- `5`: backup dump/archive/validation failure
- `6`: encryption, decryption, archive integrity, revision, or pre-mutation
  dump validation failure
- `7`: database mutation or `pg_restore` failure
- `8`: migration or post-restore storage verification failure
- `9`: prior services could not be restored
- `130`: interrupted with `SIGINT`
- `143`: terminated with `SIGTERM`

Unexpected command failures are mapped to the active stage. Signal cleanup
preserves `130`/`143`, deletes temporary plaintext and partial output, and uses
the same pre-/post-mutation service restoration rule.

## Retention, RPO, and RTO

Copy encrypted backups to a second failure domain and periodically restore one.
A practical single-user policy is 7 daily, 4 weekly, and 12 monthly ciphertexts.
Deletion remains an operator decision so the script cannot erase an incorrectly
resolved directory. Losing the age identity makes encrypted backups
unrecoverable; keep a tested offline copy.

The recovery point is the newest successfully published and replicated backup.
For a daily schedule, the nominal RPO is at most 24 hours. Recovery time is
dominated by transfer, decryption, `pg_restore`, file hashing, and optional
re-embedding; measure it with production-sized data rather than treating the CI
fixture time as an RTO promise.

The scripts target the repository Compose deployment and fixed `atlas` database
and service names. External PostgreSQL, object storage, online snapshots, PITR,
and automatic retention require deployment-specific runbooks.
