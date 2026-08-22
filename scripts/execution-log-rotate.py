#!/usr/bin/env python3
"""Mechanical weekly + 4-week rotation for the Execution Log.

No LLM reasoning anywhere in this script - pure date math and file
concatenation. Run weekly, no-agent mode. Never shrinks or summarizes
content - only reorganizes it. Nothing is ever deleted except the exact
weekly source files that were just verified to be fully and correctly
folded into a Months/ file.

Layout:
  Execution Logs/Execution Log.md    - primary, active file
  Execution Logs/Weekly/             - one file per week, transient staging
  Execution Logs/Months/             - every 4 weekly files, concatenated
                                        with each week in its own labeled
                                        section, permanent
  Execution Logs/.last-rotation-date - window-boundary state
  Execution Logs/rotation-log.md     - breadcrumb of every event
"""
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(os.environ.get(
    "EXECUTION_LOG_BASE",
    "/home/jiddy/Obsidian Core/Hermes/Execution Logs",
))
PRIMARY = BASE / "Execution Log.md"
WEEKLY_DIR = BASE / "Weekly"
MONTHS_DIR = BASE / "Months"
STATE_FILE = BASE / ".last-rotation-date"
EVENT_LOG = BASE / "rotation-log.md"

ENTRIES_MARKER = "## Entries"
MERGE_THRESHOLD = 4

WEEKLY_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-to-(\d{4}-\d{2}-\d{2})\.md$")


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def append_event(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now} | {msg}\n")


