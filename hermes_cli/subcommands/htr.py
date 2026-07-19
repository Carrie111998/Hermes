"""``hermes htr`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_htr_parser(subparsers, *, cmd_htr: Callable) -> None:
    """Attach the ``htr`` subcommand to ``subparsers``."""
    htr_parser = subparsers.add_parser(
        "htr",
        help="Hermes Trusted Task Runtime (HTR) tools",
        description="Read-only HTR run observation and integrity reporting",
    )
    htr_subparsers = htr_parser.add_subparsers(dest="htr_command", required=True)

    observe_parser = htr_subparsers.add_parser(
        "observe",
        help="Build a read-only observation snapshot for one run",
    )
    observe_parser.add_argument("run_id", help="Run identifier to observe")
    observe_parser.add_argument(
        "--runs-root",
        default=None,
        help="Override HTR runs root directory (default: HERMES_HOME/runs)",
    )
    observe_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a concise human summary to stderr (stdout remains JSON only)",
    )
    observe_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warning-level integrity findings as non-zero exit",
    )

    htr_parser.set_defaults(func=cmd_htr)
