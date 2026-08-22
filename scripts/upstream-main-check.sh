#!/usr/bin/env bash
set -euo pipefail

# Read-only sync check for Hermes upstream vs the running checkout.
# Allowed network action: fetch upstream main only.
# No output = no notification when the checkout is not behind upstream.

GIT="git -C /home/jiddy/.hermes/hermes-agent"
HEARTBEAT="/home/jiddy/.hermes/scripts/.upstream-check-last-success"

fetch_err="$(mktemp)"
trap 'rm -f "$fetch_err"' EXIT

if ! $GIT fetch upstream main --quiet 2>"$fetch_err"; then
  err="$(cat "$fetch_err")"
  printf 'Hermes upstream check FAILED: git fetch upstream main failed: %s\n' "$err"
  exit 1
fi

# head_behind is the number that actually matters: is the checked-out HEAD
# behind upstream. origin_behind / ahead_of_upstream are reported alongside
# it for context but never gate the check on their own.
head_behind="$($GIT rev-list HEAD..upstream/main --count)"
origin_behind="$($GIT rev-list origin/main..upstream/main --count)"
ahead_of_upstream="$($GIT rev-list upstream/main..HEAD --count)"

touch "$HEARTBEAT"

if [[ "$head_behind" == "0" ]]; then
  exit 0
fi

mapfile -t commit_lines < <($GIT log HEAD..upstream/main --oneline)
max_lines=15
total_lines="${#commit_lines[@]}"
show_lines=$(( total_lines < max_lines ? total_lines : max_lines ))

printf 'Hermes upstream check: local checkout (HEAD) is %s commit(s) behind upstream/main.\n' "$head_behind"
printf '(origin/main..upstream/main: %s, upstream/main..HEAD: %s)\n\n' "$origin_behind" "$ahead_of_upstream"
printf 'Commits:\n'
for ((i=0; i<show_lines; i++)); do
  printf '%s\n' "${commit_lines[i]}"
done

if (( total_lines > max_lines )); then
  printf '+%s more\n' "$(( total_lines - max_lines ))"
fi

printf '\nRun the manual upstream-sync process to review and pull these in — this job only checks, it does not merge or apply anything.\n'
