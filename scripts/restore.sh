#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source "$(dirname "${BASH_SOURCE[0]}")/ops_common.sh"
ops_install_signal_handlers

usage() {
  echo "Usage: scripts/restore.sh [--identity AGE_IDENTITY_FILE | --allow-plaintext] ARCHIVE" >&2
  exit 2
}

allow_plaintext=false
identity=${ATLAS_RESTORE_AGE_IDENTITY:-}
while [[ $# -gt 0 ]]; do
  case $1 in
    --identity)
      [[ $# -ge 2 && -n $2 ]] || usage
      identity=$2
      shift 2
      ;;
    --allow-plaintext)
      allow_plaintext=true
      shift
      ;;
    --help|-h) usage ;;
    --*) usage ;;
    *)
      [[ -z ${archive_arg:-} ]] || usage
      archive_arg=$1
      shift
      ;;
  esac
done
[[ -n ${archive_arg:-} ]] || usage
if [[ $allow_plaintext == true && -n $identity ]]; then
  echo "Choose decryption or --allow-plaintext, not both" >&2
  exit 2
fi
if [[ $allow_plaintext == false && -z $identity ]]; then
  echo "Encrypted restore requires --identity or ATLAS_RESTORE_AGE_IDENTITY" >&2
  exit 3
fi
if [[ $allow_plaintext == false ]] && ! ops_age_binary >/dev/null; then
  echo "Encrypted restore requires age or rage" >&2
  exit 3
fi
if ops_acquire_lock; then
  :
else
  lock_status=$?
  [[ $lock_status == 4 ]] && echo "Another Atlas backup or restore is running" >&2
  exit "$lock_status"
fi

archive=$archive_arg
[[ $archive = /* ]] || archive="$PWD/$archive"
if [[ ! -f $archive || -L $archive ]]; then
  echo "Backup is missing or unsafe" >&2
  exit 2
fi
if find uploads -mindepth 1 -maxdepth 1 ! -name .gitkeep -print -quit | grep -q .; then
  echo "Restore requires an empty uploads directory; refusing to overwrite files" >&2
  exit 2
fi

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/atlas-restore.XXXXXX")
chmod 700 "$tmp_dir"
mkdir "$tmp_dir/content"
uploads_stage=$(mktemp -d "$PWD/.atlas-uploads-restore.XXXXXX")
chmod 700 "$uploads_stage"
running_services=$(docker compose ps --status running --services 2>/dev/null || true)
restart_services=()
mutation_started=false
restore_succeeded=false
failure_code=6
cleanup() {
  status=$?
  trap - EXIT ERR
  trap '' INT TERM
  restart_failed=false
  if [[ ${#restart_services[@]} -gt 0 && ( $mutation_started == false || $restore_succeeded == true ) ]]; then
    docker compose start "${restart_services[@]}" >/dev/null || restart_failed=true
  fi
  rm -rf -- "$tmp_dir" "$uploads_stage"
  if [[ $status != 0 && $mutation_started == true ]]; then
    echo "Restore failed after mutation began; application services remain stopped" >&2
  fi
  if [[ $restart_failed == true ]]; then
    echo "Restore validation finished but prior services could not be restored" >&2
    [[ $status == 0 ]] && status=9
  fi
  exit "$status"
}
on_error() {
  exit "$failure_code"
}
trap cleanup EXIT
trap on_error ERR

# Decryption/copy and all archive validation occur in a private temporary
# directory before services stop or the target database is changed.
if [[ $allow_plaintext == true ]]; then
  cp -- "$archive" "$tmp_dir/bundle.tar.gz"
else
  ops_decrypt_file "$identity" "$archive" "$tmp_dir/bundle.tar.gz"
fi
python3 scripts/unpack_backup.py "$tmp_dir/bundle.tar.gz" "$tmp_dir/content"
for required in manifest.json SHA256SUMS database.dump uploads.tar; do
  if [[ ! -f "$tmp_dir/content/$required" ]]; then
    echo "Invalid backup: missing $required" >&2
    exit 6
  fi
done
(cd "$tmp_dir/content" && sha256sum -c SHA256SUMS)
docker compose run --rm --no-deps --entrypoint python \
  -v "$tmp_dir/content:/backup:ro" api scripts/check_backup_revision.py /backup/manifest.json
python3 scripts/unpack_uploads.py "$tmp_dir/content/uploads.tar" "$uploads_stage"

# Validate the custom dump with the exact pg_restore shipped by the database
# image before any destructive command is allowed to run.
if ! docker compose exec -T db pg_isready -U atlas -d postgres >/dev/null; then
  echo "Database is unavailable for restore validation" >&2
  exit 3
fi
docker compose exec -T db pg_restore --list <"$tmp_dir/content/database.dump" >/dev/null

for service in api web; do
  grep -qx "$service" <<<"$running_services" && restart_services+=("$service")
done
if [[ ${#restart_services[@]} -gt 0 ]]; then
  docker compose stop "${restart_services[@]}" >/dev/null
fi

database_ready=false
for _attempt in $(seq 1 60); do
  if docker compose exec -T db pg_isready -U atlas -d postgres >/dev/null 2>&1; then
    database_ready=true
    break
  fi
  sleep 1
done
if [[ $database_ready != true ]]; then
  echo "Database did not become ready for restore" >&2
  exit 3
fi
public_objects=$(docker compose exec -T db psql -U atlas -d atlas -Atqc \
  "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public') + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public') + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public')")
if [[ $public_objects != 0 ]]; then
  echo "Restore requires a database with an empty public schema; refusing to drop $public_objects object(s)" >&2
  exit 2
fi

failure_code=7
mutation_started=true
docker compose exec -T db psql -U atlas -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'atlas' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS atlas;
CREATE DATABASE atlas OWNER atlas;
SQL
docker compose exec -T db pg_restore -U atlas -d atlas --single-transaction \
  --no-owner --no-acl --exit-on-error \
  <"$tmp_dir/content/database.dump"
failure_code=8
docker compose run --rm --no-deps --entrypoint alembic api upgrade head
mv uploads "$tmp_dir/original-uploads"
mv "$uploads_stage" uploads
uploads_stage="$tmp_dir/published-uploads"
docker compose run --rm --no-deps --entrypoint python api scripts/verify_storage.py
restore_succeeded=true
echo "Restore completed. Start the API and verify /api/v1/ready before use."
