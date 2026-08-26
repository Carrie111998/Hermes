"""``hermes webhook`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_webhook_parser(subparsers, *, cmd_webhook: Callable) -> None:
    """Attach the ``webhook`` subcommand to ``subparsers``."""
    # =========================================================================
    # webhook command
    # =========================================================================
    webhook_parser = subparsers.add_parser(
        "webhook",
        help="Manage dynamic webhook subscriptions",
        description="Create, list, and remove webhook subscriptions for event-driven agent activation",
    )
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_action")

    wh_sub = webhook_subparsers.add_parser(
        "subscribe", aliases=["add"], help="Create a webhook subscription"
    )
    wh_sub.add_argument("name", help="Route name (used in URL: /webhooks/<name>)")
    wh_sub.add_argument(
        "--prompt", default="", help="Prompt template with {dot.notation} payload refs"
    )
    wh_sub.add_argument(
        "--events", default="", help="Comma-separated event types to accept"
    )
    wh_sub.add_argument("--description", default="", help="What this subscription does")
    wh_sub.add_argument(
        "--skills", default="", help="Comma-separated skill names to load"
    )
    wh_sub.add_argument(
        "--deliver",
        default="log",
        help="Delivery target: log, telegram, discord, slack, etc.",
    )
    wh_sub.add_argument(
        "--deliver-chat-id",
        default="",
        help="Target chat ID for cross-platform delivery",
    )
    wh_sub.add_argument(
        "--secret", default="", help="HMAC secret (auto-generated if omitted)"
    )
    wh_sub.add_argument(
        "--deliver-only",
        action="store_true",
        help="Skip the agent — deliver the rendered prompt directly as the "
        "message. Zero LLM cost. Requires --deliver to be a real target "
        "(not 'log').",
    )
    wh_sub.add_argument(
        "--script",
        default="",
        help="Filter/transform script under ~/.hermes/scripts/. The route "
        "payload is passed as JSON on stdin; empty stdout, [SILENT], or a "
        "nonzero exit code ignores the webhook.",
    )

    webhook_subparsers.add_parser(
        "list", aliases=["ls"], help="List all dynamic subscriptions"
    )

    wh_rm = webhook_subparsers.add_parser(
        "remove", aliases=["rm"], help="Remove a subscription"
    )
    wh_rm.add_argument("name", help="Subscription name to remove")

    wh_test = webhook_subparsers.add_parser(
        "test", help="Send a test POST to a webhook route"
    )
    wh_test.add_argument("name", help="Subscription name to test")
    wh_test.add_argument(
        "--payload", default="", help="JSON payload to send (default: test payload)"
    )

    # ── Orca completion bridge ──────────────────────────────────────────
    wh_orca_reg = webhook_subparsers.add_parser(
        "orca-register",
        help="Register an Orca run so its completion wakes this conversation",
    )
    wh_orca_reg.add_argument("--run-id", required=True, help="Orca run id")
    wh_orca_reg.add_argument(
        "--goal", default="", help="What the run was asked to do"
    )
    wh_orca_reg.add_argument(
        "--session-key",
        default="",
        help="Gateway session key to report back to (default: the live "
        "HERMES_SESSION_KEY, so the completion returns to the thread that "
        "launched the run)",
    )
    wh_orca_reg.add_argument(
        "--worktree", default="", help="Worktree path, for the report"
    )
    wh_orca_reg.add_argument(
        "--terminal", default="", help="Orca terminal handle, for the report"
    )

    wh_orca_runs = webhook_subparsers.add_parser(
        "orca-runs", help="List registered Orca runs"
    )
    wh_orca_runs.add_argument(
        "--state", default="", help="Filter by state (open, completed)"
    )

    webhook_subparsers.add_parser(
        "orca-sweep",
        help="Re-query Orca for every open run and deliver any that finished",
    )

    wh_orca_notify = webhook_subparsers.add_parser(
        "orca-notify",
        help="Send a signed completion notification to the local bridge",
    )
    wh_orca_notify.add_argument("--run-id", required=True, help="Orca run id")
    wh_orca_notify.add_argument(
        "--event", default="worker_done",
        help="Signal kind (worker_done, hermes-ready, exit, stop, ...)",
    )
    wh_orca_notify.add_argument(
        "--route", default="orca", help="Webhook route name for the bridge"
    )
    wh_orca_notify.add_argument(
        "--secret", default="", help="HMAC secret (default: from config.yaml)"
    )
    wh_orca_notify.add_argument(
        "--event-id", default="", help="Sender-supplied id used for dedupe"
    )
    wh_orca_notify.add_argument(
        "--sequence", type=int, default=-1,
        help="Monotonic sequence number for out-of-order rejection",
    )

    webhook_parser.set_defaults(func=cmd_webhook)
