"""``hermes readiness`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_readiness_parser(subparsers, *, cmd_readiness: Callable) -> None:
    """Attach the ``readiness`` subcommand to ``subparsers``."""

    readiness_parser = subparsers.add_parser(
        "readiness",
        help="Generate project go-live readiness reports",
        description="Analyse a project and generate readiness documentation",
    )

    readiness_parser.add_argument(
        "--repo",
        required=True,
        help="Path to the project repository",
    )

    readiness_parser.add_argument(
        "--output",
        required=True,
        help="Directory for generated reports",
    )

    readiness_parser.add_argument(
        "--format",
        action="append",
        choices=("markdown", "json"),
        default=[],
        help="Output format (repeatable)",
    )

    readiness_parser.set_defaults(func=cmd_readiness)
