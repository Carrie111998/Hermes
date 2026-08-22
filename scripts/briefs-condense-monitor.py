#!/usr/bin/env python3
"""Cheap daily check for the hermes-briefs-condense monitor-script hybrid.
No LLM reasoning here — just counts and date math. Output must be stable
(deterministic given current state, no timestamps) so the monitor's
byte-hash change detection only fires the agent when something is
actually newly due.
"""
import re
from datetime import date, timedelta
from pathlib import Path

BRIEFS = Path("/home/jiddy/Obsidian Core/Hermes/Briefs")
MONTHS_DIR = BRIEFS / "Archived Briefs" / "Months"
YEARS_DIR = BRIEFS / "Archived Briefs" / "Years"

DAILY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def months_due_for_condensation() -> list[str]:
    """A (year, month) is due once its LAST day is >30 days in the past
    (the whole month has safely aged out of the 30-day top-level window)
    AND it doesn't already have a Months/ summary."""
    cutoff = date.today() - timedelta(days=30)
    by_month: dict[tuple[int, int], list[str]] = {}
    if BRIEFS.exists():
        for p in BRIEFS.iterdir():
            if not p.is_file():
                continue
            m = DAILY_RE.match(p.name)
            if not m:
                continue
            y, mo = int(m.group(1)), int(m.group(2))
            by_month.setdefault((y, mo), []).append(p.name)

    already_condensed = set()
    if MONTHS_DIR.exists():
        for p in MONTHS_DIR.glob("*-summary.md"):
            mm = re.match(r"(\d{4})-(\d{2})-summary\.md", p.name)
            if mm:
                already_condensed.add((int(mm.group(1)), int(mm.group(2))))

    due = []
    for (y, mo), notes in by_month.items():
        if (y, mo) in already_condensed:
            continue
        if _last_day_of_month(y, mo) <= cutoff:
            due.append(f"{y:04d}-{mo:02d} ({len(notes)} notes)")
    return sorted(due)


def years_with_12_monthlies_uncondensed() -> list[int]:
    counts: dict[int, int] = {}
    if MONTHS_DIR.exists():
        for p in MONTHS_DIR.glob("*-summary.md"):
            m = re.match(r"(\d{4})-\d{2}-summary\.md", p.name)
            if m:
                counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    existing_years = set()
    if YEARS_DIR.exists():
        for p in YEARS_DIR.glob("*-summary.md"):
            m = re.match(r"(\d{4})-summary\.md", p.name)
            if m:
                existing_years.add(int(m.group(1)))
    return sorted(y for y, c in counts.items() if c >= 12 and y not in existing_years)


def main() -> None:
    due_months = months_due_for_condensation()
    due_years = years_with_12_monthlies_uncondensed()
    print(f"months_due_for_monthly_condense: {len(due_months)}")
    if due_months:
        print("  " + ", ".join(due_months))
    print(f"years_due_for_yearly_condense: {due_years}")


if __name__ == "__main__":
    main()
