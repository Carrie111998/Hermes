"""Pure parse core for Telegram inbound messages.

Extracted from ``TelegramAdapter._build_message_event`` (adapter.py) so the
payload-derivable field logic — chat-type normalization, the routable
thread-id rules (#3206/#22423), reply context with native partial quotes
(#22619), source identity fields — is importable WITHOUT adapter state.

Dual access model: every accessor works on BOTH python-telegram-bot
``Message`` objects (attribute access — the adapter's caller) and raw Bot
API JSON dicts (key access — the ingress conformance vector generator's
caller, and exactly the shape the relay connector's poller consumes). PTB
deliberately mirrors Bot API field names, so one field vocabulary serves
both; the generator therefore runs with NO python-telegram-bot dependency
while still exercising the SAME rules the adapter uses.

Deliberately excluded (adapter-side): DM-topic/group-topic config lookups
(chat_topic / auto_skill), rich_sent_store reply-text fallback, channel
prompts, media/sticker handling. The core derives every field that exists
in the payload alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: Telegram addresses a forum's General topic as thread id "1" while
#: delivering its messages with ``message_thread_id=None`` (#22423).
GENERAL_TOPIC_THREAD_ID = "1"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Field access across PTB objects (attributes) and Bot API dicts (keys)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_chat_type(chat: Any) -> str:
    """Bot API chat.type → Hermes chat_type (dm/group/channel).

    Normalizes through ``str`` so PTB enums (``ChatType.SUPERGROUP``),
    plain strings, and mocks all resolve identically — the adapter's
    long-standing rule.
    """
    telegram_chat_type = str(_get(chat, "type", "") or "").split(".")[-1].lower()
    if telegram_chat_type in {"group", "supergroup"}:
        return "group"
    if telegram_chat_type == "channel":
        return "channel"
    return "dm"


def effective_message_thread_id(message: Any) -> Optional[str]:
    """The ROUTABLE thread id for a message, or None.

    Rules (verbatim from the adapter):
      - forum-group topic messages keep their id;
      - plain group/DM replies carry a reply-UI anchor in
        ``message_thread_id`` that is NOT a session thread — dropped (#3206);
      - private-chat topic messages (DM topics) keep their id;
      - forum-group messages with NO id are the General topic → "1" (#22423).
    """
    chat = _get(message, "chat")
    chat_type = (
        str(_get(chat, "type", "") or "").split(".")[-1].lower() if chat is not None else ""
    )
    raw = _get(message, "message_thread_id")
    is_topic_message = bool(_get(message, "is_topic_message", False))
    is_forum_group = chat_type in ("group", "supergroup") and _get(chat, "is_forum", False) is True
    if raw is not None:
        if is_forum_group or (chat_type in ("group", "supergroup") and is_topic_message):
            return str(raw)
        if chat_type == "private" and is_topic_message:
            return str(raw)
        return None
    if is_forum_group:
        return GENERAL_TOPIC_THREAD_ID
    return None


def extract_reply_context(message: Any) -> tuple[Optional[str], Optional[str]]:
    """(reply_to_id, reply_to_text) from the payload alone.

    Prefers Telegram's native partial quote (``message.quote`` TextQuote,
    #22619) so replying to a selected substring doesn't inject the whole
    replied-to message; falls back to the quoted message's text/caption.
    The adapter layers two further fallbacks on top (rich-message echo,
    rich_sent_store) — both need adapter/gateway state and stay there.
    """
    reply = _get(message, "reply_to_message")
    if not reply:
        return None, None
    reply_id_raw = _get(reply, "message_id")
    reply_to_id = str(reply_id_raw) if reply_id_raw is not None else None
    quote = _get(message, "quote")
    quote_text = _get(quote, "text") if quote is not None else None
    if quote_text:
        return reply_to_id, str(quote_text)
    reply_to_text = _get(reply, "text") or _get(reply, "caption") or None
    return reply_to_id, (str(reply_to_text) if reply_to_text else None)


@dataclass
class TelegramParsedMessage:
    """Payload-derivable fields of one Telegram inbound message."""

    chat_type: str
    chat_id: str
    chat_name: Optional[str]
    thread_id: Optional[str]
    user_id: Optional[str]
    user_name: Optional[str]
    user_is_bot: bool
    message_id: str
    text: str
    reply_to_id: Optional[str]
    reply_to_text: Optional[str]


def parse_telegram_message(message: Any) -> TelegramParsedMessage:
    """Derive every payload-only field of a Telegram inbound message.

    Pure: no I/O, no adapter/config state. Mirrors ``_build_message_event``'s
    derivations exactly (source identity, thread routing, reply context);
    the adapter composes topic/skill/prompt enrichment on top. Behavior
    changes here MUST be reflected in the committed ingress conformance
    vectors (regenerate) and reviewed against the connector's normalizer.
    """
    chat = _get(message, "chat") or {}
    user = _get(message, "from_user") or _get(message, "from")

    chat_type = normalize_chat_type(chat)
    thread_id = effective_message_thread_id(message)

    chat_id = str(_get(chat, "id", ""))
    # PTB exposes ``full_name`` (computed) on Chat for private chats; Bot API
    # dicts carry first_name/last_name. Title covers groups/channels.
    chat_title = _get(chat, "title")
    chat_full_name = _get(chat, "full_name") or " ".join(
        p for p in (_get(chat, "first_name"), _get(chat, "last_name")) if p
    )
    chat_name = chat_title or (chat_full_name or None)

    user_id: Optional[str] = None
    user_name: Optional[str] = None
    user_is_bot = False
    if user is not None:
        uid = _get(user, "id")
        user_id = str(uid) if uid is not None else None
        user_name = _get(user, "full_name") or " ".join(
            p for p in (_get(user, "first_name"), _get(user, "last_name")) if p
        ) or None
        user_is_bot = bool(_get(user, "is_bot", False))
    elif chat_type in {"dm", "channel"}:
        # Channel posts / anonymous DMs: fall back to the chat identity —
        # the event still carries a stable author (adapter parity).
        user_id = chat_id
        user_name = chat_name if chat_type == "channel" else (chat_full_name or None)

    reply_to_id, reply_to_text = extract_reply_context(message)

    msg_id = _get(message, "message_id")
    return TelegramParsedMessage(
        chat_type=chat_type,
        chat_id=chat_id,
        chat_name=chat_name,
        thread_id=thread_id,
        user_id=user_id,
        user_name=user_name,
        user_is_bot=user_is_bot,
        message_id=str(msg_id) if msg_id is not None else "",
        text=str(_get(message, "text") or ""),
        reply_to_id=reply_to_id,
        reply_to_text=reply_to_text,
    )
