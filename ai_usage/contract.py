from __future__ import annotations

from datetime import datetime, timezone

# Canonical provider grid order for the tray. (key, display label, mode)
# mode ∈ {budget (rolling %-windows), tokens (log-summed counts),
#         balance (pay-as-you-go outstanding-$),
#         spend (month-to-date estimated-$ from token counts × rate card)}.
PROVIDERS: list[tuple[str, str, str]] = [
    ("anthropic", "Claude", "budget"),
    ("openai-codex", "Codex", "budget"),
    ("kimi", "Kimi K3", "budget"),
    ("deepseek", "DeepSeek", "balance"),
    ("gemini", "Gemini", "spend"),
    ("xai", "Grok", "tokens"),
    ("opencode-go", "OpenCode Go", "tokens"),
]

# Maps AccountUsageWindow.label (emitted by agent/account_usage.py fetchers)
# to (canonical window id, tray display label).
WINDOW_LABEL_TO_ID: dict[str, tuple[str, str]] = {
    # Anthropic (_fetch_anthropic_account_usage)
    "Current session": ("5h", "5h"),
    "Current week": ("wk", "Weekly"),
    "Opus week": ("wk_opus", "Weekly · Opus"),
    "Sonnet week": ("wk_sonnet", "Weekly · Sonnet"),
    # Codex (_fetch_codex_account_usage)
    "Session": ("5h", "Session"),
    "Weekly": ("wk", "Weekly"),
}

# state.db billing_provider substrings that map to a canonical tokens-mode key.
# Best-effort; extend as new routes appear. Verified live values today:
# only "openai-codex" and "anthropic" present, so these start empty.
BILLING_PROVIDER_ALIASES: dict[str, list[str]] = {
    "kimi": ["kimi", "moonshot"],
    "gemini": ["gemini", "google", "generativelanguage"],
    "xai": ["xai", "grok"],
}

# (window id, tray display label, window length in seconds)
TOKEN_WINDOWS: list[tuple[str, str, int]] = [
    ("5h", "5h", 5 * 3600),
    ("24h", "Today", 24 * 3600),
    ("7d", "Week", 7 * 86400),
]


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, second precision."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
