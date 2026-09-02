"""``hermes qualification`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.qualification_cmd import SCENARIOS


def build_qualification_parser(subparsers, *, cmd_qualification: Callable) -> None:
    """Attach the ``qualification`` subcommand to ``subparsers``."""
    qualification_parser = subparsers.add_parser(
        "qualification",
        help="Describe a public, observation-only qualification scenario",
        allow_abbrev=False,
        add_help=False,
    )
    qualification_parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        required=True,
        help="Qualification fixture scenario to describe",
    )
    qualification_parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="Emit the canonical JSON qualification report",
    )
    qualification_parser.set_defaults(func=cmd_qualification)
