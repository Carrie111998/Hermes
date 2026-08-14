"""Parser for the opt-in Claude Remote Control recovery command."""

from __future__ import annotations

import argparse
from typing import Callable


def build_claude_recovery_parser(subparsers, *, cmd_claude_recovery: Callable) -> None:
    parser = subparsers.add_parser(
        "claude-recovery",
        help="Inspect or safely trigger an existing Claude Remote Control task",
        description="Inspect or safely trigger an existing configured Claude Remote Control task",
    )
    parser.add_argument("--json", action="store_true", help="Print redacted JSON")
    actions = parser.add_subparsers(dest="action")
    for action, help_text in (
        ("status", "Inspect task, auth, and session state without changing anything"),
        ("repair", "Trigger the existing task only when the safe preflight passes"),
    ):
        child = actions.add_parser(action, help=help_text)
        child.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="Print redacted JSON",
        )
    parser.set_defaults(func=cmd_claude_recovery, action="status", json=False)
