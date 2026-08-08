"""``hermes auth`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_auth_parser(subparsers, *, cmd_auth: Callable) -> None:
    """Attach the ``auth`` subcommand to ``subparsers``."""
    auth_parser = subparsers.add_parser(
        "auth",
        help="Manage pooled provider credentials",
    )
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action")
    auth_add = auth_subparsers.add_parser("add", help="Add a pooled credential")
    auth_add.add_argument(
        "provider",
        help="Provider id (for example: anthropic, openai-codex, openrouter)",
    )
    auth_add.add_argument(
        "--type",
        dest="auth_type",
        choices=["oauth", "api-key", "api_key"],
        help="Credential type to add",
    )
    auth_add.add_argument("--label", help="Optional display label")
    auth_add.add_argument(
        "--api-key", help="API key value (otherwise prompted securely)"
    )
    auth_add.add_argument("--portal-url", help="Nous portal base URL")
    auth_add.add_argument("--inference-url", help="Nous inference base URL")
    auth_add.add_argument("--client-id", help="OAuth client id")
    auth_add.add_argument("--scope", help="OAuth scope override")
    auth_add.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open a browser for OAuth login",
    )
    auth_add.add_argument(
        "--timeout", type=float, help="OAuth/network timeout in seconds"
    )
    auth_add.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for OAuth login",
    )
    auth_add.add_argument("--ca-bundle", help="Custom CA bundle for OAuth login")
    auth_list = auth_subparsers.add_parser("list", help="List pooled credentials")
    auth_list.add_argument("provider", nargs="?", help="Optional provider filter")
    auth_remove = auth_subparsers.add_parser(
        "remove", help="Remove a pooled credential by index, id, or label"
    )
    auth_remove.add_argument("provider", help="Provider id")
    auth_remove.add_argument(
        "target", help="Credential index, entry id, or exact label"
    )
    auth_reset = auth_subparsers.add_parser(
        "reset", help="Clear exhaustion status for all credentials for a provider"
    )
    auth_reset.add_argument("provider", help="Provider id")
    auth_status = auth_subparsers.add_parser(
        "status", help="Show auth status for a provider"
    )
    auth_status.add_argument("provider", help="Provider id")
    auth_usage = auth_subparsers.add_parser(
        "usage",
        help=(
            "Show live account usage (Codex / Anthropic / OpenRouter) for a "
            "provider, fetched outside an interactive session. Pass "
            "`--reset` to redeem a banked rate-limit reset credit instead "
            "(Codex only)."
        ),
    )
    auth_usage.add_argument(
        "provider",
        help=(
            "Provider id with a live usage endpoint "
            "(openai-codex, anthropic, openrouter)"
        ),
    )
    auth_usage.add_argument(
        "--all",
        action="store_true",
        dest="all_accounts",
        help=(
            "Render (or, with --reset, redeem on) every pool entry of this "
            "provider instead of just the resolver-selected one. "
            "Per-entry outcomes are reported inline; the command exits "
            "non-zero only if every entry failed."
        ),
    )
    auth_usage.add_argument(
        "--account",
        dest="account",
        default="",
        metavar="LABEL",
        help=(
            "Target the pool entry whose stored label matches LABEL. "
            "Use `hermes auth list <provider>` to see available labels."
        ),
    )
    auth_usage.add_argument(
        "--reset",
        action="store_true",
        dest="reset_action",
        help=(
            "Redeem one banked rate-limit reset credit on the Codex "
            "backend, instead of rendering the live usage snapshot. "
            "Mirror of the REPL `/usage reset [--force]` slash command."
        ),
    )
    auth_usage.add_argument(
        "--force",
        action="store_true",
        dest="force",
        help=(
            "With --reset: redeem even if the busiest rate-limit window is "
            "not fully used. A banked reset restores the FULL 5h + weekly "
            "allowance, so spending it early wastes most of it. Ignored "
            "without --reset."
        ),
    )
    auth_logout = auth_subparsers.add_parser(
        "logout", help="Log out a provider and clear stored auth state"
    )
    auth_logout.add_argument("provider", help="Provider id")
    auth_spotify = auth_subparsers.add_parser(
        "spotify", help="Authenticate Hermes with Spotify via PKCE"
    )
    auth_spotify.add_argument(
        "spotify_action",
        nargs="?",
        choices=["login", "status", "logout"],
        default="login",
    )
    auth_spotify.add_argument(
        "--client-id", help="Spotify app client_id (or set HERMES_SPOTIFY_CLIENT_ID)"
    )
    auth_spotify.add_argument(
        "--redirect-uri",
        help="Allow-listed localhost redirect URI for your Spotify app",
    )
    auth_spotify.add_argument("--scope", help="Override requested Spotify scopes")
    auth_spotify.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not attempt to open the browser automatically",
    )
    auth_spotify.add_argument(
        "--timeout", type=float, help="Callback/token exchange timeout in seconds"
    )
    auth_parser.set_defaults(func=cmd_auth)
