#!/usr/bin/env bash
# skill-sync doctor: verify everything a sync needs, and print the exact fix
# for whatever is missing.
#
# Usage: doctor.sh <user@host> [user@host2 ...]
#
# Checks, in order:
#   local : ssh client, rsync, an SSH keypair, Tailscale state (optional)
#   remote: SSH reachability + key auth, rsync, skills dir
#
# Exit 0 when every remote is ready to sync; 1 otherwise.

set -uo pipefail

SSH_TIMEOUT="${SKILL_SYNC_SSH_TIMEOUT:-8}"
SSH_OPTS="-o ConnectTimeout=$SSH_TIMEOUT -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
FAILURES=0

ok()   { echo "  [ok] $1"; }
warn() { echo "  [--] $1"; }
bad()  { echo "  [!!] $1"; FAILURES=$((FAILURES + 1)); }
fix()  { echo "        fix: $1"; }

if [ $# -eq 0 ]; then
    echo "Usage: doctor.sh <user@host> [user@host2 ...]"
    echo ""
    echo "Tip: pick the right login. With Tailscale, the hostname is the machine's"
    echo "node name ('tailscale status', first column) but the USER is that box's"
    echo "unix login — they usually differ. Probe candidates with:"
    echo "  ssh -o BatchMode=yes -o ConnectTimeout=8 <user>@<host> 'echo OK'"
    exit 1
fi

echo "== Local checks =="

command -v ssh >/dev/null 2>&1 && ok "ssh client present" || {
    bad "ssh client missing"
    fix "install OpenSSH (macOS: built-in; Debian/Ubuntu: sudo apt install openssh-client)"
}

command -v rsync >/dev/null 2>&1 && ok "rsync present" || {
    bad "rsync missing"
    fix "macOS: built-in / brew install rsync; Debian/Ubuntu: sudo apt install rsync"
}

if ls "$HOME"/.ssh/id_* >/dev/null 2>&1; then
    ok "SSH keypair found in ~/.ssh"
else
    bad "no SSH keypair in ~/.ssh"
    fix "ssh-keygen -t ed25519   (then: ssh-copy-id <user@host> for each remote)"
fi

if command -v tailscale >/dev/null 2>&1; then
    ts_state=$(tailscale status --json 2>/dev/null | grep -o '"BackendState": *"[^"]*"' | head -1 | sed 's/.*"BackendState": *"//;s/"//')
    case "$ts_state" in
        Running) ok "Tailscale running" ;;
        "")      warn "Tailscale installed but status unreadable" ;;
        *)       bad "Tailscale installed but state is '$ts_state'"
                 fix "tailscale up   (log in with the same tailnet on every machine)" ;;
    esac
else
    warn "Tailscale not installed (optional — any SSH route works)"
    fix "recommended for cross-network sync: https://tailscale.com/download, then 'tailscale up' on every machine"
fi

for remote in "$@"; do
    echo ""
    echo "== Remote: $remote =="

    # shellcheck disable=SC2086
    out=$(ssh $SSH_OPTS "$remote" 'echo __SKILL_SYNC_OK__' 2>&1)
    if printf '%s' "$out" | grep -q '__SKILL_SYNC_OK__'; then
        ok "SSH key auth works"
    else
        case "$out" in
            *"Permission denied"*)
                bad "SSH reachable but key NOT authorized ($remote)"
                fix "ssh-copy-id $remote   (enter the password once)"
                fix "wrong user? Tailscale login name != unix user — try the box's actual unix login, e.g. ssh <unixuser>@${remote#*@}"
                ;;
            *"Connection refused"*)
                bad "host up but sshd not listening ($remote)"
                fix "macOS remote: System Settings > General > Sharing > Remote Login ON (or: sudo systemsetup -setremotelogin on)"
                fix "Linux remote: sudo apt install openssh-server && sudo systemctl enable --now ssh"
                fix "or use Tailscale SSH: run 'sudo tailscale set --ssh' on the remote"
                fix "or flip direction: run the sync FROM that machine, pulling from this one"
                ;;
            *"Could not resolve hostname"*)
                bad "hostname does not resolve ($remote)"
                fix "use the Tailscale node name ('tailscale status', first column) or its 100.x.y.z IP"
                ;;
            *"timed out"*|*"Operation timed out"*|*"No route to host"*)
                bad "host unreachable ($remote)"
                fix "check 'tailscale status' on BOTH machines; 'tailscale ping ${remote#*@}'; is the box awake?"
                ;;
            *)
                bad "SSH failed ($remote): $(printf '%s' "$out" | head -1)"
                fix "run manually to see the full error: ssh -v $remote 'echo OK'"
                ;;
        esac
        continue
    fi

    # shellcheck disable=SC2086
    if ssh $SSH_OPTS "$remote" 'command -v rsync' >/dev/null 2>&1; then
        ok "remote rsync present"
    else
        bad "remote rsync missing"
        fix "install rsync on $remote (Debian/Ubuntu: sudo apt install rsync)"
    fi

    # shellcheck disable=SC2086
    count=$(ssh $SSH_OPTS "$remote" 'find "${HERMES_HOME:-$HOME/.hermes}/skills" -name SKILL.md 2>/dev/null | wc -l' 2>/dev/null | tr -d ' ')
    if [ -n "$count" ] && [ "$count" -gt 0 ] 2>/dev/null; then
        ok "remote skills dir present ($count skills)"
    else
        warn "remote has no ~/.hermes/skills yet (fine for a first push — it will be created)"
    fi
done

echo ""
if [ "$FAILURES" -gt 0 ]; then
    echo "Doctor: $FAILURES problem(s) found. Apply the fixes above, then re-run."
    exit 1
fi
echo "Doctor: all checks passed. Ready to sync."
