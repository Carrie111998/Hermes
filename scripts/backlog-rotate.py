#!/usr/bin/env python3
"""Mechanical monthly + 12-month archival sweep for the Backlog.

No LLM reasoning anywhere in this script - pure text processing. Run
monthly, no-agent mode. Never deletes an open item - only moves lines
that are already struck through (closed) out of the live file and into
a dated archive, so the active Backlog.md always shows just what's open.
Once 12 monthly archive files have accumulated, they are further merged
into one permanent Years/ file, each month kept in its own labeled
section - concatenation, never summarization, nothing ever shrunk.

Note: monthly archive files are only written for months something
actually closed (see main()), so a batch of 12 is not guaranteed to be a
clean calendar year if some month had zero closures - hence the merged
file is named by its actual first-to-last month range, not by a bare
year, mirroring how execution-log-rotate.py names its own Months/ files
by actual date range rather than assuming calendar alignment.

Layout:
  Execution Logs/Backlogs/Backlog.md              - primary, active file
  Execution Logs/Backlogs/Archived/                - one file per month a
                                                      sweep actually moved
                                                      something, transient
                                                      staging for the
                                                      12-month merge
  Execution Logs/Backlogs/Archived/Years/          - every 12 monthly
                                                      files, merged with
                                                      each month in its
                                                      own labeled section,
                                                      permanent
  Execution Logs/Backlogs/rotation-log.md          - breadcrumb of every event
"""
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(os.environ.get(
    "BACKLOG_BASE",
    "/home/jiddy/Obsidian Core/Hermes/Execution Logs/Backlogs",
))
PRIMARY = BASE / "Backlog.md"
ARCHIVE_DIR = BASE / "Archived"
YEARS_DIR = ARCHIVE_DIR / "Years"
EVENT_LOG = BASE / "rotation-log.md"

CLOSED_MARKER = "## Closed"
EMPTY_PLACEHOLDER = "(none yet)"
MERGE_THRESHOLD = 12

MONTHLY_NAME_RE = re.compile(r"^Backlog-(\d{4}-\d{2})\.md$")


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def append_event(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now} | {msg}\n")


