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
    # ``hermes doctor logs <path>`` — one-shot rename-rotation of a foreign
    # log (oMLX server.log, dashboard.error.log) so an operator or host-ops
    # automation can rotate on demand instead of hand-truncating (t_57aac3e7
    # fix 1). Reuses _rotate_error_log_if_needed via import.
    doctor_sub = doctor_parser.add_subparsers(
        dest="doctor_command", metavar="{logs}"
    )
    logs_parser = doctor_sub.add_parser(
        "logs",
        help="Rotate a log file on demand (rename-rotation)",
        description=(
            "One-shot rename-rotation of a single log file. Rotates "
            "<path> to <path>.1 (shifting .1 -> .2 -> ... up to --backups) "
            "so the writer's open fd keeps pointing at the old inode while a "
            "fresh file is created. Safe for foreign writers (oMLX, launchd "
            "stderr) that hold the file open."
        ),
    )
    logs_parser.add_argument(
        "path",
        help="Absolute path to the log file to rotate",
    )
    logs_parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help=(
            "Size cap in bytes. When omitted, the cap is inferred from the "
            "path (10 MiB for oMLX logs, 5 MiB otherwise). With --force this "
            "only affects reporting; rotation happens regardless."
        ),
    )
    logs_parser.add_argument(
        "--backups",
        type=int,
        default=None,
        help="Number of rotated backups to keep (default 3). 0 deletes instead.",
    )
    logs_parser.add_argument(
        "--force",
        action="store_true",
        help="Rotate even if the file is under the size cap (default).",
    )
    doctor_parser.set_defaults(func=cmd_doctor)
