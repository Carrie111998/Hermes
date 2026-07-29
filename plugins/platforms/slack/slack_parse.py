"""Pure parse core for Slack inbound message events (layer 2).

Same decomposition as discord_parse.py: layer 1 (Bolt's event plumbing +
workspace/token resolution) is vendor/adapter code; layer 3 (auth gating,
file downloads, assistant-thread metadata, channel-team caches) stays
adapter-side. THIS module is layer 2 — the Hermes-unique, payload-derivable
derivation rules extracted from ``SlackAdapter._handle_slack_message``:

  - DM detection (channel_type im/mpim, D-prefix fallback) and the
    1:1-vs-MPIM distinction (MPIM is a SHARED surface: channel-style
    gating applies; only 1:1 IMs earn the DM exemptions);
  - thread_ts session scoping (#15421/#15464 rules): genuine thread reply
    (thread_ts present and != ts) keys per-thread; top-level messages key
    on ts when reply_in_thread (the default) else None (shared channel
    session);
  - mention detection (the literal ``<@{bot_user_id}>`` token — server-
    resolved by Slack into the text);
  - bot/self-message classification (bot_id / subtype=bot_message).

Consumed by the adapter (delegation) and by the ingress conformance vector
generator (scripts/generate_ingress_vectors.py) — the executable spec for
the connector's normalizer (gateway-gateway src/relay/slack.ts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def slack_is_dm(event: Dict[str, Any]) -> bool:
    """im/mpim are DM-style; explicit channel/group are not; else D-prefix."""
    channel_type = str(event.get("channel_type") or "")
    if channel_type in {"im", "mpim"}:
        return True
    if channel_type in {"channel", "group"}:
        return False
    return str(event.get("channel") or "").startswith("D")


def slack_is_one_to_one_dm(event: Dict[str, Any]) -> bool:
    """Only a 1:1 IM earns the DM exemptions (mention-exempt, react-safe).
    An MPIM is a shared surface and obeys channel-style operator controls."""
    channel_type = str(event.get("channel_type") or "")
    if channel_type:
        return channel_type == "im"
    # No channel_type on the payload: a D-prefixed channel id covers both
    # im and mpim historically; the adapter treats prefix-only detection as
    # im (legacy single-workspace shape).
    return str(event.get("channel") or "").startswith("D")


def slack_session_thread_ts(
    event: Dict[str, Any],
    *,
    reply_in_thread: bool = True,
    dm_top_level_threads_as_sessions: bool = True,
) -> Optional[str]:
    """The session-scoping thread key (#15421 bug 1 / #15464 invariant).

    A ``thread_ts == ts`` payload (some thread-root shapes) is NOT a real
    reply and must not prevent the shared-session path.
    """
    ts = str(event.get("ts") or "")
    raw = event.get("thread_ts")
    thread_ts = str(raw) if raw else None
    if slack_is_dm(event):
        if thread_ts:
            return thread_ts
        return ts if dm_top_level_threads_as_sessions else None
    if thread_ts and thread_ts != ts:
        return thread_ts  # (a) genuine thread reply
    if reply_in_thread:
        return ts  # (b) synthetic thread root (legacy default)
    return None  # (c) shared channel session


def slack_chat_type(event: Dict[str, Any]) -> str:
    """dm | thread | channel — the classification the wire event carries.

    A genuine thread reply (thread_ts != ts) is "thread"; top-level channel
    messages are "channel" even though session scoping may key them on a
    synthetic thread root (that's a session-keying concern, not a
    chat-type one — mirrors the connector's slackChatType).
    """
    if slack_is_dm(event):
        return "dm"
    ts = str(event.get("ts") or "")
    raw = event.get("thread_ts")
    if raw and str(raw) != ts:
        return "thread"
    return "channel"


def slack_mentions_bot(event: Dict[str, Any], bot_user_id: str) -> bool:
    """Slack resolves mentions server-side into the literal ``<@UID>``
    token in text — substring detection is authoritative (adapter rule)."""
    if not bot_user_id:
        return False
    return f"<@{bot_user_id}>" in str(event.get("text") or "")


def slack_is_bot_message(event: Dict[str, Any]) -> bool:
    """bot_id present or subtype=bot_message ⇒ authored by an integration."""
    return bool(event.get("bot_id")) or str(event.get("subtype") or "") == "bot_message"


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the bot's own mention token(s) and trim (command detection
    runs on the stripped text, same as Discord)."""
    if not bot_user_id:
        return text.strip()
    return text.replace(f"<@{bot_user_id}>", "").strip()


@dataclass
class SlackParsedMessage:
    """Payload-derivable fields of one Slack message event."""

    chat_type: str
    chat_id: str
    is_dm: bool
    is_one_to_one_dm: bool
    session_thread_ts: Optional[str]
    user_id: Optional[str]
    team_id: Optional[str]
    message_id: str
    text: str
    mentions_bot: bool
    user_is_bot: bool


def parse_slack_event(
    event: Dict[str, Any],
    bot_user_id: str = "",
    *,
    reply_in_thread: bool = True,
    dm_top_level_threads_as_sessions: bool = True,
) -> SlackParsedMessage:
    """Derive every payload-only field of a Slack message event. Pure: no
    Bolt context, no workspace caches, no config reads — the two behavior
    toggles arrive as explicit arguments (the adapter passes its config)."""
    text = str(event.get("text") or "")
    mentions = slack_mentions_bot(event, bot_user_id)
    return SlackParsedMessage(
        chat_type=slack_chat_type(event),
        chat_id=str(event.get("channel") or ""),
        is_dm=slack_is_dm(event),
        is_one_to_one_dm=slack_is_one_to_one_dm(event),
        session_thread_ts=slack_session_thread_ts(
            event,
            reply_in_thread=reply_in_thread,
            dm_top_level_threads_as_sessions=dm_top_level_threads_as_sessions,
        ),
        user_id=(str(event["user"]) if event.get("user") else None),
        team_id=(str(event["team"]) if event.get("team") else None),
        message_id=str(event.get("ts") or ""),
        text=strip_bot_mention(text, bot_user_id) if mentions else text,
        mentions_bot=mentions,
        user_is_bot=slack_is_bot_message(event),
    )
