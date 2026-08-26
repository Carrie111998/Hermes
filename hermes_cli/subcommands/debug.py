"""``hermes debug`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_debug_parser(subparsers, *, cmd_debug: Callable) -> None:
    """Attach the ``debug`` subcommand to ``subparsers``."""
    # =========================================================================
    # debug command
    # =========================================================================
    debug_parser = subparsers.add_parser(
        "debug",
        help="Debug tools — inspect logs locally or share a sanitized system summary",
        description="Debug utilities for Hermes Agent. Use 'hermes debug share' to "
        "upload a sanitized, log-free system summary to a paste "
        "service and get a shareable URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes debug share              Upload debug report (asks for confirmation)
    hermes debug share --yes        Skip confirmation (for scripts/CI)
    hermes debug share --local --lines 500  Inspect more log lines locally
    hermes debug share --local      Print report locally (no upload)
    hermes debug share --local --no-redact  Inspect unredacted logs locally
    hermes debug share --nous       Upload to Nous-internal storage (private)
    hermes debug delete <url>       Delete a previously uploaded paste
""",
    )
    debug_sub = debug_parser.add_subparsers(dest="debug_command")
    share_parser = debug_sub.add_parser(
        "share",
        help="Upload a sanitized system summary and print a shareable URL",
    )
    share_parser.add_argument(
        "--lines",
        type=int,
        default=200,
        help="Number of log lines for --local or --nous (default: 200)",
    )
    share_parser.add_argument(
        "--expire",
        type=int,
        default=7,
        # Retained for CLI compatibility. Public pastes use the fixed six-hour
        # deletion schedule and private Nous bundles use their fixed retention.
        help=argparse.SUPPRESS,
    )
    share_parser.add_argument(
        "--local",
        action="store_true",
        help="Print the report locally instead of uploading",
    )
    share_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "Skip the confirmation prompt and upload immediately. Required "
            "in non-interactive contexts (scripts/CI); without it, and with "
            "no TTY on stdin, the command refuses rather than upload silently."
        ),
    )
    share_parser.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "Disable log redaction for --local or --nous. Public paste "
            "uploads always use a sanitized, log-free system summary."
        ),
    )
    share_parser.add_argument(
        "--nous",
        action="store_true",
        help=(
            "Upload the debug bundle to Nous-internal storage (AWS S3) instead "
            "of a public paste service. The bundle is private — viewable only "
            "by Nous staff (and allowlisted Discord mods) via a Google-login-"
            "gated viewer — and auto-deletes after 14 days. Still force-redacts "
            "secrets unless --no-redact is also passed."
        ),
    )
    delete_parser = debug_sub.add_parser(
        "delete",
        help="Delete a paste uploaded by 'hermes debug share'",
    )
    delete_parser.add_argument(
        "urls",
        nargs="*",
        default=[],
        help="One or more paste URLs to delete (e.g. https://paste.rs/abc123)",
    )
    debug_parser.set_defaults(func=cmd_debug)
