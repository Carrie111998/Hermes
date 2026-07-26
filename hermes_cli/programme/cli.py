"""Command-line interface for programme control."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from hermes_cli.programme.gate import (
    check_drain,
    get_state,
    inflight_count,
    is_halt_signalled,
    set_state,
)


def print_status() -> None:
    state = check_drain()
    print(f"Programme state: {state.state}")
    print(f"In-flight tasks: {inflight_count()}")
    print(f"Last change: {state.changed_at}")
    print(f"Halt signal: {'present' if is_halt_signalled() else 'absent'}")
    if state.reason:
        print(f"Reason: {state.reason}")
    if state.changed_by:
        print(f"Changed by: {state.changed_by}")


def print_gate_status(limit: int = 20) -> None:
    """Print programme state and the newest universal-ingress refusals."""
    from hermes_cli.programme.ingress import list_recent_rejections

    state = get_state()
    print(f"programme state: {state.state}")
    print(f"reason:          {state.reason or 'n/a'}")
    print(f"last N={int(limit)} rejected ingress attempts:")
    rows = list_recent_rejections(limit)
    if not rows:
        print("  (none)")
        return
    for row in rows:
        print(
            f"  {row['ts']}  route={row['route']}  "
            f"profile={row['profile'] or '-'}  "
            f"session={row['session_id'] or '-'}  "
            f"reason={row['reason'] or row['state']}"
        )


def _apply(state: str, reason: str | None, changed_by: str) -> int:
    result = set_state(state, reason, changed_by)
    print(
        f"Programme state: {result.state} "
        f"(in-flight at change: {result.task_count_at_change})"
    )
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    return _apply("PAUSED", args.reason, args.changed_by)


def _cmd_resume(args: argparse.Namespace) -> int:
    return _apply("RUNNING", "resumed", args.changed_by)


def _cmd_drain(args: argparse.Namespace) -> int:
    return _apply("DRAINING", "operator drain", args.changed_by)


def _cmd_halt(args: argparse.Namespace) -> int:
    return _apply("HALTED", args.reason, args.changed_by)


def _cmd_status(_args: argparse.Namespace) -> int:
    print_status()
    return 0


def _cmd_gate_status(args: argparse.Namespace) -> int:
    print_gate_status(getattr(args, "limit", 20))
    return 0


def _add_gate_parser(subparsers: argparse._SubParsersAction) -> None:
    gate = subparsers.add_parser(
        "gate",
        help="Inspect universal conversation-ingress admission",
    )
    gate_subparsers = gate.add_subparsers(
        dest="gate_command",
        required=True,
    )
    status = gate_subparsers.add_parser(
        "status",
        help="Show programme state and recent ingress rejections",
    )
    status.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of recent rejections to print (default: 20)",
    )
    status.set_defaults(func=_cmd_gate_status)


def _add_by(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--by", dest="changed_by", required=True, help="Operator identity")


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    """Register non-conflicting top-level programme mutation commands."""
    pause = subparsers.add_parser("pause", help="Reject new tasks; let in-flight tasks finish")
    pause.add_argument("--reason", required=True, help="Why admission is paused")
    _add_by(pause)
    pause.set_defaults(func=_cmd_pause)

    resume = subparsers.add_parser("resume", help="Resume new task admission")
    _add_by(resume)
    resume.set_defaults(func=_cmd_resume)

    drain = subparsers.add_parser(
        "drain", help="Reject new tasks and pause after the last in-flight task"
    )
    _add_by(drain)
    drain.set_defaults(func=_cmd_drain)

    halt = subparsers.add_parser(
        "halt", help="Reject new tasks and signal leaves to stop at a safe checkpoint"
    )
    halt.add_argument("--reason", required=True, help="Why the programme is halted")
    _add_by(halt)
    halt.set_defaults(func=_cmd_halt)

    _add_gate_parser(subparsers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-programme")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--reason", required=True)
    _add_by(pause)
    pause.set_defaults(func=_cmd_pause)

    resume = subparsers.add_parser("resume")
    _add_by(resume)
    resume.set_defaults(func=_cmd_resume)

    drain = subparsers.add_parser("drain")
    _add_by(drain)
    drain.set_defaults(func=_cmd_drain)

    halt = subparsers.add_parser("halt")
    halt.add_argument("--reason", required=True)
    _add_by(halt)
    halt.set_defaults(func=_cmd_halt)

    status = subparsers.add_parser("status")
    status.set_defaults(func=_cmd_status)
    _add_gate_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_parser",
    "main",
    "print_gate_status",
    "print_status",
    "register_cli",
]
