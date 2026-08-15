"""Small CLI owner for Kanban security inspection and approval."""

from __future__ import annotations

import argparse
import json

from .service import Actor, KanbanSecurityService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes kanban security")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("task_id")
    run.add_argument("run_id", type=int)
    events = sub.add_parser("events")
    events.add_argument("task_id")
    events.add_argument("--cursor")
    events.add_argument("--limit", type=int, default=100)
    queue = sub.add_parser("publications")
    queue.add_argument("--limit", type=int, default=100)
    decide = sub.add_parser("decide")
    decide.add_argument("intent_id")
    decide.add_argument("wire_sha256")
    decide.add_argument("decision", choices=("approve", "reject"))
    decide.add_argument("--reason")
    return parser


def main(argv=None, *, service: KanbanSecurityService, actor: Actor) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        value = service.run_summary(actor, task_id=args.task_id, run_id=args.run_id)
    elif args.command == "events":
        value = service.event_page(
            actor, task_id=args.task_id, cursor=args.cursor, limit=args.limit
        )
    elif args.command == "publications":
        value = service.publication_queue(actor, limit=args.limit)
    else:
        value = {
            "approval_id": service.approve(
                actor,
                intent_id=args.intent_id,
                wire_sha256=args.wire_sha256,
                decision=args.decision,
                reason=args.reason,
            )
        }
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
