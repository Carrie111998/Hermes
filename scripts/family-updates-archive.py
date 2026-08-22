#!/usr/bin/env python3
"""Mechanical FIFO + archive maintenance for family-updates.md.

No LLM reasoning anywhere in this script — pure entry counting, date
comparison, and file concatenation. Run daily, no-agent mode.

Layout:
  Reports/Family Updates/family-updates.md       - rolling 90-entry FIFO
  Reports/Family Updates/archive-in-progress.md   - truncated overflow
  Reports/Family Updates/Archive/Months/          - 180-entry / forced-annual merges
  Reports/Family Updates/Archive/Years/           - two-Months-notes merges
  Reports/Family Updates/.last-annual-reset-year  - idempotency guard
  Reports/Family Updates/archive-log.md           - breadcrumb of every event
"""
import re
import sys
from datetime import datetime, date
from pathlib import Path

BASE = Path("/home/jiddy/Obsidian Core/Hermes/Reports/Family Updates")
MAIN = BASE / "family-updates.md"
PROGRESS = BASE / "archive-in-progress.md"
MONTHS_DIR = BASE / "Archive" / "Months"
YEARS_DIR = BASE / "Archive" / "Years"
STATE_FILE = BASE / ".last-annual-reset-year"
EVENT_LOG = BASE / "archive-log.md"

CAP = 90
ENTRY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2} \| ")

MAIN_HEADER = """---
status: observational
purpose: Append-only log of every write to Family/<Name>/ (create or update, any person) AND every family session handoff (start/end). Consumed by the daily brief's Family and Handoffs sections. This is a log, not a source of truth — diagnostic only, same tier as other Reports/ content.
---

# Family Updates Log

One line per event, newest at the bottom. Two entry types, same file, same
FIFO/archive mechanism, distinguished by an explicit `type:` field right
after the timestamp:

`YYYY-MM-DD HH:MM | type:folder-activity | Person | Subfolder | create|update | one-line summary`

`YYYY-MM-DD HH:MM | type:handoff | Person | start|end | one-line summary (end lines include start time + duration)`

Entries from before this format existed have no `type:` field — left as-is,
never backfilled. See skill: family-session-handoff for the handoff-event
procedure.

Append atomically (temp file + rename), never truncate or rewrite prior lines.
Rolling FIFO, most recent {cap} entries only — older entries live in
archive-in-progress.md, then Archive/Months/, then Archive/Years/.

---
"""

PROGRESS_HEADER = """---
status: observational
purpose: Holding area for entries truncated out of family-updates.md once it hits its {cap}-entry FIFO cap. Once this file also reaches {cap} entries, it and family-updates.md's current {cap} are merged into one dated note in Archive/Months/, and both files reset to empty. Mechanical only — never edited by hand, never summarized.
---

# Archive In Progress

Same line format as family-updates.md (both `type:folder-activity` and
`type:handoff` entries, or untagged legacy lines). Entries here are appended
oldest-first as they're truncated out of the main log — do not reorder.

---
"""


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ENTRY_RE.match(ln)]


def entry_date(line: str) -> date:
    m = ENTRY_RE.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def write_log_file(path: Path, header_template: str, entries: list[str]) -> None:
    header = header_template.format(cap=CAP)
    body = "\n".join(entries)
    content = header + (body + "\n" if entries else "")
    atomic_write(path, content)


