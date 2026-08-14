#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 1 ]]; then
  echo "Usage: scripts/backup.sh /path/to/atlas-backup.tar.gz" >&2
  exit 2
fi
archive=$1
[[ "$archive" = /* ]] || archive="$PWD/$archive"
archive_dir=$(dirname "$archive")
mkdir -p "$archive_dir"
if [[ -e "$archive" ]]; then
  echo "Refusing to overwrite existing backup: $archive" >&2
  exit 2
fi

tmp_dir=$(mktemp -d "$archive_dir/.atlas-backup.XXXXXX")
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

for service in api web; do
  grep -qx "$service" <<<"$running_services" && restart_services+=("$service")
done
if [[ ${#restart_services[@]} -gt 0 ]]; then
  docker compose stop "${restart_services[@]}" >/dev/null
fi

docker compose exec -T db pg_isready -U atlas -d atlas >/dev/null
docker compose run --rm --no-deps --entrypoint python api scripts/verify_storage.py
docker compose exec -T db pg_dump -U atlas -d atlas \
  --format=custom --no-owner --no-acl >"$tmp_dir/database.dump"
docker compose run --rm --no-deps --entrypoint tar api \
  --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  --exclude='./.gitkeep' -C /app/uploads -cf - . >"$tmp_dir/uploads.tar"

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

partial="$archive.partial"
tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  -C "$tmp_dir" -czf "$partial" manifest.json SHA256SUMS database.dump uploads.tar
mv "$partial" "$archive"
echo "Backup created: $archive"
