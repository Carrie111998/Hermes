"""CLI support for native gateway replay."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from gateway.replay import ReplayPlan


def add_replay_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "replay",
        help="Replay bridge-message corpora through the gateway",
        description=(
            "Replay a typed bridge-message corpus through the real Hermes gateway "
            "adapter path without connecting live adapters or sending live outbound messages."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--plan",
        metavar="PATH",
        help="JSON replay plan containing platform, messages/corpus, and replay options.",
    )
    source.add_argument(
        "--corpus",
        metavar="PATH",
        help="JSON/JSONL bridge-message corpus. Defaults to --platform whatsapp.",
    )
    parser.add_argument(
        "--platform",
        default="whatsapp",
        help="Platform adapter to use with --corpus (default: whatsapp).",
    )
    parser.add_argument(
        "--delivery-mode",
        choices=("capture", "drop"),
        default="capture",
        help="Capture outbound sends in the replay result or silently drop them (default: capture).",
    )
    parser.add_argument(
        "--require-mention",
        dest="bypass_require_mention",
        action="store_false",
        default=True,
        help="Honor platform mention gates during replay (default bypasses them).",
    )
    parser.add_argument(
        "--enforce-auth",
        dest="bypass_auth",
        action="store_false",
        default=True,
        help="Honor live gateway authorization checks during replay (default bypasses them).",
    )
    parser.set_defaults(func=cmd_replay)
    return parser


def _load_plan(args) -> ReplayPlan:
    if getattr(args, "plan", None):
        return ReplayPlan.from_path(Path(args.plan))
    if getattr(args, "corpus", None):
        return ReplayPlan.from_corpus_path(
            Path(args.corpus),
            platform=getattr(args, "platform", "whatsapp"),
            delivery_mode=getattr(args, "delivery_mode", "capture"),
            bypass_require_mention=getattr(args, "bypass_require_mention", True),
            bypass_auth=getattr(args, "bypass_auth", True),
        )
    raise ValueError("provide --plan or --corpus")


def cmd_replay(args) -> None:
    from gateway.run import GatewayRunner

    plan = _load_plan(args)
    result = asyncio.run(GatewayRunner().replay(plan))
    print(result.to_json())
