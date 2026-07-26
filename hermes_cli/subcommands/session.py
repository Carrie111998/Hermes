"""CLI inspection and confirmed manual rotation for the shared session ledger."""

from __future__ import annotations

import argparse
import json

from hermes_cli.session import api, schema


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _cmd_list(args: argparse.Namespace) -> int:
    schema.ensure_migrated()
    conn = schema.connect()
    try:
        params: list[object] = []
        where = ""
        if args.task:
            where = "WHERE task_id = ?"
            params.append(str(args.task))
        params.append(int(args.limit))
        rows = conn.execute(
            f"""
            SELECT id, task_id, parent_session_id, lane, profile, route,
                   opened_ts, closed_ts, rotation_reason
              FROM sessions
              {where}
             ORDER BY task_id ASC, opened_ts ASC, rowid ASC
             LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No rotation sessions.")
        return 0
    print("TASK  SESSION  PARENT  LANE  PROFILE  ROUTE  OPENED  CLOSED  REASON")
    for row in rows:
        print(
            f"{row['task_id']}  {row['id']}  "
            f"{row['parent_session_id'] or '-'}  {row['lane']}  "
            f"{row['profile'] or '-'}  {row['route'] or '-'}  "
            f"{row['opened_ts']}  {row['closed_ts'] or '-'}  "
            f"{row['rotation_reason'] or '-'}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    schema.ensure_migrated()
    conn = schema.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (str(args.session_id),),
        ).fetchone()
        if row is None:
            print(f"Session not found: {args.session_id}")
            return 1
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, indent=2))
        for table in (
            "cost_ledger",
            "leaf_verdicts",
            "dispatch_envelopes",
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            count = 0
            if exists is not None:
                columns = {
                    str(item["name"])
                    for item in conn.execute(f"PRAGMA table_info({table})")
                }
                if "session_id" in columns:
                    count = int(
                        conn.execute(
                            f"SELECT COUNT(*) count FROM {table} "
                            "WHERE session_id = ?",
                            (str(args.session_id),),
                        ).fetchone()["count"]
                    )
            print(f"{table}: {count}")
    finally:
        conn.close()
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    current = api.get_open_session_for_task(args.task)
    if current is None:
        print(f"No open session for task: {args.task}")
        return 1
    if not args.confirm:
        print(
            "Dry run: would rotate "
            f"session {current['id']} for task {args.task} "
            f"with reason {args.reason}. Re-run with --confirm."
        )
        return 0

    from hermes_cli.session.controller import rotate_now

    new_id, _prefix = rotate_now(
        current_session_id=str(current["id"]),
        task_id=str(current["task_id"]),
        lane=str(current["lane"]),
        profile=current["profile"],
        route=current["route"],
        reason=str(args.reason),
        token_count_at_close=int(current["token_count_at_close"] or 0),
    )
    print(f"Rotated {current['id']} -> {new_id}")
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "session",
        help="Inspect or manually rotate shared continuity sessions",
    )
    children = parser.add_subparsers(dest="session_command", required=True)

    list_parser = children.add_parser("list", help="List rotation sessions")
    list_parser.add_argument("--task")
    list_parser.add_argument("--limit", type=_positive_int, default=100)
    list_parser.set_defaults(func=_cmd_list)

    show = children.add_parser("show", help="Show one session and attribution")
    show.add_argument("session_id")
    show.set_defaults(func=_cmd_show)

    rotate = children.add_parser(
        "rotate",
        help="Manually rotate the open session for a task",
    )
    rotate.add_argument("--task", required=True)
    rotate.add_argument(
        "--reason",
        choices=("manual", "soft_limit", "hard_limit", "error"),
        default="manual",
    )
    rotate.add_argument("--confirm", action="store_true")
    rotate.set_defaults(func=_cmd_rotate)


__all__ = ["register_cli"]
