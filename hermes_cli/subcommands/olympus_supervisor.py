"""Parser for the observe-only ``hermes olympus-supervisor`` surface."""

from __future__ import annotations

from collections.abc import Callable


def _add_json_flag(parser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )


def build_olympus_supervisor_parser(
    subparsers,
    *,
    cmd_olympus_supervisor: Callable,
) -> None:
    parser = subparsers.add_parser(
        "olympus-supervisor",
        help="Observe and rank the canonical Olympus Kanban queue",
        description=(
            "Run the Phase A observe-only Olympus supervisor. It reads Hermes "
            "Kanban and writes local checkpoints/projections/drafts; it never "
            "claims tasks, launches providers, consumes approvals, or sends messages."
        ),
    )
    parser.add_argument(
        "--board",
        help="Kanban board slug (default: olympus_supervisor.board)",
    )
    actions = parser.add_subparsers(dest="olympus_supervisor_action")

    run = actions.add_parser("run", help="Run continuous observe-only cycles")
    run.add_argument(
        "--max-cycles",
        type=int,
        help="Stop after this many cycles (primarily for supervised validation)",
    )
    _add_json_flag(run)

    run_once = actions.add_parser(
        "run-once", help="Run exactly one bounded observe-only cycle"
    )
    _add_json_flag(run_once)

    inspect = actions.add_parser(
        "inspect", help="Inspect checkpoint, heartbeat, stop, and failure state"
    )
    _add_json_flag(inspect)

    queue = actions.add_parser(
        "queue", help="Read and rank the Kanban queue without writing state"
    )
    _add_json_flag(queue)

    explain = actions.add_parser(
        "explain-next",
        help="Explain the next recommendation or why no task is executable",
    )
    _add_json_flag(explain)

    checkpoint = actions.add_parser(
        "checkpoint", help="Print the durable restart checkpoint"
    )
    _add_json_flag(checkpoint)

    health = actions.add_parser(
        "health", help="Validate heartbeat, lease, checkpoint, and stop state"
    )
    _add_json_flag(health)

    stop = actions.add_parser("stop", help="Create the explicit global stop control")
    stop.add_argument(
        "--reason",
        default="operator emergency stop",
        help="Operator-facing stop reason",
    )
    _add_json_flag(stop)

    resume = actions.add_parser(
        "resume",
        help="Clear the stop control without starting a cycle",
    )
    _add_json_flag(resume)

    mission_control = actions.add_parser(
        "render-mission-control",
        help="Regenerate the read-only Mission Control projection",
    )
    _add_json_flag(mission_control)

    telegram = actions.add_parser(
        "telegram-preview",
        help="Preview draft-only Telegram messages and the durable test sink",
    )
    _add_json_flag(telegram)

    parser.set_defaults(func=cmd_olympus_supervisor)