def append_event(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{now} | {msg}\n"
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def merge_note_title(entries: list[str]) -> tuple[str, date, date]:
    dates = sorted(entry_date(e) for e in entries)
    start, end = dates[0], dates[-1]
    return f"{start.isoformat()}-to-{end.isoformat()}", start, end


def write_months_note(entries: list[str], year_hint: int, partial: bool) -> Path:
    slug, start, end = merge_note_title(entries)
    MONTHS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"family-updates-{slug}.md"
    path = MONTHS_DIR / fname
    partial_note = "\n**PARTIAL PERIOD** — this note covers less than a full 180-entry cycle (forced year-boundary close).\n" if partial else ""
    content = (
        "---\n"
        "status: archived\n"
        f"period_start: {start.isoformat()}\n"
        f"period_end: {end.isoformat()}\n"
        f"year: {year_hint}\n"
        f"entry_count: {len(entries)}\n"
        "purpose: Mechanically merged half-year batch of family-updates.md entries. Concatenation with a title, not synthesis.\n"
        "---\n\n"
        f"# Family Updates — {slug}\n"
        f"{partial_note}\n"
        + "\n".join(entries) + "\n"
    )
    atomic_write(path, content)
    return path


def try_merge_years(year: int) -> None:
    candidates = []
    if MONTHS_DIR.exists():
        for p in sorted(MONTHS_DIR.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            m = re.search(r"^year: (\d+)$", text, re.MULTILINE)
            if m and int(m.group(1)) == year:
                candidates.append(p)
    if len(candidates) < 2:
        return
    # Take the earliest two not yet merged (Months/ files are never deleted,
    # so re-runs must not re-merge the same pair — guard via a marker file).
    marker = YEARS_DIR / f".merged-{year}-from"
    already = set()
    if marker.exists():
        already = set(marker.read_text(encoding="utf-8").splitlines())
    pending = [p for p in candidates if p.name not in already]
    if len(pending) < 2:
        return
    pair = pending[:2]
    all_entries = []
    for p in pair:
        text = p.read_text(encoding="utf-8")
        body = text.split("---\n\n", 2)[-1]
        all_entries.extend(ln for ln in body.splitlines() if ENTRY_RE.match(ln))
    all_entries.sort(key=entry_date)
    YEARS_DIR.mkdir(parents=True, exist_ok=True)
    year_path = YEARS_DIR / f"family-updates-{year}.md"
    content = (
        "---\n"
        "status: archived\n"
        f"year: {year}\n"
        f"entry_count: {len(all_entries)}\n"
        "purpose: Mechanically merged annual roll-up of two Archive/Months/ notes. Concatenation with a title, not synthesis. JID's to keep or delete on his own timeline — never auto-deleted.\n"
        "---\n\n"
        f"# Family Updates — {year}\n\n"
        + "\n".join(all_entries) + "\n"
    )
    atomic_write(year_path, content)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as f:
        for p in pair:
            f.write(p.name + "\n")
    append_event(
        f"YEARLY MERGE | {year} | combined {pair[0].name} + {pair[1].name} into "
        f"{year_path.name} ({len(all_entries)} entries). Source Months/ notes retained, not deleted."
    )


def main() -> int:
    today = date.today()

    # Idempotency guard for the forced annual reset.
    last_reset_year = int(STATE_FILE.read_text().strip()) if STATE_FILE.exists() else today.year

    main_entries = read_entries(MAIN)
    progress_entries = read_entries(PROGRESS)

    # 1. Forced annual reset takes priority: first run of a new calendar year.
    if today.year > last_reset_year:
        combined = main_entries + progress_entries
        if combined:
            combined.sort(key=entry_date)
            partial = len(combined) < (2 * CAP)
            note_path = write_months_note(combined, last_reset_year, partial=True)
            append_event(
                f"ANNUAL FORCED CLOSE | {last_reset_year} | merged {len(combined)} entries "
                f"({len(main_entries)} from main + {len(progress_entries)} from archive-in-progress) "
                f"into {note_path.name}. {'Marked partial.' if partial else ''}"
            )
        write_log_file(MAIN, MAIN_HEADER, [])
        write_log_file(PROGRESS, PROGRESS_HEADER, [])
        atomic_write(STATE_FILE, str(today.year))
        try_merge_years(last_reset_year)
        main_entries, progress_entries = [], []
        last_reset_year = today.year

    # 2. Normal FIFO trim: cap main at CAP, oldest overflow -> progress.
    if len(main_entries) > CAP:
        overflow = main_entries[: len(main_entries) - CAP]
        main_entries = main_entries[len(main_entries) - CAP :]
        progress_entries = progress_entries + overflow
        write_log_file(MAIN, MAIN_HEADER, main_entries)
        write_log_file(PROGRESS, PROGRESS_HEADER, progress_entries)
        append_event(
            f"TRUNCATE | moved {len(overflow)} entries from family-updates.md "
            f"to archive-in-progress.md (main now at {len(main_entries)}/{CAP}, "
            f"archive-in-progress at {len(progress_entries)}/{CAP})"
        )

    # 3. Half-year cycle complete: archive-in-progress hit the cap too.
    if len(progress_entries) >= CAP:
        combined = main_entries + progress_entries[:CAP]
        combined.sort(key=entry_date)
        note_path = write_months_note(combined, today.year, partial=False)
        remaining_progress = progress_entries[CAP:]
        write_log_file(MAIN, MAIN_HEADER, [])
        write_log_file(PROGRESS, PROGRESS_HEADER, remaining_progress)
        append_event(
            f"HALF-YEAR MERGE | combined {len(combined)} entries "
            f"(main + archive-in-progress) into {note_path.name}, reset both files to empty."
        )
        try_merge_years(today.year)

    return 0


if __name__ == "__main__":
    sys.exit(main())
