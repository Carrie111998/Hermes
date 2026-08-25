#!/usr/bin/env bash
# skill-sync: pull/push skills between machines via SSH + rsync
#
# Pull (default): sync.sh <user@host> [user@host2 ...]
# Push:           sync.sh --push <user@host> [user@host2 ...]
# Dry run:        DRY_RUN=1 sync.sh <user@host>
#
# Remotes come from positional args and/or SKILL_SYNC_REMOTES (comma- or
# space-separated). There are no built-in defaults — the script exits with
# usage if no remote is given.
#
# Run doctor.sh first on a new pairing: it verifies Tailscale/SSH/rsync and
# prints the exact fix for whatever is missing.

set -uo pipefail

LOCAL_SKILLS="${HERMES_HOME:-$HOME/.hermes}/skills"
mkdir -p "$LOCAL_SKILLS"

DRY_RUN="${DRY_RUN:-0}"
SSH_TIMEOUT="${SKILL_SYNC_SSH_TIMEOUT:-8}"
SSH_OPTS="-o ConnectTimeout=$SSH_TIMEOUT -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
PUSH_MODE=0
TARGETS=""
TOTAL_ERRORS=0

usage() {
    echo "Usage: sync.sh [--push] <user@host> [user@host2 ...]"
    echo "       DRY_RUN=1 sync.sh <user@host>        # preview only"
    echo "Remotes may also come from SKILL_SYNC_REMOTES (comma/space separated)."
    echo "First time? Run doctor.sh <user@host> to verify connectivity."
}

while [ $# -gt 0 ]; do
    case "$1" in
        --push) PUSH_MODE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) TARGETS="$TARGETS $1" ;;
    esac
    shift
done

if [ -n "${SKILL_SYNC_REMOTES:-}" ]; then
    TARGETS="$TARGETS $(echo "$SKILL_SYNC_REMOTES" | tr ',' ' ')"
fi
# Dedupe, drop empties
TARGETS=$(echo "$TARGETS" | tr ' ' '\n' | awk 'NF && !seen[$0]++')

if [ -z "$TARGETS" ]; then
    echo "No remotes configured."
    usage
    exit 1
fi

try_ssh() {
    local remote="$1"; shift
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$remote" "$@"
}

get_mtime() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

# Archived/backup copies are never synced.
is_excluded() {
    case "$1" in
        *.bak*|*.archive*) return 0 ;;
        *) return 1 ;;
    esac
}

# Resolve the remote skills dir once (honors a remote HERMES_HOME override).
remote_skills_dir() {
    try_ssh "$1" 'echo "${HERMES_HOME:-$HOME/.hermes}/skills"' 2>/dev/null | tail -1
}

# Print "<category/skill>\t<mtime>" per skill on the remote.
remote_skill_list() {
    try_ssh "$1" '
        SKILLS="${HERMES_HOME:-$HOME/.hermes}/skills"
        [ -d "$SKILLS" ] || exit 0
        find "$SKILLS" -name "SKILL.md" -type f | while read -r f; do
            rel="${f#$SKILLS/}"
            dir="${rel%/SKILL.md}"
            mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
            printf "%s\t%s\n" "$dir" "$mtime"
        done
    ' 2>/dev/null
}

do_rsync() {
    # do_rsync <src> <dst> [--delete] — errors are VISIBLE, never swallowed
    local src="$1" dst="$2" del="${3:-}"
    # shellcheck disable=SC2086
    rsync -azL $del -e "ssh $SSH_OPTS" "$src" "$dst"
}

