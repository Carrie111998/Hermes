"""``hermes doctor`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

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
        "--ack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Acknowledge a security advisory by ID and exit. After ack, the "
            "advisory will no longer trigger startup banners. Run `hermes "
            "doctor` first to see active advisories and their IDs. "
            "Accepts both Python advisory IDs (e.g. `shai-hulud-2026-05`) "
            "and npm GHSA IDs (e.g. `GHSA-qwww-vcr4-c8h2`)."
        ),
    )
    doctor_parser.add_argument(
        "--unack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Reverse an ack previously set via `--ack`: re-enable the "
            "advisory so it appears in future `hermes doctor` runs. "
            "Accepts both Python and npm advisory IDs."
        ),
    )
    doctor_parser.set_defaults(func=cmd_doctor)
