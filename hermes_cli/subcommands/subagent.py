"""``hermes subagent`` subcommand parser.

Wired from ``hermes_cli/main.py``.  Provides the shell-facing entry point
for subagent model selection:

    hermes subagent                       # status
    hermes subagent model                 # interactive picker
    hermes subagent model <model>         # validated direct selection
    hermes subagent model --reset         # inherit parent
"""

from __future__ import annotations

from typing import Callable


def build_subagent_parser(subparsers, *, cmd_subagent: Callable) -> None:
    """Attach the ``subagent`` subcommand to ``subparsers``."""
    subagent_parser = subparsers.add_parser(
        "subagent",
        help="Inspect or pin the subagent model",
        description=(
            "Show the current subagent model selection.  When no override "
            "is configured, subagents inherit the parent model."
        ),
    )
    subparsers_sub = subagent_parser.add_subparsers(dest="subagent_command")

    # subagent (no subcommand) → status
    subagent_parser.set_defaults(func=cmd_subagent)

    # subagent model → status / select / reset
    model_parser = subparsers_sub.add_parser(
        "model",
        help="Select or reset the subagent model",
        description=(
            "Pin all subagents to a specific model, or reset to inherit "
            "the parent model.  The delegation provider/model is read on "
            "every child spawn — no restart needed."
        ),
    )
    model_parser.add_argument(
        "model",
        nargs="?",
        help="Model to pin subagents to (e.g. 'sonnet', 'gpt-5', 'claude-4-7-opus')",
    )
    model_parser.add_argument(
        "--provider",
        default=None,
        help="Provider to route subagents through (e.g. 'openrouter', 'nous')",
    )
    model_parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove the subagent model/provider override (inherit parent)",
    )
    model_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh provider model catalogs before opening the picker",
    )
    model_parser.set_defaults(func=cmd_subagent)
