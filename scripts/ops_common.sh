#!/usr/bin/env bash

# Shared primitives for backup/restore. This file is sourced; callers own
# cleanup and human-facing error messages.

ops_install_signal_handlers() {
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

ops_validate_private_directory() {
  local directory=$1 owner mode
  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  owner=$(stat -c '%u' -- "$directory") || return 1
  mode=$(stat -c '%a' -- "$directory") || return 1
  [[ "$owner" == "$(id -u)" ]] || return 1
  (( (8#$mode & 077) == 0 )) || return 1
}

ops_runtime_lock_root() {
  local uid runtime_root fallback_base
  uid=$(id -u) || return 3
  if [[ -n ${XDG_RUNTIME_DIR:-} ]]; then
    ops_validate_private_directory "$XDG_RUNTIME_DIR" || return 3
    printf '%s\n' "$XDG_RUNTIME_DIR"
    return 0
  fi

  runtime_root="/run/user/$uid"
  if ops_validate_private_directory "$runtime_root"; then
    printf '%s\n' "$runtime_root"
    return 0
  fi

  # Minimal containers and CI runners often have no login-session runtime
  # directory. Create a private per-UID directory without ever following or
  # replacing a pre-existing symlink.
  fallback_base=${TMPDIR:-/tmp}
  runtime_root="$fallback_base/atlas-wiki-runtime-$uid"
  [[ -d "$fallback_base" && ! -L "$fallback_base" ]] || return 3
  if [[ ! -e "$runtime_root" && ! -L "$runtime_root" ]]; then
    (umask 077; mkdir -- "$runtime_root") || return 3
  fi
  ops_validate_private_directory "$runtime_root" || return 3
  printf '%s\n' "$runtime_root"
}

ops_default_lock_file() {
  local lock_root lock_id project_name
  lock_root=$(ops_runtime_lock_root) || return 3
  # COMPOSE_PROJECT_NAME is the resource namespace shared by the database and
  # volumes. Falling back to Compose's ordinary directory-basename default
  # keeps local invocations compatible while avoiding a full-path identity.
  project_name=${COMPOSE_PROJECT_NAME:-$(basename -- "$(pwd -P)")}
  [[ -n "$project_name" ]] || return 3
  lock_id=$(printf '%s' "$project_name" | sha256sum | awk '{print substr($1,1,24)}') || return 3
  printf '%s/atlas-wiki-ops-%s.lock\n' "$lock_root" "$lock_id"
}

ops_acquire_lock() {
  local lock_file parent canonical_parent normalized_parent old_umask path_inode fd_inode owner mode
  command -v flock >/dev/null 2>&1 || return 3

  if [[ -n ${ATLAS_OPS_LOCK_FILE:-} ]]; then
    lock_file=$ATLAS_OPS_LOCK_FILE
  else
    lock_file=$(ops_default_lock_file) || return 3
  fi
  [[ "$lock_file" == /* ]] || lock_file="$PWD/$lock_file"
  parent=$(dirname -- "$lock_file")
  canonical_parent=$(realpath -e -- "$parent") || return 3
  normalized_parent=$(realpath -m -- "$parent") || return 3
  [[ "$canonical_parent" == "$normalized_parent" ]] || return 3
  ops_validate_private_directory "$canonical_parent" || return 3
  lock_file="$canonical_parent/$(basename -- "$lock_file")"
  [[ ! -L "$lock_file" ]] || return 3
  if [[ -e "$lock_file" ]]; then
    [[ -f "$lock_file" ]] || return 3
    owner=$(stat -c '%u' -- "$lock_file") || return 3
    mode=$(stat -c '%a' -- "$lock_file") || return 3
    [[ "$owner" == "$(id -u)" ]] || return 3
    (( (8#$mode & 077) == 0 )) || return 3
  fi

  # Append-open does not truncate a target even if an attacker races the
  # pre-open checks. Verify the opened inode still matches the non-symlink path
  # before using it as the lock.
  old_umask=$(umask)
  umask 077
  exec 9>>"$lock_file" || { umask "$old_umask"; return 3; }
  umask "$old_umask"
  [[ -f "$lock_file" && ! -L "$lock_file" ]] || { exec 9>&-; return 3; }
  path_inode=$(stat -Lc '%d:%i' -- "$lock_file") || { exec 9>&-; return 3; }
  fd_inode=$(stat -Lc '%d:%i' -- "/proc/$$/fd/9") || { exec 9>&-; return 3; }
  [[ "$path_inode" == "$fd_inode" ]] || { exec 9>&-; return 3; }
  owner=$(stat -Lc '%u' -- "/proc/$$/fd/9") || { exec 9>&-; return 3; }
  mode=$(stat -Lc '%a' -- "/proc/$$/fd/9") || { exec 9>&-; return 3; }
  [[ "$owner" == "$(id -u)" ]] || { exec 9>&-; return 3; }
  (( (8#$mode & 077) == 0 )) || { exec 9>&-; return 3; }
  flock -n 9 || return 4
}

ops_age_binary() {
  if [[ -n ${ATLAS_AGE_BIN:-} ]]; then
    command -v "$ATLAS_AGE_BIN"
  elif command -v age >/dev/null 2>&1; then
    command -v age
  elif command -v rage >/dev/null 2>&1; then
    command -v rage
  else
    return 1
  fi
}

ops_encrypt_file() {
  local recipient=$1 source=$2 destination=$3 age_bin
  age_bin=$(ops_age_binary) || return 3
  "$age_bin" --encrypt --recipient "$recipient" --output "$destination" "$source"
}

ops_decrypt_file() {
  local identity=$1 source=$2 destination=$3 age_bin owner mode
  age_bin=$(ops_age_binary) || return 3
  [[ -f "$identity" && ! -L "$identity" && -r "$identity" ]] || return 3
  owner=$(stat -c '%u' -- "$identity") || return 3
  mode=$(stat -c '%a' -- "$identity") || return 3
  [[ "$owner" == "$(id -u)" ]] || return 3
  (( (8#$mode & 077) == 0 )) || return 3
  "$age_bin" --decrypt --identity "$identity" --output "$destination" "$source"
}