def split_header_and_entries(text: str) -> tuple[str, str]:
    """Split the primary file into (header_including_marker, entries_body).

    The header is everything up to and including the '## Entries' line -
    preserved exactly, never rotated. Entries is everything after it.
    Refuses to guess if the marker is missing, rather than silently
    treating the whole file as either header or entries.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.rstrip("\n") == ENTRIES_MARKER:
            return "".join(lines[: i + 1]), "".join(lines[i + 1:])
    raise RuntimeError(
        f"'{ENTRIES_MARKER}' marker not found in {PRIMARY} - refusing to "
        "guess the header/entries split point"
    )


def read_last_rotation_date() -> date:
    if STATE_FILE.exists():
        return datetime.strptime(STATE_FILE.read_text().strip(), "%Y-%m-%d").date()
    # First-ever rotation: no prior boundary recorded. Start the clock now
    # rather than inventing an earlier start date.
    return date.today()


def write_weekly_file(entries_body: str, start: date, end: date) -> Path:
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_DIR / f"{start.isoformat()}-to-{end.isoformat()}.md"
    content = (
        "---\n"
        "status: staged\n"
        f"period_start: {start.isoformat()}\n"
        f"period_end: {end.isoformat()}\n"
        "purpose: Mechanically rotated week of Execution Log entries. "
        "Concatenation with a title, not synthesis. Staged for a 4-week "
        "merge into Months/, never edited by hand.\n"
        "---\n\n"
        f"# Execution Log — Week of {start.isoformat()} to {end.isoformat()}\n\n"
        + entries_body.strip("\n") + "\n"
    )
    atomic_write(path, content)
    return path


def strip_weekly_wrapper(text: str) -> str:
    """Return just the entries body of a weekly file (drop its frontmatter
    and title heading)."""
    parts = text.split("---\n\n", 1)
    body = parts[-1] if len(parts) > 1 else text
    if body.startswith("# "):
        _, _, rest = body.partition("\n\n")
        body = rest
    return body


def try_merge_months() -> None:
    """Whenever MERGE_THRESHOLD or more weekly files exist, merge the
    oldest batch into one Months/ file, each week in its own labeled
    section, then delete only those exact merged sources. Loops in case
    more than one batch has accumulated (e.g. recovering from missed
    runs). Returns a list of human-readable summary lines for anything it
    actually did, so the caller can report it (vs. staying silent)."""
    summaries: list[str] = []
    while True:
        weekly_files = sorted(
            (p for p in WEEKLY_DIR.glob("*.md") if WEEKLY_NAME_RE.match(p.name)),
            key=lambda p: p.name,
        )
        if len(weekly_files) < MERGE_THRESHOLD:
            return summaries

        batch = weekly_files[:MERGE_THRESHOLD]
        sections = []
        first_start = last_end = None
        for p in batch:
            m = WEEKLY_NAME_RE.match(p.name)
            wk_start, wk_end = m.group(1), m.group(2)
            first_start = first_start or wk_start
            last_end = wk_end
            body = strip_weekly_wrapper(p.read_text(encoding="utf-8"))
            sections.append(f"## Week of {wk_start} to {wk_end}\n\n{body.strip()}\n")

        month_fname = f"{first_start}-to-{last_end}.md"
        month_path = MONTHS_DIR / month_fname
        content = (
            "---\n"
            "status: archived\n"
            f"period_start: {first_start}\n"
            f"period_end: {last_end}\n"
            f"entry_weeks: {len(batch)}\n"
            "purpose: Mechanically merged 4-week batch of the Execution "
            "Log. Concatenation with labeled week sections, not "
            "synthesis. Nothing is ever deleted from history - only "
            "consolidated.\n"
            "---\n\n"
            f"# Execution Log — {first_start} to {last_end}\n\n"
            + "\n---\n\n".join(sections)
        )

        # Any disk/permission failure here (mkdir or write) must not crash
        # uncaught and must not delete the weekly sources - caught and
        # logged instead, same fail-safe posture as the rest of this
        # system's no-agent jobs.
        try:
            MONTHS_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(month_path, content)
            written = month_path.read_text(encoding="utf-8")
            ok = (
                written.strip()
                and f"# Execution Log — {first_start} to {last_end}" in written
                and all(f"## Week of {WEEKLY_NAME_RE.match(p.name).group(1)}" in written for p in batch)
            )
        except OSError as exc:
            msg = (
                f"MONTHLY MERGE FAILED (write error) | attempted {month_fname} "
                f"from {[p.name for p in batch]}: {exc}. Source weekly files "
                "left in place, nothing deleted."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
            return summaries
        if not ok:
            msg = (
                f"MONTHLY MERGE FAILED VERIFICATION | attempted {month_fname} "
                f"from {[p.name for p in batch]} - source weekly files left "
                "in place, nothing deleted."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
            return summaries

        for p in batch:
            p.unlink()

        msg = (
            f"Monthly merge: combined {len(batch)} weekly files into "
            f"{month_fname} ({first_start} to {last_end})."
        )
        append_event(
            f"MONTHLY MERGE | combined {len(batch)} weekly files "
            f"({', '.join(p.name for p in batch)}) into {month_fname}. "
            "Source weekly files deleted after verified write."
        )
        summaries.append(msg)


def main() -> int:
    if not PRIMARY.exists():
        print(f"ERROR: primary log not found at {PRIMARY}", file=sys.stderr)
        return 1

    header, entries_body = split_header_and_entries(PRIMARY.read_text(encoding="utf-8"))
    last_boundary = read_last_rotation_date()
    today = date.today()

    summaries: list[str] = []
    if entries_body.strip():
        weekly_path = write_weekly_file(entries_body, last_boundary, today)
        atomic_write(PRIMARY, header)
        append_event(
            f"WEEKLY ROTATION | moved entries ({last_boundary.isoformat()} "
            f"to {today.isoformat()}) into {weekly_path.name}, primary "
            "reset to header only."
        )
        summaries.append(
            f"Execution Log rotated: week of {last_boundary.isoformat()} to "
            f"{today.isoformat()} moved to {weekly_path.name}."
        )
        summaries.extend(try_merge_months())
    # else: nothing logged this week - silent no-op (empty stdout), matches
    # the no-agent convention used across this system's other archival jobs.

    atomic_write(STATE_FILE, today.isoformat())

    if summaries:
        print("\n".join(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
