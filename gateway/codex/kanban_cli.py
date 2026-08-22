"""Command-line entry point for read-only projection reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_constants import get_hermes_home

from gateway.codex.kanban_reconciliation import (
    CodexKanbanReconciler,
    read_projection_status,
)


def reconciliation_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Codex Bridge to Kanban reconciliation"
    )
    parser.add_argument(
        "--bridge-db",
        type=Path,
        default=get_hermes_home() / "codex_bridge" / "state.db",
    )
    parser.add_argument("--kanban-db", type=Path)
    parser.add_argument("--board", default="default")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--status",
        action="store_true",
        help="print read-only projection health instead of reconciliation",
    )
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        report = read_projection_status(
            args.bridge_db,
            board=args.board,
            kanban_db_path=args.kanban_db,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("Codex/Kanban projection status (read-only; 0 mutations)")
            print(f"Pending: {report['pending_count']}")
            print(f"Cursor: {report['projection_cursor']}")
            print(f"Retry: {report['retry_state']} ({report['retry_count']})")
            print(f"Last error: {report['last_error'] or '-'}")
            print(f"Dependency ready: {report['dependency']['ready']}")
        return 0 if report["dependency"]["ready"] else 2
    report = CodexKanbanReconciler(
        args.bridge_db,
        board=args.board,
        kanban_db_path=args.kanban_db,
    ).inspect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Codex/Kanban reconciliation (dry-run; 0 mutations)")
        print(f"Bridge jobs: {report['bridge_jobs']}")
        print(f"Kanban cards: {report['kanban_cards']}")
        for name, count in report["counts"].items():
            print(f"{name}: {count}")
    findings = sum(
        count
        for name, count in report["counts"].items()
        if name not in {"exact_match", "mapped_match"}
    )
    return 1 if args.fail_on_findings and findings else 0
