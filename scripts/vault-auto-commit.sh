#!/bin/bash
# Mechanical auto-commit + push for the vault git repo. No LLM reasoning -
# pure git plumbing. Silent when there is nothing new (empty stdout is the
# intended steady state for a 15-minute no-agent cron job). A push failure
# is left to fail the script (set -e) so it surfaces as a real cron error -
# the whole point of this job is the off-machine copy on GitHub, so a
# silently-failing push must not go unnoticed.
set -euo pipefail

VAULT="/home/jiddy/Obsidian Core"
cd "$VAULT"

git add -A

if ! git diff --cached --quiet; then
    git commit -q -m "auto: $(date -Iseconds)"
fi

git push -q origin main
