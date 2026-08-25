#!/usr/bin/env python3
"""Idempotent rules-based session taxonomy classifier.

Assigns ``disposition`` (project / archive / transient / junk) plus
``project_group`` / ``project`` to sessions that have none yet, so the Desktop
sidebar's Projects / Archives sections have data to render.

Rules (deliberately generic — no user-specific data, no absolute paths):
- Source-based noise: cron, tool, subagent, kanban, hermes_browser and
  speed-test* sources are operational noise -> transient (junk for speed-test).
- Content-based pass for cli/desktop/api_server/tui/webui/acp sources:
  probe/echo sessions -> transient; otherwise project with project_group /
  project inferred from a small keyword table.

Idempotent: only writes rows whose disposition is NULL, so manual
classifications are never clobbered. Writes through SessionDB so the whole
compression lineage is stamped as a unit (like archive/pin).

Usage:
    python3 scripts/backfill_session_dispositions.py [--dry-run] [--db PATH] [--home HERMES_HOME]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Source classes that are operational noise, never user project work.
NOISE_SOURCES = {
    "cron",
    "tool",
    "subagent",
    "kanban",
    "hermes_browser",
}

# Keyword table: (project_group, project) candidates matched against the
# session title. First match wins; keys are lowercased.
PROJECT_KEYWORDS: List[Tuple[Tuple[str, ...], str, str]] = [
    (("fusion", "router"), "Hermes", "Fusion Router"),
    (("sidebar", "taxonomy"), "Hermes", "Sidebar Taxonomy"),
    (("new tab", "newtab", "new-tab"), "Hermes", "New Tab"),
    (("agora", "share", "session share"), "Hermes", "Agora"),
    (("plugin",), "Hermes", "Plugin Dev"),
    (("skill",), "Hermes", "Skill Dev"),
    (("kanban",), "Hermes", "Kanban"),
]

# Titles that are clearly throwaway probes/echoes.
PROBE_TITLES = {"probe", "echo", "test", "ping", "smoke test", "hello"}


def classify(session_meta: Dict[str, object]) -> Optional[Dict[str, str]]:
    """Return ``{disposition, project_group, project}`` or None (leave alone).

    Pure function — importable and unit-testable, no side effects on import.
    Only sessions with no existing disposition are passed in by the caller.
    """
    source = str(session_meta.get("source") or "").lower()
    title = str(session_meta.get("title") or "").lower().strip()

    if source.startswith("speed-test"):
        return {"disposition": "junk", "project_group": None, "project": None}
    if source in NOISE_SOURCES:
        return {"disposition": "transient", "project_group": None, "project": None}

    if source in {"cli", "desktop", "api_server", "tui", "webui", "acp"}:
        if title in PROBE_TITLES or title.startswith("probe "):
            return {"disposition": "transient", "project_group": None, "project": None}
        for keywords, group, project in PROJECT_KEYWORDS:
            if all(k in title for k in keywords):
                return {
                    "disposition": "project",
                    "project_group": group,
                    "project": project,
                }
        return {"disposition": "project", "project_group": None, "project": None}

    return None


def _open_db(home: Optional[str], db_path: Optional[str]):
    from hermes_state import SessionDB

    if db_path:
        return SessionDB(db_path=Path(db_path).expanduser())
    home_path = Path(home).expanduser() if home else Path.home() / ".hermes"
    return SessionDB(db_path=home_path / "state.db")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print changes without writing")
    parser.add_argument("--db", help="explicit path to state.db")
    parser.add_argument("--home", help="HERMES_HOME (defaults to ~/.hermes)")
    args = parser.parse_args(argv)

    db = _open_db(args.home, args.db)

    # Only rows with no disposition yet — never clobber manual classification.
    unclassified = db.list_sessions_rich(
        limit=100000,
        include_archived=True,
        exclude_dispositions=[],
    )
    candidates = [s for s in unclassified if not s.get("disposition")]

    changed = 0
    for session in candidates:
        result = classify(session)
        if not result:
            continue
        if not args.dry_run:
            db.set_session_disposition(
                session["id"],
                result["disposition"],
                result.get("project_group"),
                result.get("project"),
            )
        changed += 1
        print(
            f"{'[dry-run] ' if args.dry_run else ''}{session['id'][:12]} "
            f"source={session.get('source')} disposition={result['disposition']} "
            f"group={result.get('project_group') or '-'} project={result.get('project') or '-'}"
        )
    print(f"{'Would classify' if args.dry_run else 'Classified'} {changed} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
