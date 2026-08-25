#!/usr/bin/env python3
"""P2P skill-sync triage: list, filter, classify, and print a per-direction picker.

Unlike sync.sh (all-or-nothing per direction), this:
  - lists local + each remote's skills by mtime
  - filters out .bak / .archive and any user-named excludes
  - intersects remote-pull candidates with the REMOTE-READABLE set (so
    permission-restricted skills are reported, not silently dropped)
  - classifies each remote-newer skill as NEW / superset / divergent (needs merge)
  - prints a numbered picker for PULL and PUSH per remote

Usage:
  python3 p2p_sync.py user@host [user@host2 ...]
  python3 p2p_sync.py --exclude some-skill user@host

It only REPORTS. The agent reads the picker, gets the user's selection, then
executes rsync (clean copies) + union merges (divergent) itself. Nothing is
written or transferred by this script.
"""
import difflib
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
LOCAL = os.path.join(os.environ.get("HERMES_HOME", os.path.join(HOME, ".hermes")), "skills")
SSH = "ssh -o ConnectTimeout=8 -o BatchMode=yes"

EXCLUDE_SUBSTR = [".bak", ".archive"]  # always skipped


def parse_args(argv):
    excludes, remotes = [], []
    i = 0
    while i < len(argv):
        if argv[i] == "--exclude":
            excludes.append(argv[i + 1])
            i += 2
        else:
            remotes.append(argv[i])
            i += 1
    return excludes, remotes


def is_excluded(path, name_excludes):
    if any(s in path for s in EXCLUDE_SUBSTR):
        return True
    return any(path.endswith(x) or f"/{x}" in f"/{path}" for x in name_excludes)


def listing(host):
    """Return {skill_path: mtime} for local or a remote host."""
    if host == "local":
        d = {}
        for root, _dirs, files in os.walk(LOCAL, followlinks=True):
            if "SKILL.md" in files:
                p = os.path.join(root, "SKILL.md")
                rel = os.path.relpath(root, LOCAL)
                d[rel] = int(os.stat(p).st_mtime)
        return d
    rem = (
        'SK="${HERMES_HOME:-$HOME/.hermes}/skills"; [ -d "$SK" ] || exit 0; '
        "find \"$SK\" -name SKILL.md -type f | while read -r f; do "
        'rel="${f#$SK/}"; d="${rel%/SKILL.md}"; '
        'm=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f"); printf "%s\\t%s\\n" "$d" "$m"; done'
    )
    out = subprocess.run([*SSH.split(), host, rem], capture_output=True, text=True).stdout
    d = {}
    for line in out.strip().splitlines():
        if "\t" in line:
            p, m = line.rsplit("\t", 1)
            d[p] = int(m)
    return d


def readable_set(host):
    """Skills on the remote whose SKILL.md is readable by the SSH user."""
    rem = (
        'cd "${HERMES_HOME:-$HOME/.hermes}/skills" 2>/dev/null && '
        "for f in $(find . -name SKILL.md -readable -type f 2>/dev/null || find . -name SKILL.md -type f); "
        'do echo "${f#./}" | sed "s|/SKILL.md||"; done'
    )
    out = subprocess.run([*SSH.split(), host, rem], capture_output=True, text=True).stdout
    return set(out.split())


def classify(local_md, remote_md):
    """NEW handled by caller. Here: 'identical' | 'superset' | 'divergent'."""
    with open(local_md) as fh:
        L = fh.read().splitlines()
    with open(remote_md) as fh:
        R = fh.read().splitlines()
    if L == R:
        return "identical"
    diff = list(difflib.unified_diff(L, R, lineterm=""))
    removed = sum(
        1 for d in diff if d.startswith("-") and not d.startswith("---") and d.strip() != "-"
    )
    return "superset" if removed == 0 else "divergent"


def main():
    excludes, remotes = parse_args(sys.argv[1:])
    if not remotes:
        print("usage: p2p_sync.py [--exclude NAME]... user@host [user@host2 ...]")
        sys.exit(1)

    L = listing("local")
    for host in remotes:
        print("=" * 72)
        print(f"REMOTE: {host}")
        print("=" * 72)
        R = listing(host)
        if not R:
            print("  [!] no skills / SSH failed — run doctor.sh to diagnose")
            continue
        rd = readable_set(host)

        pull_new, pull_upd, skipped_perms = [], [], []
        for p, m in sorted(R.items()):
            if is_excluded(p, excludes):
                continue
            if p not in rd:
                skipped_perms.append(p)
                continue
            if p not in L:
                pull_new.append(p)
            elif m > L[p]:
                pull_upd.append((p, (m - L[p]) // 60))

        push = []
        for p, m in sorted(L.items()):
            if is_excluded(p, excludes):
                continue
            if p not in R:
                push.append((p, "NEW"))
            elif L[p] > m:
                push.append((p, f"upd {(L[p] - m) // 60}m"))

        n = 0
        print(f"\n-- PULL <- {host}: {len(pull_new)} new, {len(pull_upd)} updated --")
        for p in pull_new:
            n += 1
            print(f"{n:>3}. {p:<55} [NEW]")
        for p, age in pull_upd:
            n += 1
            print(f"{n:>3}. {p:<55} [upd {age}m — diff to bucket it]")
        if skipped_perms:
            print(f"\n  [!] {len(skipped_perms)} skill(s) UNREADABLE over SSH (permissions):")
            for p in skipped_perms:
                print(f"      - {p}")
            print("      Fix: chown/chmod on remote. NOT silently dropped.")

        n = 0
        print(f"\n-- PUSH -> {host}: {len(push)} --")
        for p, s in push:
            n += 1
            print(f"{n:>3}. {p:<55} [{s}]")
        print()


if __name__ == "__main__":
    main()
