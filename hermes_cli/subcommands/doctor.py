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
        "--audit",
        action="store_true",
        help=(
            "Also run `npm audit` against the Node.js dependency trees. Off by "
            "default: a single audit takes 40-120s per target (four targets), "
            "which dominated the runtime of every `hermes doctor` while rarely "
            "finishing. With this flag each audit gets a budget generous enough "
            "to complete (override with HERMES_DOCTOR_NPM_AUDIT_TIMEOUT)."
        ),
    )
    doctor_parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Run the expensive state.db checks too (full-text index "
            "verification). Re-reads every indexed message, so it takes "
            "minutes on a multi-GB database; the default run skips it."
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
    doctor_parser.set_defaults(func=cmd_doctor)
