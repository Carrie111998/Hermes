#!/bin/bash
# shellcheck shell=bash

# Sanitize a raw directory name into a safe identifier
sanitize_profile_name() {
  local raw="$1"
  local base="${raw##*/}"
  base="${base#.}"
  local name
  name="$(printf '%s' "$base" | tr -cs '[:alnum:]_' '_')"
  name="${name%_}"
  printf '%s' "$name"
}

resolve_profiles() {
  PROFILE_DIRS=()
  if [ -f "/data/options.json" ]; then
    while IFS= read -r _line; do
      [ -n "$_line" ] && PROFILE_DIRS+=("$_line")
    done < <(jq -r '.profiles[]? // empty' "/data/options.json")
  fi

  if [ "${#PROFILE_DIRS[@]}" -eq 0 ]; then
    PROFILE_DIRS=(".")
  fi

  PROFILE_NAMES=()
  PROFILE_HOMES=()
  PROFILE_SRC_DIRS=()
  PROFILE_VENV_DIRS=()
  PROFILE_PATH_PREFIX=()

  for i in "${!PROFILE_DIRS[@]}"; do
    local dir="${PROFILE_DIRS[$i]}"
    local name="$(sanitize_profile_name "$dir")"
    [ -z "$name" ] && name="profile_$i"

    PROFILE_NAMES[i]="$name"
    PROFILE_HOMES[i]="$HOME/$dir"
    PROFILE_SRC_DIRS[i]="$INSTALL_DIR"
    PROFILE_VENV_DIRS[i]="$INSTALL_DIR/.venv"
    if [ "$i" -eq 0 ]; then
      PROFILE_PATH_PREFIX[i]=""
    else
      PROFILE_PATH_PREFIX[i]="/profile/$name"
    fi
  done

  API_PORTS=()
  TTYD_HERMES_PORTS=()
  TTYD_TERMINAL_PORTS=()
  DASHBOARD_PORTS=()
  for i in "${!PROFILE_DIRS[@]}"; do
    API_PORTS[i]=$((8642 + i))
    TTYD_HERMES_PORTS[i]=$((49269 + i))
    TTYD_TERMINAL_PORTS[i]=$((49369 + i))
    DASHBOARD_PORTS[i]=$((49469 + i))
  done
}
