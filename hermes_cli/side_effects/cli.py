"""Command-line inspection and maintenance for the side-effect ledger."""

from __future__ import annotations

import argparse
import json

from hermes_cli.side_effects import api


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _nonnegative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _cmd_list(args: argparse.Namespace) -> int:
    rows = api.list_rows(
        task_id=args.task,
        action_type=args.action,
        status=args.status,
        lane=args.lane,
        limit=args.limit,
    )
    if not rows:
        print("No side-effect rows.")
        return 0
    print(
        f"{'ID':>6}  {'Timestamp':20}  {'Task':16}  {'Lane':16}  "
        f"{'Action':20}  {'Status':10}  {'Try':>3}  External ref"
    )
    for row in rows:
        external_ref = str(row["external_ref"] or "")
        if len(external_ref) > 36:
            external_ref = f"{external_ref[:33]}..."
        print(
            f"{row['id']:>6}  {row['ts']:20}  "
            f"{str(row['task_id'] or '-'):16.16}  {row['lane']:16.16}  "
            f"{row['action_type']:20.20}  {row['status']:10.10}  "
            f"{row['attempt_number']:>3}  {external_ref}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    row = api.get_row(args.id)
    if row is None:
        raise SystemExit(f"side-effect row {args.id} not found")
    print(json.dumps(row, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    result = api.gc(
        older_than_days=args.older_than,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"Would delete {result['would_delete']} terminal row(s).")
    else:
        print(f"Deleted {result['deleted']} terminal row(s).")
    return 0


def _cmd_mark_abandoned(args: argparse.Namespace) -> int:
    api.mark_abandoned(row_id=args.id, reason=args.reason)
    print(f"Marked side-effect row {args.id} abandoned.")
    return 0


def _cmd_mark_stale_scan(_args: argparse.Namespace) -> int:
    count = api.mark_stale_scan()
    print(f"Marked {count} side-effect row(s) stale.")
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "side-effects",
        help="Inspect and maintain the side-effect idempotency ledger.",
    )
    commands = parser.add_subparsers(
        dest="side_effects_command",
        required=True,
    )

    list_parser = commands.add_parser("list", help="List recent ledger rows.")
    list_parser.add_argument("--task")
    list_parser.add_argument("--action")
    list_parser.add_argument("--status")
    list_parser.add_argument("--lane")
    list_parser.add_argument("--limit", type=_positive_int, default=50)
    list_parser.set_defaults(func=_cmd_list)

    show = commands.add_parser("show", help="Show one full ledger row as JSON.")
    show.add_argument("id", type=_positive_int)
    show.set_defaults(func=_cmd_show)

    gc_parser = commands.add_parser(
        "gc",
        help="Delete old done/failed/abandoned rows.",
    )
    gc_parser.add_argument(
        "--older-than",
        type=_nonnegative_int,
        required=True,
        metavar="DAYS",
    )
    gc_parser.add_argument("--dry-run", action="store_true")
    gc_parser.set_defaults(func=_cmd_gc)

    abandoned = commands.add_parser(
        "mark-abandoned",
        help="Mark an unresolved row abandoned.",
    )
    abandoned.add_argument("id", type=_positive_int)
    abandoned.add_argument("--reason", required=True)
    abandoned.set_defaults(func=_cmd_mark_abandoned)

    stale = commands.add_parser(
        "mark-stale-scan",
        help="Apply action-specific stale windows to active rows.",
    )
    stale.set_defaults(func=_cmd_mark_stale_scan)


__all__ = ["register_cli"]
