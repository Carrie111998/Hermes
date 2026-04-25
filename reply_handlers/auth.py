"""Authorization checks for inbound reply commands.

Reuses the existing TELEGRAM_ALLOWED_USERS and WHATSAPP_ALLOWED_USERS env vars
that the platform layer already enforces (see gateway/platforms/telegram.py:175
and scripts/whatsapp-bridge/bridge.js). We re-check here so the reply-handler
path is testable in isolation and remains safe even if a platform regression
loosens the upstream filter.
"""
from __future__ import annotations

import os
from typing import Union


def _allowed_set(env_var: str) -> set[str]:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_authorized_telegram(user_id: Union[int, str]) -> bool:
    """Check Telegram numeric user_id against TELEGRAM_ALLOWED_USERS CSV."""
    allowed = _allowed_set("TELEGRAM_ALLOWED_USERS")
    if not allowed:
        return False
    if "*" in allowed:
        return True
    return str(user_id) in allowed


def is_authorized_whatsapp(sender_jid: str) -> bool:
    """Check WhatsApp JID (e.g. '34652029134@s.whatsapp.net') against WHATSAPP_ALLOWED_USERS.

    Matches either the raw number prefix (`34652029134`) or the full JID.
    """
    allowed = _allowed_set("WHATSAPP_ALLOWED_USERS")
    if not allowed:
        return False
    if "*" in allowed:
        return True
    if sender_jid in allowed:
        return True
    number = sender_jid.split("@", 1)[0]
    return number in allowed