def split_at_closed(text: str) -> tuple[str, str]:
    """Split the primary file into (everything_through_marker, closed_body).

    Refuses to guess if the marker is missing, rather than silently
    treating the whole file as either half.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.rstrip("\n") == CLOSED_MARKER:
            return "".join(lines[: i + 1]), "".join(lines[i + 1:])
    raise RuntimeError(
        f"'{CLOSED_MARKER}' marker not found in {PRIMARY} - refusing to "
        "guess the open/closed split point"
    )


def strip_month_wrapper(text: str) -> str:
    """Return just the closed-items body of a monthly archive file (drop
    its frontmatter and title heading)."""
    parts = text.split("---\n\n", 1)
    body = parts[-1] if len(parts) > 1 else text
    if body.startswith("# "):
        _, _, rest = body.partition("\n\n")
        body = rest
    return body


def try_merge_years() -> list[str]:
    """Whenever MERGE_THRESHOLD or more monthly archive files exist, merge
    the oldest batch into one Years/ file, each month in its own labeled
    section, then delete only those exact merged sources. Loops in case
    more than one batch has accumulated. Returns human-readable summary
    lines for anything it actually did."""
    summaries: list[str] = []
    while True:
        monthly_files = sorted(
            (p for p in ARCHIVE_DIR.glob("*.md") if MONTHLY_NAME_RE.match(p.name)),
            key=lambda p: p.name,
        )
        if len(monthly_files) < MERGE_THRESHOLD:
            return summaries

        batch = monthly_files[:MERGE_THRESHOLD]
        sections = []
        first_month = last_month = None
        for p in batch:
            month = MONTHLY_NAME_RE.match(p.name).group(1)
            first_month = first_month or month
            last_month = month
            body = strip_month_wrapper(p.read_text(encoding="utf-8"))
            sections.append(f"## {month}\n\n{body.strip()}\n")

        year_fname = f"Backlog-{first_month}-to-{last_month}.md"
        year_path = YEARS_DIR / year_fname
        content = (
            "---\n"
            "status: archived\n"
            f"period_start: {first_month}\n"
            f"period_end: {last_month}\n"
            f"entry_months: {len(batch)}\n"
            "purpose: Mechanically merged 12-month batch of closed Backlog "
            "items. Concatenation with labeled month sections, not "
            "synthesis. Nothing is ever deleted from history - only "
            "consolidated.\n"
            "---\n\n"
            f"# Backlog — Archived, {first_month} to {last_month}\n\n"
            + "\n---\n\n".join(sections)
        )

        # Any disk/permission failure here (mkdir or write) must not crash
        # uncaught and must not delete the monthly sources - caught and
        # logged instead, same fail-safe posture as the rest of this
        # system's no-agent jobs.
        try:
            YEARS_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(year_path, content)
            written = year_path.read_text(encoding="utf-8")
            ok = (
                written.strip()
                and f"# Backlog — Archived, {first_month} to {last_month}" in written
                and all(f"## {MONTHLY_NAME_RE.match(p.name).group(1)}" in written for p in batch)
            )
        except OSError as exc:
            msg = (
                f"YEARLY MERGE FAILED (write error) | attempted {year_fname} "
                f"from {[p.name for p in batch]}: {exc}. Source monthly "
                "files left in place, nothing deleted."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
            return summaries
        if not ok:
            msg = (
                f"YEARLY MERGE FAILED VERIFICATION | attempted {year_fname} "
                f"from {[p.name for p in batch]} - source monthly files "
                "left in place, nothing deleted."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
            return summaries

        for p in batch:
            p.unlink()

        msg = (
            f"Yearly merge: combined {len(batch)} monthly archive files "
            f"into {year_fname} ({first_month} to {last_month})."
        )
        append_event(
            f"YEARLY MERGE | combined {len(batch)} monthly files "
            f"({', '.join(p.name for p in batch)}) into {year_fname}. "
            "Source monthly files deleted after verified write."
        )
        summaries.append(msg)


def main() -> int:
    if not PRIMARY.exists():
        print(f"ERROR: primary backlog not found at {PRIMARY}", file=sys.stderr)
        return 1

    text = PRIMARY.read_text(encoding="utf-8")
    header_through_marker, closed_body = split_at_closed(text)

    summaries: list[str] = []
    stripped = closed_body.strip()
    if stripped and stripped != EMPTY_PLACEHOLDER:
        today = date.today()
        month_tag = today.strftime("%Y-%m")
        archive_path = ARCHIVE_DIR / f"Backlog-{month_tag}.md"

        new_section = (
            f"## Closed this sweep ({today.isoformat()})\n\n" + stripped + "\n"
        )

        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            if archive_path.exists():
                existing = archive_path.read_text(encoding="utf-8")
                content = existing.rstrip("\n") + "\n\n" + new_section
            else:
                content = (
                    "---\n"
                    "status: archived\n"
                    f"month: {month_tag}\n"
                    "purpose: Mechanically swept closed Backlog.md items. "
                    "Concatenation of struck-through lines already closed "
                    "in the Execution Log, not synthesis. Nothing here is "
                    "ever deleted.\n"
                    "---\n\n"
                    f"# Backlog — Closed items, {month_tag}\n\n"
                    + new_section
                )
            atomic_write(archive_path, content)
            written = archive_path.read_text(encoding="utf-8")
            ok = stripped in written
        except OSError as exc:
            msg = (
                f"BACKLOG ARCHIVE FAILED (write error) | attempted "
                f"{archive_path.name}: {exc}. Primary Backlog.md left "
                "untouched, nothing lost."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
            ok = None  # skip primary reset below

        if ok is False:
            msg = (
                f"BACKLOG ARCHIVE FAILED VERIFICATION | attempted "
                f"{archive_path.name} - primary Backlog.md left untouched, "
                "nothing lost."
            )
            append_event(msg)
            summaries.append(f"⚠ {msg}")
        elif ok:
            # Only reset the primary's closed section after the archive
            # write is verified on disk - never clear live data on a write
            # we can't confirm.
            new_primary = header_through_marker + "\n" + EMPTY_PLACEHOLDER + "\n"
            atomic_write(PRIMARY, new_primary)
            msg = (
                f"Backlog swept: closed items moved into {archive_path.name}, "
                "primary Closed section reset."
            )
            append_event(
                f"MONTHLY SWEEP | moved closed items into {archive_path.name}. "
                "Primary Backlog.md Closed section reset to empty."
            )
            summaries.append(msg)
    # else: nothing closed since the last sweep - silent no-op, matches the
    # no-agent convention used across this system's other archival jobs.

    summaries.extend(try_merge_years())

    if summaries:
        print("\n".join(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