pull_from_remote() {
    local remote="$1"
    local synced=0 skipped=0 new_count=0 errors=0

    echo ">> Pulling from $remote ..."

    local listing rdir
    if ! listing=$(remote_skill_list "$remote"); then
        echo "   [!] SSH failed — run doctor.sh $remote to diagnose"
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        return
    fi
    [ -z "$listing" ] && { echo "   No skills found on remote"; return; }
    rdir=$(remote_skills_dir "$remote")
    [ -z "$rdir" ] && rdir=".hermes/skills"

    while IFS="$(printf '\t')" read -r skill_path remote_mtime; do
        [ -z "$skill_path" ] && continue
        is_excluded "$skill_path" && continue
        skill_name="${skill_path##*/}"
        local_dir="$LOCAL_SKILLS/$skill_path"
        local_skill="$local_dir/SKILL.md"

        if [ -f "$local_skill" ]; then
            local_mtime=$(get_mtime "$local_skill")
            if [ "$remote_mtime" -gt "$local_mtime" ]; then
                age_diff=$(( (remote_mtime - local_mtime) / 60 ))
                echo "   [^] $skill_name  (remote ${age_diff}m newer)"
                synced=$((synced + 1))
                if [ "$DRY_RUN" != "1" ]; then
                    mkdir -p "$local_dir"
                    do_rsync "$remote:$rdir/$skill_path/" "$local_dir/" --delete \
                        || { echo "   [!] rsync failed for $skill_name"; errors=$((errors + 1)); }
                fi
            else
                skipped=$((skipped + 1))
            fi
        else
            echo "   [+] $skill_name  (new, from $skill_path)"
            new_count=$((new_count + 1))
            if [ "$DRY_RUN" != "1" ]; then
                mkdir -p "$local_dir"
                do_rsync "$remote:$rdir/$skill_path/" "$local_dir/" \
                    || { echo "   [!] rsync failed for $skill_name"; errors=$((errors + 1)); }
            fi
        fi
    done <<< "$listing"

    TOTAL_ERRORS=$((TOTAL_ERRORS + errors))
    total=$((synced + new_count))
    if [ "$DRY_RUN" = "1" ]; then
        echo "   Dry run: would update $synced, pull $new_count new; $skipped unchanged."
    else
        echo "   Done: $((synced + new_count - errors)) transferred ($synced updated, $new_count new), $skipped unchanged, $errors errors."
    fi
}

push_to_remote() {
    local remote="$1"
    local synced=0 new_count=0 skipped=0 errors=0

    echo ">> Pushing to $remote ..."

    local rdir
    if ! rdir=$(remote_skills_dir "$remote") || [ -z "$rdir" ]; then
        echo "   [!] SSH failed — run doctor.sh $remote to diagnose"
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        return
    fi
    try_ssh "$remote" "mkdir -p '$rdir'" 2>/dev/null

    local remote_listing local_listing
    remote_listing=$(remote_skill_list "$remote" || true)
    local_listing=$(find "$LOCAL_SKILLS" -name "SKILL.md" -type f)
    [ -z "$local_listing" ] && { echo "   No local skills to push"; return; }

    # NOTE: loop reads from a heredoc, NOT a pipe — counters survive.
    while read -r f; do
        [ -z "$f" ] && continue
        rel="${f#$LOCAL_SKILLS/}"
        skill_path="${rel%/SKILL.md}"
        is_excluded "$skill_path" && continue
        skill_name="${skill_path##*/}"
        local_mtime=$(get_mtime "$f")

        remote_mtime=0
        if [ -n "$remote_listing" ]; then
            match=$(printf '%s\n' "$remote_listing" | grep "^${skill_path}$(printf '\t')" || true)
            [ -n "$match" ] && remote_mtime=$(printf '%s' "$match" | cut -f2)
        fi

        if [ "$local_mtime" -gt "$remote_mtime" ]; then
            if [ "$remote_mtime" -eq 0 ]; then
                echo "   [+] $skill_name  (new on remote)"
                new_count=$((new_count + 1))
            else
                age_diff=$(( (local_mtime - remote_mtime) / 60 ))
                echo "   [^] $skill_name  (local ${age_diff}m newer)"
                synced=$((synced + 1))
            fi
            if [ "$DRY_RUN" != "1" ]; then
                try_ssh "$remote" "mkdir -p '$rdir/$skill_path'" 2>/dev/null
                do_rsync "$LOCAL_SKILLS/$skill_path/" "$remote:$rdir/$skill_path/" --delete \
                    || { echo "   [!] rsync failed for $skill_name"; errors=$((errors + 1)); }
            fi
        else
            skipped=$((skipped + 1))
        fi
    done <<< "$local_listing"

    TOTAL_ERRORS=$((TOTAL_ERRORS + errors))
    total=$((synced + new_count))
    if [ "$DRY_RUN" = "1" ]; then
        echo "   Dry run: would update $synced, push $new_count new; $skipped unchanged."
        return
    fi
    echo "   Done: $((total - errors)) transferred ($synced updated, $new_count new), $skipped unchanged, $errors errors."

    # Verify on the remote — never trust a silent rsync loop.
    local remote_count
    remote_count=$(try_ssh "$remote" 'find "${HERMES_HOME:-$HOME/.hermes}/skills" -name SKILL.md 2>/dev/null | wc -l' 2>/dev/null | tr -d ' ')
    local local_count
    local_count=$(printf '%s\n' "$local_listing" | wc -l | tr -d ' ')
    echo "   Verify: remote now has $remote_count skills (local: $local_count)."
}

for target in $TARGETS; do
    if [ "$PUSH_MODE" -eq 1 ]; then
        push_to_remote "$target"
    else
        pull_from_remote "$target"
    fi
done

if [ "$TOTAL_ERRORS" -gt 0 ]; then
    echo "!! Completed with $TOTAL_ERRORS error(s)."
    exit 1
fi
