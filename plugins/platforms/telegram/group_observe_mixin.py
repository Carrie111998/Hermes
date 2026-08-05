"""Observed group-context attribution methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c4 cluster: storing skipped unmentioned group chatter into the session transcript and aligning triggered turns with observed-history attribution (shared anonymised sources and channel prompts).

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (``_GENERAL_TOPIC_THREAD_ID``) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``GroupObserveMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

from datetime import datetime, timezone
import dataclasses
import logging
from typing import Any, Optional

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
)

try:
    from telegram import Message
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any

logger = logging.getLogger("plugins.platforms.telegram.adapter")

class GroupObserveMixin:
    """Observed group-context attribution cluster lifted verbatim from ``TelegramAdapter``."""

    def _should_observe_unmentioned_group_message(self, message: Message) -> bool:
        """Return True when a group message should be stored but not dispatched."""
        if self._is_own_message(message):
            return False
        if not self._telegram_observe_unmentioned_group_messages():
            return False
        if not self._is_group_chat(message):
            return False

        thread_id = getattr(message, "message_thread_id", None)
        allowed_topics = self._telegram_allowed_topics()
        if allowed_topics:
            topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
            if topic_id not in allowed_topics:
                return False

        if thread_id is not None:
            try:
                if int(thread_id) in self._telegram_ignored_threads():
                    return False
            except (TypeError, ValueError):
                return False

        chat_id_str = str(getattr(getattr(message, "chat", None), "id", ""))
        if self._telegram_exclusive_bot_mentions() and self._explicit_bot_mentions_exclude_self(message):
            return False

        allowed = self._telegram_observe_allowed_chats()
        # Observed context is shared at chat/topic scope so a later trigger from
        # another user can see it.  Require an explicit chat allowlist; that
        # keeps shared observed history limited to operator-approved groups and
        # lets gateway authorization pass even after the shared session source
        # drops the per-sender user_id.
        if not allowed or chat_id_str not in allowed:
            return False

        # Only observe messages skipped by the require_mention gate.  If the
        # message would be processed normally, let the dispatcher handle it;
        # if require_mention is disabled, every group message is a request.
        if chat_id_str in self._telegram_free_response_chats():
            return False
        if self._telegram_is_free_response_topic(message):
            return False
        if not self._telegram_require_mention():
            return False
        if self._is_reply_to_bot(message):
            return False
        if self._message_mentions_bot(message):
            return False
        if self._message_matches_mention_patterns(message):
            return False
        return True

    def _telegram_group_observe_shared_source(self, source):
        """Return a chat/topic-scoped source for observed Telegram group context."""
        return dataclasses.replace(source, user_id=None, user_name=None, user_id_alt=None)

    def _telegram_group_observe_attributed_text(self, event: MessageEvent) -> str:
        user_id = event.source.user_id or "unknown"
        sender = event.source.user_name or user_id
        return f"[{sender}|{user_id}]\n{event.text or ''}"

    def _telegram_group_observe_channel_prompt(self) -> str:
        username = self._current_bot_username() or "unknown"
        bot_id = getattr(getattr(self, "_bot", None), "id", None) or "unknown"
        return (
            "You are handling a Telegram group chat message.\n"
            f"- Your identity: user_id={bot_id}, @-mention name in this group=@{username}\n"
            "- observed Telegram group context may be provided in a separate context-only block "
            "before the current message; it is not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and use observed context only when the current message asks for it."
        )

    def _apply_telegram_group_observe_attribution(self, event: MessageEvent) -> MessageEvent:
        """Align triggered group turns with observed-history attribution."""
        if not self._telegram_observe_unmentioned_group_messages():
            return event
        raw_message = getattr(event, "raw_message", None)
        if not raw_message or not self._is_group_chat(raw_message):
            return event
        chat_id_str = str(getattr(getattr(raw_message, "chat", None), "id", ""))
        allowed = self._telegram_observe_allowed_chats()
        if not allowed or chat_id_str not in allowed:
            return event
        shared_source = self._telegram_group_observe_shared_source(event.source)
        observe_prompt = self._telegram_group_observe_channel_prompt()
        channel_prompt = f"{event.channel_prompt}\n\n{observe_prompt}" if event.channel_prompt else observe_prompt
        if event.message_type == MessageType.COMMAND:
            # Commands must retain the original source (with user_id) so
            # slash-access control (_check_slash_access) can identify the
            # sender.  Replacing the source with an anonymised shared source
            # (user_id=None) causes admin-only commands like /new to be
            # denied even when the sender is an admin, because
            # SlashAccessPolicy.is_admin(None) is always False.
            # Still inject channel_prompt for group context.
            return dataclasses.replace(
                event,
                channel_prompt=channel_prompt,
            )
        return dataclasses.replace(
            event,
            text=self._telegram_group_observe_attributed_text(event),
            source=shared_source,
            channel_prompt=channel_prompt,
        )

    def _observe_unmentioned_group_message(
        self,
        message: Message,
        msg_type: MessageType,
        update_id: Optional[int] = None,
        event: Optional[MessageEvent] = None,
    ) -> None:
        """Append skipped group chatter to the target session without dispatching."""
        store = getattr(self, "_session_store", None)
        if not store:
            return
        try:
            event = event or self._build_message_event(message, msg_type, update_id=update_id)
            shared_source = self._telegram_group_observe_shared_source(event.source)
            session_entry = store.get_or_create_session(shared_source)
            entry = {
                "role": "user",
                "content": self._telegram_group_observe_attributed_text(event),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if event.message_id:
                entry["message_id"] = str(event.message_id)
            store.append_to_transcript(session_entry.session_id, entry)
            adapter_name = getattr(self, "name", "telegram")
            logger.info(
                "[%s] Telegram group message observed (no bot trigger): chat=%s from=%s",
                adapter_name,
                getattr(getattr(message, "chat", None), "id", "unknown"),
                event.source.user_id or "unknown",
            )
        except Exception as exc:
            adapter_name = getattr(self, "name", "telegram")
            logger.warning("[%s] Failed to observe Telegram group message: %s", adapter_name, exc)
