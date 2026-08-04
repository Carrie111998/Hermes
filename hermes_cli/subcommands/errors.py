"""``hermes errors`` subcommand parser.

Structured view over ``logs/error-ledger.jsonl`` — the JSONL error ledger
written by the root-logger handler installed in ``hermes_logging``
(see ``agent/error_ledger.py``). Falls back to parsing ``errors.log``
when the ledger has no entries yet (pre-existing installs).

Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_errors_parser(subparsers, *, cmd_errors: Callable) -> None:
    """Attach the ``errors`` subcommand to ``subparsers``."""
    errors_parser = subparsers.add_parser(
        "errors",
        help="Show recent errors from the structured error ledger",
        description=(
            "Query the JSONL error ledger (~/.hermes/logs/error-ledger.jsonl): "
            "every ERROR-or-above record, structured and categorized "
            "(api / cron / general)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes errors                     Show the 20 newest errors
    hermes errors -n 50               Show the 50 newest
    hermes errors --since 1h          Errors from the last hour
    hermes errors --category api      Only API-call failures
    hermes errors --category cron     Only cron job failures
    hermes errors --stats             Aggregate counts by category/logger
    hermes errors --json              Raw JSONL (newest first)
""",
    )
    errors_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        help="Number of errors to show (default: 20)",
    )
    errors_parser.add_argument(
        "--since",
        metavar="TIME",
        help="Show errors since TIME ago (e.g. 1h, 30m, 2d)",
    )
    errors_parser.add_argument(
        "--category",
        choices=["api", "cron", "general"],
        help="Filter by error category",
    )
    errors_parser.add_argument(
        "--stats",
        action="store_true",
        help="Show aggregate counts instead of individual errors",
    )
    errors_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSONL, one record per line, newest first",
    )
    errors_parser.set_defaults(func=cmd_errors)
