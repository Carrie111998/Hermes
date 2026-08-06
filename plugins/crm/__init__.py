"""Personal CRM (keep-in-touch) plugin.

A Dex-style personal relationship manager. Registers only an operator-facing
CLI surface (``hermes crm ...``); no model tools are added. The agent operates
the CRM through the terminal tool, and the ``digest`` subcommand is designed to
be scheduled via ``hermes cron`` for daily keep-in-touch nudges.
"""

from __future__ import annotations

from plugins.crm.cli import crm_command, register_cli


def register(ctx) -> None:
    ctx.register_cli_command(
        name="crm",
        help="Personal CRM — contacts, keep-in-touch cadences, and reminders",
        setup_fn=register_cli,
        handler_fn=crm_command,
        description=(
            "Operator CLI for the personal CRM (keep-in-touch) pipeline. "
            "Manages contacts, logs interactions, tracks keep-in-touch "
            "cadences and important dates, and renders a cron-ready daily "
            "digest of who to reach out to."
        ),
    )
