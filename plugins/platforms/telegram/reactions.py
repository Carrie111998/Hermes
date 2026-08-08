"""Canonical Telegram standard reaction emoji."""


def canonical_standard_emoji(emoji: str) -> str | None:
    """Return Telegram's canonical form for a standard reaction emoji."""
    raw = str(emoji or "").strip()
    if not raw:
        return None
    try:
        from telegram.constants import ReactionEmoji

        allowed = tuple(str(getattr(item, "value", item)) for item in ReactionEmoji)
    except (ImportError, TypeError):
        allowed = ()
    if not allowed:
        # Older / minimal PTB: still validate the value at the Bot API boundary.
        return raw if "\u200d" in raw else raw.rstrip("\ufe0e\ufe0f") or raw
    if raw in allowed:
        return raw
    bare = raw.replace("\ufe0e", "").replace("\ufe0f", "")
    for value in allowed:
        if value.replace("\ufe0e", "").replace("\ufe0f", "") == bare:
            return value
    return None
