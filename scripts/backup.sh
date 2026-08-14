#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source "$(dirname "${BASH_SOURCE[0]}")/ops_common.sh"
ops_install_signal_handlers

usage() {
  echo "Usage: scripts/backup.sh [--recipient AGE_RECIPIENT | --allow-plaintext] ARCHIVE" >&2
  exit 2
}

allow_plaintext=false
recipient=${ATLAS_BACKUP_AGE_RECIPIENT:-}
while [[ $# -gt 0 ]]; do
  case $1 in
    --recipient)
      [[ $# -ge 2 && -n $2 ]] || usage
      recipient=$2
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
if [[ $allow_plaintext == true && -n $recipient ]]; then
  echo "Choose encryption or --allow-plaintext, not both" >&2
  exit 2
fi
if [[ $allow_plaintext == false && -z $recipient ]]; then
  echo "Encrypted backup requires --recipient or ATLAS_BACKUP_AGE_RECIPIENT" >&2
  exit 3
fi
if [[ $allow_plaintext == false ]] && ! ops_age_binary >/dev/null; then
  echo "Encrypted backup requires age or rage" >&2
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
archive_dir=$(dirname "$archive")
mkdir -p "$archive_dir"
if [[ -e $archive || -e $archive.partial ]]; then
  echo "Refusing to overwrite existing or partial backup: $archive" >&2
  exit 2
fi

tmp_dir=$(mktemp -d "$archive_dir/.atlas-backup.XXXXXX")
chmod 700 "$tmp_dir"
partial=$archive.partial
running_services=$(docker compose ps --status running --services 2>/dev/null || true)
restart_services=()
failure_code=5
cleanup() {
  status=$?
  trap - EXIT ERR
  trap '' INT TERM
  restart_failed=false
  if [[ ${#restart_services[@]} -gt 0 ]]; then
    docker compose start "${restart_services[@]}" >/dev/null || restart_failed=true
  fi
  rm -f -- "$partial"
  rm -rf -- "$tmp_dir"
  if [[ $restart_failed == true ]]; then
    echo "Backup finished but prior services could not be restored" >&2
    [[ $status == 0 ]] && status=9
  fi
  exit "$status"
}
on_error() {
  exit "$failure_code"
}
trap cleanup EXIT
trap on_error ERR

for service in api web; do
  grep -qx "$service" <<<"$running_services" && restart_services+=("$service")
done
if [[ ${#restart_services[@]} -gt 0 ]]; then
  docker compose stop "${restart_services[@]}" >/dev/null
fi

if ! docker compose exec -T db pg_isready -U atlas -d atlas >/dev/null; then
  echo "Database is unavailable for backup" >&2
  exit 3
fi
docker compose run --rm --no-deps --entrypoint python api scripts/verify_storage.py
docker compose exec -T db pg_dump -U atlas -d atlas \
  --format=custom --no-owner --no-acl >"$tmp_dir/database.dump"
docker compose exec -T db pg_restore --list <"$tmp_dir/database.dump" >/dev/null
docker compose run --rm --no-deps --quiet --no-TTY --entrypoint tar api \
  --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  --exclude='./.gitkeep' -C /app/uploads -cf - . >"$tmp_dir/uploads.tar"
tar -tf "$tmp_dir/uploads.tar" >/dev/null

git_sha=$(git rev-parse HEAD 2>/dev/null || printf 'unknown')
alembic_version=$(docker compose exec -T db psql -U atlas -d atlas -Atqc \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || printf 'unknown')
created_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
db_sha=$(sha256sum "$tmp_dir/database.dump" | awk '{print $1}')
uploads_sha=$(sha256sum "$tmp_dir/uploads.tar" | awk '{print $1}')
printf '%s  %s\n%s  %s\n' \
  "$db_sha" database.dump "$uploads_sha" uploads.tar >"$tmp_dir/SHA256SUMS"
cat >"$tmp_dir/manifest.json" <<EOF
{"format_version":1,"created_at":"$created_at","git_sha":"$git_sha","alembic_version":"$alembic_version"}
EOF

tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  -C "$tmp_dir" -czf "$tmp_dir/bundle.tar.gz" manifest.json SHA256SUMS database.dump uploads.tar
if [[ $allow_plaintext == true ]]; then
  echo "WARNING: publishing an explicitly requested plaintext development backup" >&2
  cp -- "$tmp_dir/bundle.tar.gz" "$partial"
else
  failure_code=6
  ops_encrypt_file "$recipient" "$tmp_dir/bundle.tar.gz" "$partial"
fi
# Publication is the commit point. Ignore signals only for the atomic rename so
# an interrupt cannot report failure while leaving a newly published archive.
trap '' INT TERM
mv -- "$partial" "$archive"
echo "Backup created: $archive"
