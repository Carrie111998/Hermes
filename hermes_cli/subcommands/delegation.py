"""``hermes delegation`` subcommand parser."""
from __future__ import annotations

from typing import Callable


def build_delegation_parser(subparsers, *, cmd_delegation: Callable) -> None:
    """Attach delegation profile management commands to ``subparsers``."""
    parser = subparsers.add_parser(
        "delegation",
        help="Manage subagent delegation settings",
        description="Manage subagent delegation settings and named credential profiles.",
    )
    actions = parser.add_subparsers(dest="delegation_action")
    profiles = actions.add_parser(
        "profiles",
        help="Manage named delegation credential profiles",
    )
    profile_actions = profiles.add_subparsers(dest="profiles_action")
    profile_actions.add_parser("list", aliases=["ls"], help="List delegation profiles")

    add = profile_actions.add_parser("add", help="Add or replace a delegation profile")
    add.add_argument("profile_name", help="Profile name (letters, numbers, '_' and '-')")
    add.add_argument("--model", help="Model override")
    add.add_argument("--provider", help="Provider override")
    add.add_argument("--base-url", help="OpenAI-compatible endpoint override")
    add.add_argument("--api-key", help="API key override (stored in config.yaml)")
    add.add_argument(
        "--api-mode",
        choices=["chat_completions", "codex_responses", "anthropic_messages"],
        help="Transport API mode override",
    )

    remove = profile_actions.add_parser(
        "remove", aliases=["rm"], help="Remove a delegation profile"
    )
    remove.add_argument("profile_name", help="Profile name")
    parser.set_defaults(func=cmd_delegation)
