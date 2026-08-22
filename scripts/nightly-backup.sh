#!/usr/bin/env bash
set -euo pipefail

# Nightly encrypted backup of Hermes agent state.
# Silent on success (no_agent cron: empty stdout = no notification).
# On any failure, prints a message to stdout so it gets delivered.

HERMES_HOME="/home/jiddy/.hermes"
SYNCTHING_STATE_DIR="/home/jiddy/.local/state/syncthing"
PRIMARY_DIR="/home/jiddy/backups/hermes-agent"
SECONDARY_DIR="/home/jiddy/hermes-backups-sync"
RECIPIENT_FILE="$PRIMARY_DIR/age-recipient.pub"
KEEP=14

fail() {
  echo "hermes-nightly-backup FAILED: $1"
  exit 1
}

trap 'fail "unexpected error at line $LINENO"' ERR

[[ -f "$RECIPIENT_FILE" ]] || fail "missing age recipient file: $RECIPIENT_FILE"

required_source_files=(
  "$HERMES_HOME/config.yaml"
  "$HERMES_HOME/SOUL.md"
  "$HERMES_HOME/auth.json"
  "$HERMES_HOME/channel_directory.json"
  "$HERMES_HOME/.env"
  "$HERMES_HOME/google_token.json"
  "$HERMES_HOME/google_client_secret.json"
  "$HERMES_HOME/kanban.db"
  "$HERMES_HOME/state.db"
  "$HERMES_HOME/cron/jobs.json"
  "$HERMES_HOME/cron/executions.db"
  "$HERMES_HOME/cron/notepad.db"
  "$HERMES_HOME/cron/skill_sync_exclusions.txt"
  "$SYNCTHING_STATE_DIR/config.xml"
  "$SYNCTHING_STATE_DIR/cert.pem"
  "$SYNCTHING_STATE_DIR/key.pem"
  "$SYNCTHING_STATE_DIR/https-cert.pem"
  "$SYNCTHING_STATE_DIR/https-key.pem"
)
for required_file in "${required_source_files[@]}"; do
  [[ -f "$required_file" ]] || fail "missing required source file: $required_file"
done

required_source_dirs=(
  "$HERMES_HOME/memories"
  "$HERMES_HOME/hooks"
  "$HERMES_HOME/skills"
  "$HERMES_HOME/scripts"
  "$HERMES_HOME/state"
  "$HERMES_HOME/credentials"
)
for required_dir in "${required_source_dirs[@]}"; do
  [[ -d "$required_dir" ]] || fail "missing required source directory: $required_dir"
done

stamp="$(date +%Y-%m-%d)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

staging="$work_dir/hermes-backup-$stamp"
mkdir -p "$staging"

notes=()

snapshot_sqlite() {
  local source_db="$1"
  local target_db="$2"

  [[ -f "$source_db" ]] || fail "missing SQLite database: $source_db"

  # Python's sqlite3.backup() is the online-backup API. It produces a
  # transactionally consistent standalone file even when the source uses WAL.
  python3 - "$source_db" "$target_db" << "PYEOF"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PYEOF
  chmod 600 "$target_db"
}

verify_sqlite() {
  local database="$1"
  python3 - "$database" << "PYEOF"
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = db.execute("PRAGMA quick_check").fetchone()[0]
db.close()
if result != "ok":
    raise SystemExit(f"SQLite quick_check failed for {sys.argv[1]}: {result}")
PYEOF
}

# Consistent snapshot of the WAL-mode kanban DB — never a raw file copy.
# Uses Python's sqlite3.backup() (same online-backup API as the CLI's ".backup"),
# since the sqlite3 CLI binary isn't installed on this host.
snapshot_sqlite "$HERMES_HOME/kanban.db" "$staging/kanban.db"

# Consistent snapshot of the WAL-mode main state DB — same reasoning and
# same online-backup API as kanban.db above. state.db is actively written
# by every running session (confirmed live -shm/-wal sidecars present), so
# a raw cp mid-write risks a torn/inconsistent snapshot. Added 2026-08-12
# after auditing the backup's scope and finding this file — the largest
# single piece of durable state on the system — was never captured at all.
snapshot_sqlite "$HERMES_HOME/state.db" "$staging/state.db"

cp -a \
  "$HERMES_HOME/config.yaml" \
  "$HERMES_HOME/SOUL.md" \
  "$HERMES_HOME/auth.json" \
  "$HERMES_HOME/channel_directory.json" \
  "$HERMES_HOME/memories" \
  "$HERMES_HOME/hooks" \
  "$HERMES_HOME/skills" \
  "$HERMES_HOME/scripts" \
  "$HERMES_HOME/state" \
  "$HERMES_HOME/credentials" \
  "$HERMES_HOME/.env" \
  "$HERMES_HOME/google_token.json" \
  "$HERMES_HOME/google_client_secret.json" \
  "$staging/"

