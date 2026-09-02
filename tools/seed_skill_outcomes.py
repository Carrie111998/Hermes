#!/usr/bin/env python3
"""Seed skill-outcome telemetry from existing usage history (2026-08).

The evolution dashboard (GET /api/skills/evolution) reads outcome telemetry
(success/failure records + utility scores) from the sidecar .usage.json.
Before the skill_report_outcome tool existed, skills accumulated use_count /
patch_count but NO outcome records — so the dashboard showed empty data.

This one-shot seed backfills one 'unknown' outcome per skill that has a
non-zero use_count, so the dashboard immediately reflects the tracked skill
population. Unknown outcomes do NOT affect utility scores (they are excluded
from the EMA), so this is a safe, non-distorting seed.

Usage:
    HERMES_HOME=~/.hermes python3 tools/seed_skill_outcomes.py
    # --dry-run to preview without writing
    HERMES_HOME=~/.hermes python3 tools/seed_skill_outcomes.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    ap.add_argument("--min-use", type=int, default=1, help="min use_count to seed (default 1)")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from tools.skill_usage import (
        _backfill_outcome_keys,
        get_outcome_summary,
        load_usage,
        record_outcome,
        save_usage,
    )

    data = load_usage()
    candidates = []
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        rec = _backfill_outcome_keys(raw)
        # Only seed skills that have been used but have no outcome history yet
        if rec.get("use_count", 0) >= args.min_use and not rec.get("outcomes"):
            candidates.append((str(name), rec.get("use_count", 0)))

    candidates.sort(key=lambda t: -t[1])
    print(f"待种子技能数: {len(candidates)}")

    if args.dry_run:
        for name, use in candidates[:30]:
            print(f"  [dry-run] {name} (use={use})")
        return 0

    for name, use in candidates:
        # One 'unknown' outcome per used skill — marks it tracked without
        # distorting utility (unknown excluded from EMA).
        record_outcome(name, "unknown")

    # Verify
    verified = 0
    for name, _ in candidates:
        s = get_outcome_summary(name)
        if s.get("unknown_count", 0) >= 1:
            verified += 1
    print(f"✅ 已种子 {verified}/{len(candidates)} 个技能")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
