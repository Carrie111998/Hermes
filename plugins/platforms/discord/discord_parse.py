"""Pure parse core for Discord inbound messages (layer 2).

The Discord inbound path splits into three layers:
  1. payload → SDK object (discord.py's gateway machinery: member caches,
     typed channels). VENDOR code — not oracled; the connector's
     discord.js/raw-payload equivalent is assumed to resolve the same
     fields (the documented SDK-equivalence axiom, parity report §5).
  2. SDK-view → MessageEvent fields — the HERMES-UNIQUE derivation rules:
     chat-type classification, thread identity, mention stripping,
     forwarded-snapshot folding, reply-reference extraction,
     attachment→MessageType classification, chat naming. THIS MODULE.
  3. Effects: media caching, auto-thread creation, gating, history
     backfill. Adapter-side.

Layer 2 consumes a small SDK-view IR (`DiscordMessageView`) constructible
from EITHER a discord.py ``Message`` (adapter's caller — attribute access,
``view_from_sdk_message``) OR a raw-ish dict of the same vocabulary (the
ingress conformance vector generator — ``view_from_dict``), so the
generator runs with NO discord.py dependency while exercising the SAME
rules the adapter uses.

Extracted from ``DiscordAdapter._handle_message`` (the derivation sites,
verbatim rules); the adapter delegates. Behavior changes here MUST be
reflected in the committed ingress vectors (regenerate) and reviewed
against the connector's normalizer (gateway-gateway src/relay/discord.ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from gateway.platforms.base import MessageType

#: Image extensions the media pipeline accepts before falling back to .jpg.
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


@dataclass
class AttachmentView:
    """SDK-view of one attachment (discord.py Attachment ≙ raw payload)."""

    content_type: Optional[str] = None
    filename: Optional[str] = None
    url: Optional[str] = None
    #: Native voice-note markers (raw: flags bit / duration_secs+waveform).
    is_voice_message: Optional[bool] = None
    duration: Optional[float] = None
    waveform: Optional[str] = None


@dataclass
class DiscordMessageView:
    """SDK-view IR: the ~18 fields layer 2 reads, SDK-agnostic."""

    message_id: str
    content: str
    channel_id: str
    channel_name: Optional[str] = None
    #: dm | guildText | thread — the SDK-resolved channel classification
    #: (discord.py: isinstance checks; raw: guild_id/thread flags).
    channel_kind: str = "guildText"
    guild_id: Optional[str] = None
    guild_name: Optional[str] = None
    parent_channel_id: Optional[str] = None
    parent_channel_name: Optional[str] = None
    parent_is_forum: bool = False
    author_id: str = ""
    author_name: str = ""
    author_display_name: str = ""
    author_is_bot: bool = False
    #: Resolved @mention user ids (SDK: message.mentions; raw: mentions[]).
    mentioned_user_ids: List[str] = field(default_factory=list)
    attachments: List[AttachmentView] = field(default_factory=list)
    #: Forwarded-message snapshots (message_snapshots): text + attachments.
    snapshot_texts: List[str] = field(default_factory=list)
    snapshot_attachments: List[AttachmentView] = field(default_factory=list)
    referenced_message_id: Optional[str] = None
    referenced_attachments: List[AttachmentView] = field(default_factory=list)
    is_reply: bool = False


def _att_from_dict(d: Any) -> AttachmentView:
    if not isinstance(d, dict):
        return AttachmentView()
    return AttachmentView(
        content_type=d.get("content_type"),
        filename=d.get("filename"),
        url=d.get("url"),
        is_voice_message=d.get("is_voice_message"),
        duration=d.get("duration"),
        waveform=d.get("waveform"),
    )


def view_from_dict(payload: dict) -> DiscordMessageView:
    """Build the IR from a raw-vocabulary dict (generator/test caller)."""
    return DiscordMessageView(
        message_id=str(payload.get("id", "")),
        content=str(payload.get("content") or ""),
        channel_id=str(payload.get("channel_id", "")),
        channel_name=payload.get("channel_name"),
        channel_kind=str(payload.get("channel_kind") or "guildText"),
        guild_id=(str(payload["guild_id"]) if payload.get("guild_id") else None),
        guild_name=payload.get("guild_name"),
        parent_channel_id=(
            str(payload["parent_channel_id"])
            if payload.get("parent_channel_id")
            else None
        ),
        parent_channel_name=payload.get("parent_channel_name"),
        parent_is_forum=bool(payload.get("parent_is_forum", False)),
        author_id=str((payload.get("author") or {}).get("id", "")),
        author_name=str((payload.get("author") or {}).get("username", "")),
        author_display_name=str(
            (payload.get("author") or {}).get("display_name")
            or (payload.get("author") or {}).get("global_name")
            or (payload.get("author") or {}).get("username", "")
        ),
        author_is_bot=bool((payload.get("author") or {}).get("bot", False)),
        mentioned_user_ids=[
            str(m.get("id"))
            for m in (payload.get("mentions") or [])
            if isinstance(m, dict) and m.get("id") is not None
        ],
        attachments=[_att_from_dict(a) for a in (payload.get("attachments") or [])],
        snapshot_texts=[
            str(s.get("content") or "").strip()
            for s in (payload.get("message_snapshots") or [])
            if isinstance(s, dict) and s.get("content")
        ],
        snapshot_attachments=[
            _att_from_dict(a)
            for s in (payload.get("message_snapshots") or [])
            if isinstance(s, dict)
            for a in (s.get("attachments") or [])
        ],
        referenced_message_id=(
            str((payload.get("referenced_message") or {}).get("id"))
            if (payload.get("referenced_message") or {}).get("id") is not None
            else None
        ),
        referenced_attachments=[
            _att_from_dict(a)
            for a in ((payload.get("referenced_message") or {}).get("attachments") or [])
        ],
        is_reply=bool(payload.get("referenced_message") or payload.get("is_reply")),
    )


def is_voice_message_attachment(att: AttachmentView) -> bool:
    """Native voice note vs plain audio file (verbatim adapter rule)."""
    if att.is_voice_message is not None:
        return bool(att.is_voice_message)
    return att.duration is not None and att.waveform is not None


def strip_own_mention(content: str, bot_user_id: str) -> str:
    """Remove the bot's OWN mention tokens (never other users') and trim —
    runs before command detection so `@Hermes /new` is recognized as /new."""
    if not bot_user_id:
        return content.strip()
    return (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
        .strip()
    )


def is_explicitly_mentioned(view: DiscordMessageView, bot_user_id: str) -> bool:
    """Resolved mentions list OR raw `<@id>`/`<@!id>` token in content."""
    if not bot_user_id:
        return False
    if bot_user_id in view.mentioned_user_ids:
        return True
    return f"<@{bot_user_id}>" in view.content or f"<@!{bot_user_id}>" in view.content


def effective_content(view: DiscordMessageView, bot_user_id: str = "") -> str:
    """Message text after snapshot folding + own-mention stripping.

    Forwarded messages (message_snapshots) with no caption of their own use
    the snapshot text as the content — the native adapter's forward rule.
    """
    raw = view.content.strip()
    if not raw and view.snapshot_texts:
        raw = "\n".join(view.snapshot_texts)
    if is_explicitly_mentioned(view, bot_user_id):
        raw = strip_own_mention(raw, bot_user_id)
    return raw


def all_attachments(view: DiscordMessageView) -> List[AttachmentView]:
    """Own + forwarded-snapshot + replied-to attachments, in adapter order."""
    return list(view.attachments) + list(view.snapshot_attachments) + list(
        view.referenced_attachments
    )


def classify_message_type(
    view: DiscordMessageView, bot_user_id: str = ""
) -> MessageType:
    """COMMAND / PHOTO / VIDEO / VOICE / AUDIO / DOCUMENT / TEXT — verbatim
    adapter rules: command prefix wins; else FIRST attachment classifies
    (any non-media attachment ⇒ DOCUMENT; no content_type ⇒ DOCUMENT)."""
    if effective_content(view, bot_user_id).startswith("/"):
        return MessageType.COMMAND
    for att in all_attachments(view):
        ct = att.content_type
        if ct:
            if ct.startswith("image/"):
                return MessageType.PHOTO
            if ct.startswith("video/"):
                return MessageType.VIDEO
            if ct.startswith("audio/"):
                return (
                    MessageType.VOICE
                    if is_voice_message_attachment(att)
                    else MessageType.AUDIO
                )
            return MessageType.DOCUMENT
        return MessageType.DOCUMENT
    return MessageType.TEXT


def chat_type_for(view: DiscordMessageView) -> str:
    """dm | thread | group (the adapter's three-way classification)."""
    if view.channel_kind == "dm":
        return "dm"
    if view.channel_kind == "thread":
        return "thread"
    return "group"


def thread_chat_name(view: DiscordMessageView) -> str:
    """Readable thread name incl. guild/parent context (verbatim rules:
    forum parents drop the '#', text parents keep it)."""
    thread_name = view.channel_name or view.channel_id or "thread"
    guild_name = view.guild_name
    parent_name = view.parent_channel_name
    if view.parent_is_forum and guild_name and parent_name:
        return f"{guild_name} / {parent_name} / {thread_name}"
    if parent_name and guild_name:
        return f"{guild_name} / #{parent_name} / {thread_name}"
    if parent_name:
        return f"{parent_name} / {thread_name}"
    return thread_name


def chat_name_for(view: DiscordMessageView) -> str:
    """DM → author name; thread → contextual thread name; channel →
    'Guild / #channel' when guild known."""
    chat_type = chat_type_for(view)
    if chat_type == "dm":
        return view.author_name
    if chat_type == "thread":
        return thread_chat_name(view)
    name = view.channel_name or view.channel_id
    if view.guild_name:
        return f"{view.guild_name} / #{name}"
    return name


@dataclass
class DiscordParsedMessage:
    """Payload/SDK-view-derivable fields of one Discord inbound message."""

    chat_type: str
    chat_id: str
    chat_name: str
    thread_id: Optional[str]
    parent_chat_id: Optional[str]
    guild_id: Optional[str]
    user_id: str
    user_name: str
    user_is_bot: bool
    message_id: str
    text: str
    message_type: MessageType
    reply_to_message_id: Optional[str]
    mentions_bot: bool


def parse_discord_message(
    view: DiscordMessageView, bot_user_id: str = ""
) -> DiscordParsedMessage:
    """Derive every SDK-view-only field (no auto-threading, no config
    gating, no media download — those are adapter-side effects layered on
    top of these values)."""
    chat_type = chat_type_for(view)
    is_thread = chat_type == "thread"
    return DiscordParsedMessage(
        chat_type=chat_type,
        chat_id=view.channel_id,
        chat_name=chat_name_for(view),
        thread_id=view.channel_id if is_thread else None,
        parent_chat_id=view.parent_channel_id if is_thread else None,
        guild_id=view.guild_id,
        user_id=view.author_id,
        user_name=view.author_display_name or view.author_name,
        user_is_bot=view.author_is_bot,
        message_id=view.message_id,
        text=effective_content(view, bot_user_id),
        message_type=classify_message_type(view, bot_user_id),
        reply_to_message_id=view.referenced_message_id,
        mentions_bot=is_explicitly_mentioned(view, bot_user_id),
    )
