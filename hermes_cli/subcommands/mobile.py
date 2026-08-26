"""``hermes mobile`` Android build command parser."""

from __future__ import annotations

from typing import Callable


def build_mobile_parser(subparsers, *, cmd_mobile: Callable) -> None:
    """Attach the Android mobile-client build command."""
    mobile_parser = subparsers.add_parser(
        "mobile",
        help="Build the Hermes Android client",
        description=(
            "Build the Capacitor Android client that embeds the Hermes Desktop renderer "
            "and connects to a remote Hermes gateway."
        ),
    )
    mobile_parser.add_argument(
        "--release",
        action="store_true",
        help="Build the optimized release variant (requires approved signing properties)",
    )
    mobile_parser.add_argument(
        "--sdk-root",
        help="Android SDK root (defaults to ANDROID_SDK_ROOT or ANDROID_HOME)",
    )
    mobile_parser.set_defaults(func=cmd_mobile)
