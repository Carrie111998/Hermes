"""CLI support for native gateway replay."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from gateway.replay import ReplayCorpus, ReplayPlan


def add_replay_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "replay",
        help="Replay bridge-message corpora through the gateway",
        description=(
            "Replay a typed bridge-message corpus through the real Hermes gateway "
            "adapter path without connecting live adapters or sending live outbound messages. "
            "ReplayCorpus applies deterministic ordering, dedup, bare-reaction skipping, "
            "quote/media preservation, missing-media reporting, and the per-turn future-read fence "
            "before messages feed the ReplayPlan."
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
    source.add_argument(
        "--bridge-message-log",
        metavar="PATH",
        help="SQLite DB containing bridge_message_log rows (first-class ReplayCorpus source).",
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
    parser.add_argument("--chat-id", help="bridge_message_log chat_jid to replay.")
    parser.add_argument("--since-sgt", help="Inclusive bridge_message_log sgt lower bound.")
    parser.add_argument("--until-sgt", help="Exclusive bridge_message_log sgt upper bound.")
    parser.add_argument("--limit-messages", type=int, help="Limit bridge_message_log rows before policy skips.")
    parser.add_argument(
        "--skip-messages",
        type=int,
        default=0,
        help="Skip the first N ordered bridge_message_log rows; reported in corpus_report.",
    )
    parser.add_argument(
        "--media-root",
        help="Flat directory for remapping missing bridge_message_log media basenames; unresolved media is reported.",
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
    if getattr(args, "bridge_message_log", None):
        if not getattr(args, "chat_id", None):
            raise SystemExit("--bridge-message-log requires --chat-id")
        if not getattr(args, "since_sgt", None):
            raise SystemExit("--bridge-message-log requires --since-sgt")
        corpus = ReplayCorpus.from_bridge_message_log(
            Path(args.bridge_message_log),
            chat_id=args.chat_id,
            since_sgt=args.since_sgt,
            until_sgt=getattr(args, "until_sgt", None),
            limit=getattr(args, "limit_messages", None),
            skip_messages=getattr(args, "skip_messages", 0),
            media_root=getattr(args, "media_root", None),
        )
        return ReplayPlan(
            platform=getattr(args, "platform", "whatsapp"),
            delivery_mode=getattr(args, "delivery_mode", "capture"),
            bypass_require_mention=getattr(args, "bypass_require_mention", True),
            bypass_auth=getattr(args, "bypass_auth", True),
            **corpus.to_plan_kwargs(),
        )
    raise ValueError("provide --plan, --corpus, or --bridge-message-log")


def cmd_replay(args) -> None:
    from gateway.run import GatewayRunner

    plan = _load_plan(args)
    result = asyncio.run(GatewayRunner().replay(plan))
    print(result.to_json())
