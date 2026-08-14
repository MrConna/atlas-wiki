#!/usr/bin/env bash

# Shared primitives for backup/restore. This file is sourced; callers own
# cleanup and human-facing error messages.

ops_install_signal_handlers() {
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

ops_default_lock_file() {
  local lock_root lock_id
  lock_root=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}
  lock_id=$(printf '%s' "$PWD" | sha256sum | awk '{print substr($1,1,24)}')
  printf '%s/atlas-wiki-ops-%s.lock\n' "$lock_root" "$lock_id"
}

ops_acquire_lock() {
  local lock_file=${ATLAS_OPS_LOCK_FILE:-$(ops_default_lock_file)}
  command -v flock >/dev/null 2>&1 || return 3
  exec 9>"$lock_file" || return 3
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
  local identity=$1 source=$2 destination=$3 age_bin
  age_bin=$(ops_age_binary) || return 3
  [[ -f "$identity" && ! -L "$identity" && -r "$identity" ]] || return 3
  "$age_bin" --decrypt --identity "$identity" --output "$destination" "$source"
}
