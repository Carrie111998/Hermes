"""Operator CLI for durable per-task kill fences."""

from __future__ import annotations

import argparse

from hermes_cli.cost.kill_switch import (
    kill_task,
    list_killed_tasks,
    unkill_task,
)


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _cmd_task(args: argparse.Namespace) -> int:
    if not args.confirm:
        print(
            f"DRY RUN: would kill task {args.task_id} "
            "(re-run with --confirm to write the fence)"
        )
        return 0
    kill_task(
        task_id=args.task_id,
        killed_by="operator",
        reason="operator",
        notes=args.reason,
    )
    print(f"Killed task {args.task_id}; current and future cost writes are fenced.")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    rows = list_killed_tasks(
        lane=args.lane,
        profile=args.profile,
        limit=args.limit,
    )
    if not rows:
        print("No killed tasks.")
        return 0
    print("task_id  killed_ts  killed_by  reason  lane  profile")
    for row in rows:
        print(
            f"{row['task_id']}  {row['killed_ts']}  {row['killed_by']}  "
            f"{row['reason']}  {row.get('lane') or '-'}  "
            f"{row.get('profile') or '-'}"
        )
    return 0


def _cmd_unkill(args: argparse.Namespace) -> int:
    if not args.confirm:
        print(
            f"DRY RUN: would remove kill fence for task {args.task_id} "
            "(task status would remain unchanged)"
        )
        return 0
    unkill_task(task_id=args.task_id)
    print(
        f"Removed kill fence for task {args.task_id}; "
        "task status was not changed."
    )
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "kill",
        help="Fence task cost writes or inspect existing task fences.",
    )
    commands = parser.add_subparsers(dest="kill_command", required=True)

    task = commands.add_parser("task", help="Fence one task.")
    task.add_argument("task_id")
    task.add_argument(
        "--reason",
        help="Free-text operator note stored with the fence.",
    )
    task.add_argument("--confirm", action="store_true")
    task.set_defaults(func=_cmd_task)

    listing = commands.add_parser("list", help="List killed tasks.")
    listing.add_argument("--limit", type=_positive_int, default=50)
    listing.add_argument("--lane")
    listing.add_argument("--profile")
    listing.set_defaults(func=_cmd_list)

    unkill = commands.add_parser("unkill", help="Remove a task fence.")
    unkill.add_argument("task_id")
    unkill.add_argument("--confirm", action="store_true")
    unkill.set_defaults(func=_cmd_unkill)


__all__ = ["register_cli"]
