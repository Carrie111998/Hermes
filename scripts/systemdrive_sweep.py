"""Existence sweep for the literal ``%SystemDrive%`` junk tree.

A process whose environment lacks ``SYSTEMDRIVE`` cannot expand the
``REG_EXPAND_SZ`` template ``%SystemDrive%\\ProgramData`` from
``HKLM\\...\\ProfileList``, so it uses the literal string as a RELATIVE path and
builds the Windows known-folder cache under its own CWD. When that CWD is a
checkout root, the tree lands in the repository.

Both writers found in the 2026-08-16/17 hunt are fixed on main
(``run_secret_cli`` as ``ba920d1b5e``/``c3b8083116``; ``run_tests.sh``'s
``env -i`` allowlist as ``3bc7442b9b``). This sweep exists for the writer that
has not appeared yet.

Usage:
    python scripts/systemdrive_sweep.py [CHECKOUT_ROOT]

Exit 0 = clean, exit 1 = at least one tree found.

WHY THIS IS A STAT LOOP AND NOT AN INSTRUMENT
---------------------------------------------
It replaces ``scripts/systemdrive_watcher.py``, retired 2026-08-17: 1069 lines,
a ``ReadDirectoryChangesW`` backend at 0.7 ms median detection, and a 100 ms
process-creation ring buffer for attributing a writer that had already exited.

That design assumed two things were perishable. Neither is:

* **Ancestry** -- Security-4688 process-creation auditing is enabled on this box
  WITH command-line capture and ~2 days retention. Every sighting in the hunt was
  attributable straight from that log; the watcher's own measured rate was 5/8.
* **The artifact** -- these trees persist on disk. A self-deleting one would be a
  novel finding.

With neither perishable, sub-millisecond detection buys nothing over a periodic
check. And the one real failure in the record -- the 2026-08-14 sighting, still
unattributed because it was noticed after 4688 had rolled over -- was a failure
to LOOK, which a manually-armed instrument structurally cannot fix.

So the job is not speed. It is (a) run unattended, (b) sweep every root, and
(c) say plainly whether the sighting is still inside the attribution window.

``git status`` cannot do (a)-(c): ``.git/info/exclude`` carries
``%SystemDrive%/``, so the tree is invisible to git by design. Check the
filesystem, never git.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence

JUNK_NAME = "%SystemDrive%"

# Security-4688 retention measured on this box 2026-08-17 (log reached back to
# 2026-08-15 12:07:57). Past this a sighting cannot be attributed at all, which
# is a materially different report -- see format_report().
RETENTION_HOURS = 48.0

# ±5s around the tree's mtime. The writer creates the tree within a second or
# two of process start, and a NARROW filter is the documented trap: a filtered
# pass over the 18:29:34 window returned "no matches" while the unfiltered dump
# exposed the chain immediately. Widen, then read the whole dump.
_QUERY_PAD_SECS = 5


@dataclass(frozen=True)
class Sighting:
    """One junk tree, and the root it was found in."""

    root: Path
    path: Path
    mtime: float


def checkout_roots(repo_root: Path) -> List[Path]:
    """The shared checkout plus every ``.claude/worktrees/*`` sibling.

    Worktrees are the point, not an extra: of the four sightings on record,
    three were inside a worktree and only one in the shared checkout. A sweep
    of ``repo_root`` alone would have missed almost everything.
    """
    roots = [repo_root]
    worktrees = repo_root / ".claude" / "worktrees"
    try:
        entries = sorted(worktrees.iterdir())
    except OSError:  # absent, or unreadable -- the repo root still gets swept
        return roots
    roots.extend(p for p in entries if p.is_dir())
    return roots


def find_junk(roots: Iterable[Path]) -> List[Sighting]:
    """One stat per root. Deliberately NOT a tree walk.

    The writer builds the tree in its CWD, and its CWD is always a checkout
    root -- so a nested hit is not this artifact. Recursing would turn ~30
    stats into a walk of the whole multi-worktree tree, and an always-on check
    that costs that much is one that gets turned off.
    """
    sightings: List[Sighting] = []
    for root in roots:
        junk = root / JUNK_NAME
        try:
            mtime = junk.stat().st_mtime
        except OSError:  # absent (the overwhelmingly common case), or racing
            continue
        sightings.append(Sighting(root=root, path=junk, mtime=mtime))
    return sightings


def is_attributable(mtime: float, now: float, retention_hours: float) -> bool:
    """Is this sighting still inside 4688's retention window?

    Exclusive at the boundary: a sighting exactly at the edge is rolling off
    while you read the report, so promising attribution there would be a
    promise the log cannot keep.
    """
    return (now - mtime) < retention_hours * 3600.0


def _query_for(sighting: Sighting) -> str:
    start = datetime.fromtimestamp(sighting.mtime - _QUERY_PAD_SECS)
    end = datetime.fromtimestamp(sighting.mtime + _QUERY_PAD_SECS)
    stamp = "%Y-%m-%d %H:%M:%S"
    return (
        "    Get-WinEvent -FilterHashtable @{LogName='Security';Id=4688;"
        f"StartTime=(Get-Date '{start.strftime(stamp)}');"
        f"EndTime=(Get-Date '{end.strftime(stamp)}')}}\n"
        "    # then read NewProcessName / ParentProcessName / CommandLine out\n"
        "    # of $_.ToXml(). ~60-120s -- run it in the background, and do NOT\n"
        "    # pre-filter narrowly."
    )


def format_report(
    sightings: Sequence[Sighting],
    now: float,
    roots_swept: int | None = None,
) -> str:
    """Human report. A clean negative shows its work; a stale hit says so."""
    if not sightings:
        swept = "?" if roots_swept is None else str(roots_swept)
        return f"[systemdrive-sweep] clean: no {JUNK_NAME} tree in {swept} checkout root(s)."

    lines = [f"[systemdrive-sweep] FOUND {len(sightings)} {JUNK_NAME} tree(s):"]
    for s in sightings:
        seen = datetime.fromtimestamp(s.mtime).isoformat(timespec="seconds")
        lines.append(f"  {s.path}")
        lines.append(f"    created {seen} ({(now - s.mtime) / 3600.0:.1f}h ago)")
        if is_attributable(s.mtime, now, RETENTION_HOURS):
            lines.append("    ATTRIBUTABLE -- 4688 still holds this window:")
            lines.append(_query_for(s))
        else:
            # No query offered on purpose: past retention it returns nothing,
            # and a ~90s query that proves nothing reads like a dead end rather
            # than like the log having rolled over.
            lines.append(
                f"    UNATTRIBUTABLE -- older than 4688's ~{RETENTION_HOURS:.0f}h "
                "retention. The writer cannot be identified from the log; the "
                "tree itself is the only remaining evidence, so preserve it."
            )
    return "\n".join(lines)


def log_path() -> Path:
    """Where sightings are appended, matching the sibling guards on this box.

    Keyed off home rather than the checkout for two reasons: the sweep is
    invoked with a checkout root as an ARGUMENT (so it has no stable notion of
    "its own" repo), and writing inside a watched root would mean the sweep
    littering a directory it reports on.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = Path(".")
    return home / ".hermes" / "logs" / "systemdrive-sweep.jsonl"


def append_log(path: Path, sightings: Sequence[Sighting], now: float) -> None:
    """Append one JSONL record per sighting. Silent when there is nothing to say.

    A line per clean run would bury the one line that matters under a year of
    "nothing here" -- which is how a durable record stops being read. Under a
    scheduled task stdout goes nowhere, so this file is the only delivery path
    a sighting has.
    """
    if not sightings:
        return
    records = [
        {
            "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "root": str(s.root),
            "path": str(s.path),
            "mtime": datetime.fromtimestamp(s.mtime).isoformat(timespec="seconds"),
            "age_hours": round((now - s.mtime) / 3600.0, 2),
            "attributable": is_attributable(s.mtime, now, RETENTION_HOURS),
        }
        for s in sightings
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        # A diagnostic must never take down the thing it is diagnosing, but a
        # SILENT failure here would be worse than no log at all -- it would
        # look like a clean sweep to anyone reading the file later.
        print(f"[systemdrive-sweep] could not write log {path}: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="systemdrive_sweep.py",
        description=f"Report any literal {JUNK_NAME} tree in a checkout root or its worktrees.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="checkout root to sweep (default: this script's repository)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help=f"JSONL file to append sightings to (default: {log_path()})",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="report to stdout only; do not append a durable record",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent

    roots = checkout_roots(repo_root)
    sightings = find_junk(roots)
    now = time.time()
    print(format_report(sightings, now=now, roots_swept=len(roots)))
    if not args.no_log:
        append_log(Path(args.log) if args.log else log_path(), sightings, now)
    return 1 if sightings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
