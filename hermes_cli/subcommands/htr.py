"""``hermes htr`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_htr_parser(subparsers, *, cmd_htr: Callable) -> None:
    """Attach the ``htr`` subcommand to ``subparsers``."""
    htr_parser = subparsers.add_parser(
        "htr",
        help="Hermes Trusted Task Runtime (HTR) tools",
        description="Read-only HTR run observation and derived action planning",
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

    plan_parser = htr_subparsers.add_parser(
        "plan",
        help="Build a derived read-only action plan for one run",
    )
    plan_parser.add_argument("run_id", help="Run identifier to plan against")
    plan_parser.add_argument(
        "--action",
        default=None,
        help="Explicit Phase 1 lifecycle API name to plan (catalog allowlist only)",
    )
    plan_parser.add_argument(
        "--inputs-file",
        default=None,
        help="JSON file with record/actor/executor inputs for the selected action",
    )
    plan_parser.add_argument(
        "--project-checkpoint",
        default=None,
        help="Optional opaque project repository checkpoint string",
    )
    plan_parser.add_argument(
        "--remediation-intent",
        action="store_true",
        help="Explicit remediation-oriented planning intent (Policy C successor protocol)",
    )
    plan_parser.add_argument(
        "--runs-root",
        default=None,
        help=(
            "HTR runs-storage root for observation and for canonical project_dir "
            "on APIs that require it (same Phase 1 path contract as base_dir; "
            "proposal input only — does not mutate storage)"
        ),
    )
    plan_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a concise human summary to stderr (stdout remains JSON only)",
    )

    htr_parser.set_defaults(func=cmd_htr)
