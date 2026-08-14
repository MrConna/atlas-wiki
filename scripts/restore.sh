#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: scripts/restore.sh /path/to/atlas-backup.tar.gz" >&2
  exit 2
fi
archive=$1
if [[ ! -f "$archive" ]]; then
  echo "Backup not found: $archive" >&2
  exit 2
fi
if find uploads -mindepth 1 -maxdepth 1 ! -name .gitkeep -print -quit | grep -q .; then
  echo "Restore requires an empty uploads directory; refusing to overwrite files" >&2
  exit 2
fi

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/atlas-restore.XXXXXX")
running_services=$(docker compose ps --status running --services 2>/dev/null || true)
restart_services=()
cleanup() {
  status=$?
  if [[ ${#restart_services[@]} -gt 0 ]]; then
    docker compose start "${restart_services[@]}" >/dev/null || true
  fi
  rm -rf -- "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT INT TERM

python3 scripts/unpack_backup.py "$archive" "$tmp_dir"
for required in manifest.json SHA256SUMS database.dump uploads.tar; do
  if [[ ! -f "$tmp_dir/$required" ]]; then
    echo "Invalid backup: missing $required" >&2
    exit 2
  fi
done
(cd "$tmp_dir" && sha256sum -c SHA256SUMS)

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
if [[ "$database_ready" != true ]]; then
  echo "Database did not become ready for restore" >&2
  exit 3
fi
has_pages=$(docker compose exec -T db psql -U atlas -d atlas -Atqc \
  "SELECT CASE WHEN to_regclass('public.pages') IS NULL THEN 'no' ELSE 'yes' END")
if [[ "$has_pages" == yes ]]; then
  page_count=$(docker compose exec -T db psql -U atlas -d atlas -Atqc "SELECT count(*) FROM pages")
  if [[ "$page_count" != 0 ]]; then
    echo "Restore requires an empty database; refusing to overwrite $page_count page(s)" >&2
    exit 2
  fi
fi

docker compose exec -T db psql -U atlas -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'atlas' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS atlas;
CREATE DATABASE atlas OWNER atlas;
SQL
docker compose exec -T db pg_restore -U atlas -d atlas --single-transaction \
  --no-owner --no-acl --exit-on-error \
  <"$tmp_dir/database.dump"
python3 scripts/unpack_uploads.py "$tmp_dir/uploads.tar" uploads
docker compose run --rm --no-deps --entrypoint python api scripts/verify_storage.py
echo "Restore completed. Start the API and run: docker compose exec api python scripts/verify_storage.py"
