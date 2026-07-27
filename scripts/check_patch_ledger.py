"""Fail when PATCH_LEDGER.md has drifted from the actual local commits.

A patch ledger nobody verifies is worse than none: it reads authoritative while
silently omitting the local patch an upstream merge is about to eat. This is
the cheapest possible guard — every commit in ``origin/main..HEAD`` must appear
in the ledger by abbreviated SHA.

Usage:
    python scripts/check_patch_ledger.py            # verify
    python scripts/check_patch_ledger.py --list     # show missing entries only

Exit 0 when the ledger covers every local commit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "PATCH_LEDGER.md"
UPSTREAM = "origin/main"


def local_commits() -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "log", "--format=%h|%s", f"{UPSTREAM}..HEAD"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        raise SystemExit(
            f"cannot list commits against {UPSTREAM}: {out.stderr.strip()[:200]}\n"
            "Fetch upstream first, or adjust UPSTREAM."
        )
    rows = []
    for line in out.stdout.splitlines():
        if "|" in line:
            sha, subject = line.split("|", 1)
            rows.append((sha.strip(), subject.strip()))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if not LEDGER.exists():
        print(f"MISSING: {LEDGER.relative_to(REPO)}")
        return 1

    text = LEDGER.read_text(encoding="utf-8", errors="replace")
    # Abbreviated SHAs anywhere in the document, in any table or prose.
    recorded = set(re.findall(r"\b[0-9a-f]{7,40}\b", text))

    commits = local_commits()
    missing = [(sha, subj) for sha, subj in commits
               if not any(sha.startswith(r) or r.startswith(sha) for r in recorded)]

    if args.list:
        for sha, subj in missing:
            print(f"{sha} {subj}")
        return 0 if not missing else 1

    print(f"local commits vs {UPSTREAM}: {len(commits)}")
    print(f"recorded in ledger        : {len(commits) - len(missing)}")
    if missing:
        print(f"\nMISSING FROM LEDGER ({len(missing)}):")
        for sha, subj in missing:
            print(f"  {sha}  {subj[:88]}")
        print("\nAdd them to PATCH_LEDGER.md with purpose, upstream files, the "
              "test that protects them, and their removal condition.")
        return 1
    print("\nledger covers every local commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
