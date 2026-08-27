"""``hermes doctor`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_doctor_parser(subparsers, *, cmd_doctor: Callable) -> None:
    """Attach the ``doctor`` subcommand to ``subparsers``."""
    # =========================================================================
    # doctor command
    # =========================================================================
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check configuration and dependencies",
        description="Diagnose issues with Hermes Agent setup",
    )
    doctor_parser.add_argument(
        "--fix", action="store_true", help="Attempt to fix issues automatically"
    )
    doctor_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Opt-in: run one bounded, read-only real-call health probe per "
            "configured tool backend (Firecrawl/FAL/browser/MCP/TTS/STT) "
            "after the static checks. Makes real network calls."
        ),
    )
    doctor_parser.add_argument(
        "--ack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Acknowledge a security advisory by ID and exit. After ack, the "
            "advisory will no longer trigger startup banners. Run `hermes "
            "doctor` first to see active advisories and their IDs."
        ),
    )
    doctor_sub = doctor_parser.add_subparsers(
        dest="doctor_command", metavar="{deploy}"
    )
    doctor_sub.add_parser(
        "deploy",
        help="Verify every running hermes-agent process is on current code",
        description=(
            "List every running hermes-agent long-lived process (gateway, "
            "serve backend, dashboard) with pid, kind, start time, HEAD-at-start "
            "and current install HEAD. Flags STALE any process whose "
            "HERMES_AGENT_HEAD differs from current HEAD. Exits non-zero when "
            "any process is stale (or HEAD cannot be resolved)."
        ),
    )
    doctor_parser.set_defaults(func=cmd_doctor)
