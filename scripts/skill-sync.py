#!/usr/bin/env python3
"""Nightly sync: repo's bundled skills -> live installed skills directory.

No-agent, mechanical, no judgment calls -- by the time a skill file is on
origin/main (and pulled into the live checkout by hermes-sync-fork), JID
has already reviewed and merged it via GitHub. This job's only job is to
get that already-approved content into ~/.hermes/skills/, which is a
genuinely separate directory the running agent actually reads from and
does NOT auto-update just because the git checkout changed (confirmed
2026-08-12 -- this exact gap caused a real privacy incident).

Rules:
- Only touches skills present in BOTH the repo and live (a skill that
  exists live with no repo counterpart is user/curator-created and none
  of this job's business; a brand new repo skill never installed live
  is a first-install decision, not a routine sync, so it's reported but
  not auto-installed).
- Skips anything listed in skill_sync_exclusions.txt entirely.
- Copies a file repo -> live whenever it's missing or different on live.
  Never the other direction, never deletes anything.
- Detects live files with no repo counterpart inside a synced skill
  (orphans) and REPORTS them -- does not delete. Silent divergence is
  exactly the failure class this job exists to close, so an orphan that
  would otherwise sit there forever unnoticed gets surfaced instead.
- Silent (exit 0, no output) only when nothing needed syncing and no
  orphans exist. Any real change, or any error, is never silent.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_SKILLS = Path("/home/jiddy/.hermes/hermes-agent/skills")
LIVE_SKILLS = Path("/home/jiddy/.hermes/skills")
EXCLUSION_FILE = Path("/home/jiddy/.hermes/cron/skill_sync_exclusions.txt")


def load_exclusions() -> set[str]:
    excl = set()
    if EXCLUSION_FILE.exists():
        for line in EXCLUSION_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            excl.add(line)
    return excl


def find_skill_dirs(root: Path) -> dict[str, Path]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if "SKILL.md" in filenames:
            rel = os.path.relpath(dirpath, root)
            out[rel] = Path(dirpath)
    return out


def all_files(base: Path) -> dict[str, Path]:
    files = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(base)
            files[str(rel)] = full
    return files


def main() -> int:
    exclusions = load_exclusions()

    if not REPO_SKILLS.is_dir():
        print(f"hermes-skill-sync FAILED: repo skills dir missing: {REPO_SKILLS}")
        return 1
    if not LIVE_SKILLS.is_dir():
        print(f"hermes-skill-sync FAILED: live skills dir missing: {LIVE_SKILLS}")
        return 1

    repo_skills = find_skill_dirs(REPO_SKILLS)
    live_skills = find_skill_dirs(LIVE_SKILLS)

    updated: list[str] = []
    orphaned: list[str] = []
    errors: list[str] = []
    never_installed: list[str] = []

    for name in sorted(repo_skills):
        if name not in live_skills:
            never_installed.append(name)
            continue
        if name in exclusions:
            continue

        repo_dir = repo_skills[name]
        live_dir = live_skills[name]
        repo_files = all_files(repo_dir)
        live_files = all_files(live_dir)

        for rel, repo_path in repo_files.items():
            live_path = live_dir / rel
            try:
                needs_copy = (not live_path.exists()) or (
                    repo_path.read_bytes() != live_path.read_bytes()
                )
            except OSError as e:
                errors.append(f"{name}/{rel}: read error: {e}")
                continue
            if needs_copy:
                try:
                    live_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(repo_path, live_path)
                    updated.append(f"{name}/{rel}")
                except OSError as e:
                    errors.append(f"{name}/{rel}: copy failed: {e}")

        for rel in live_files:
            if rel not in repo_files:
                orphaned.append(f"{name}/{rel}")

    if errors:
        print("hermes-skill-sync FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    if not updated and not orphaned and not never_installed:
        return 0  # nothing to do, nothing to say

    if updated:
        print(f"hermes-skill-sync: updated {len(updated)} file(s):")
        for u in updated:
            print(f"  - {u}")
    if orphaned:
        print(
            f"hermes-skill-sync: {len(orphaned)} orphaned live file(s) with no "
            f"repo counterpart (NOT deleted -- review and decide manually):"
        )
        for o in orphaned:
            print(f"  - {o}")
    if never_installed:
        print(
            f"hermes-skill-sync: {len(never_installed)} repo skill(s) never "
            f"installed live at all (not auto-installed -- first-install is a "
            f"decision, not a routine sync):"
        )
        for n in never_installed:
            print(f"  - {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
