# Backup and restore

Atlas data has two authoritative parts: PostgreSQL and `uploads/`. A usable
backup must contain both from the same stopped-write window. Embeddings are
stored in PostgreSQL and are also reproducibly rebuildable from page content,
the pinned model digest, and the prompt-schema version.

## Create a backup

Run from the repository root while the database service is healthy:

```bash
scripts/backup.sh /secure/path/atlas-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
```

The script records which app services were running, stops `api` and `web`,
creates a custom-format `pg_dump`, archives `uploads/`, writes SHA-256 checksums
and manifest metadata, atomically publishes the archive, and restores the prior
service state. It refuses to overwrite an existing archive. A failed backup is
not published.

The archive is permission-restricted but not encrypted by Atlas. Store it on an
encrypted filesystem or encrypt it with an organization-approved tool before
copying it off-host. Keep an off-host copy and periodically test restoration.
A practical single-user policy is 7 daily, 4 weekly, and 12 monthly copies;
deletion remains an operator decision so the script cannot erase the wrong
directory.

## Restore into a clean environment

Use the same or a newer Atlas checkout. Start only the database, then restore:

```bash
docker compose up -d db
scripts/restore.sh /secure/path/atlas-20260814T120000Z.tar.gz
docker compose up -d api web
```

`restore.sh` validates revision compatibility, upgrades to the repository's
single Alembic head, and runs storage verification while application services
remain stopped. Do not start or migrate the API separately before it succeeds.

Restore verifies component checksums before changing anything and refuses to
overwrite a non-empty database or uploads directory. This deliberately makes
disaster recovery safe by default; in-place rollback requires a separately
approved migration plan. `verify_storage.py` checks the Alembic version,
document/page/chunk references, file presence, sizes, hashes, path containment,
and orphan files without modifying data.

After verification, test readiness, open an imported document, run keyword and
semantic searches, and run one cited Ask query. If the embedding model identity
changed, run `python -m app.cli embeddings-backfill` and repeat retrieval gates.

## Recovery objectives and limitations

The stop-write backup favors correctness over availability. Its recovery point
is the backup timestamp; its recovery time is dominated by archive transfer,
`pg_restore`, and optional re-embedding. Measure both on production-sized data.
The scripts target the repository Compose deployment and fixed `atlas` database
and service names. External PostgreSQL, object storage, online snapshots,
point-in-time recovery, automated encryption/retention, and off-host replication
require deployment-specific runbooks.