# family_credentials/ and pending/ are legitimately allowed to not exist
# yet (a fresh install before any family member's OAuth is set up, or
# before anything has ever been staged for approval) — guarded rather
# than added to the strict list above, so a genuinely-empty system
# doesn't hard-fail the whole nightly backup over it. Added 2026-08-12
# same audit as state.db above: these were previously uncaptured
# entirely, meaning a restore would have produced a system with zero
# Google credentials for every family identity and an empty approval
# queue while the backup itself reported success every night.
if [[ -d "$HERMES_HOME/family_credentials" ]]; then
  cp -a "$HERMES_HOME/family_credentials" "$staging/"
else
  notes+=("family_credentials/ absent — nothing to back up (no family identity has been set up yet)")
fi

if [[ -d "$HERMES_HOME/pending" ]]; then
  cp -a "$HERMES_HOME/pending" "$staging/"
else
  notes+=("pending/ absent — nothing to back up (no pending approvals exist)")
fi

mkdir -p "$staging/cron"
cp -a \
  "$HERMES_HOME/cron/jobs.json" \
  "$HERMES_HOME/cron/skill_sync_exclusions.txt" \
  "$staging/cron/"
snapshot_sqlite "$HERMES_HOME/cron/executions.db" "$staging/cron/executions.db"
snapshot_sqlite "$HERMES_HOME/cron/notepad.db" "$staging/cron/notepad.db"

# Preserve Syncthing's current peer configuration and device/GUI identity, but
# deliberately exclude its live LevelDB index and lock file. Those hot files are
# rebuildable and are unsafe/noisy to capture with a raw recursive copy.
mkdir -p "$staging/local-state/syncthing"
cp -a \
  "$SYNCTHING_STATE_DIR/config.xml" \
  "$SYNCTHING_STATE_DIR/cert.pem" \
  "$SYNCTHING_STATE_DIR/key.pem" \
  "$SYNCTHING_STATE_DIR/https-cert.pem" \
  "$SYNCTHING_STATE_DIR/https-key.pem" \
  "$staging/local-state/syncthing/"

# Verify every online SQLite snapshot before encryption.
verify_sqlite "$staging/kanban.db"
verify_sqlite "$staging/state.db"
verify_sqlite "$staging/cron/executions.db"
verify_sqlite "$staging/cron/notepad.db"

# Fail closed if any newly-required recovery component is absent from staging.
required_staged_paths=(
  "auth.json"
  "credentials"
  "state"
  "channel_directory.json"
  "scripts"
  "cron/jobs.json"
  "cron/executions.db"
  "cron/notepad.db"
  "cron/skill_sync_exclusions.txt"
  "local-state/syncthing/config.xml"
  "local-state/syncthing/cert.pem"
  "local-state/syncthing/key.pem"
  "local-state/syncthing/https-cert.pem"
  "local-state/syncthing/https-key.pem"
)
for required_path in "${required_staged_paths[@]}"; do
  [[ -e "$staging/$required_path" ]] || fail "required staged path missing: $required_path"
done

# Include a content-integrity manifest. It contains paths and hashes only, never
# file contents, and is verified once before the plaintext tar is created.
(
  cd "$staging"
  find . -type f ! -name BACKUP-MANIFEST.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > BACKUP-MANIFEST.sha256
  sha256sum --check --quiet BACKUP-MANIFEST.sha256
)
chmod 600 "$staging/BACKUP-MANIFEST.sha256"

archive_plain="$work_dir/hermes-backup-$stamp.tar"
tar -cf "$archive_plain" -C "$work_dir" "hermes-backup-$stamp"

archive_enc="hermes-backup-$stamp.tar.age"
age -r "$(cat "$RECIPIENT_FILE")" -o "$work_dir/$archive_enc" "$archive_plain"

mv "$work_dir/$archive_enc" "$PRIMARY_DIR/$archive_enc"

# Prune primary: keep newest $KEEP.
mapfile -t primary_backups < <(ls -1t "$PRIMARY_DIR"/hermes-backup-*.tar.age 2>/dev/null)
if (( ${#primary_backups[@]} > KEEP )); then
  for ((i = KEEP; i < ${#primary_backups[@]}; i++)); do
    rm -f -- "${primary_backups[$i]}"
  done
fi

# Secondary (off-host via Syncthing): copy encrypted archive only, after primary succeeded.
if [[ -d "$SECONDARY_DIR" ]]; then
  cp -a "$PRIMARY_DIR/$archive_enc" "$SECONDARY_DIR/$archive_enc"

  mapfile -t secondary_backups < <(ls -1t "$SECONDARY_DIR"/hermes-backup-*.tar.age 2>/dev/null)
  if (( ${#secondary_backups[@]} > KEEP )); then
    for ((i = KEEP; i < ${#secondary_backups[@]}; i++)); do
      rm -f -- "${secondary_backups[$i]}"
    done
  fi
else
  notes+=("secondary dir $SECONDARY_DIR missing, off-host copy skipped")
fi

if (( ${#notes[@]} > 0 )); then
  echo "hermes-nightly-backup completed with notes (not failures):"
  for n in "${notes[@]}"; do
    echo "  - $n"
  done
fi

exit 0
