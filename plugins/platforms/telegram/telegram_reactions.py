"""Reactions and processing-lifecycle hooks for the Telegram adapter (adapter god-file slice).

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the god-file
decomposition campaign (telegram adapter shard, final slice).  This mixin holds
the message-reaction cluster: the forum-command lazy registration hook, the
``TELEGRAM_REACTIONS`` gate, the single-reaction set/clear senders, and the
``on_processing_start`` / ``on_processing_complete`` lifecycle hooks that drive
the 👀 → 👍/👎/cleared feedback on user messages.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO (``_bot``, ``_forum_lock``,
``_forum_command_registered``, ``name`` stay on the adapter).  Neutral
dependencies import at module top; the one adapter-local helper
(``_redact_telegram_error_text``) is imported lazily inside the methods that
use it so this module never imports the adapter at import time -> no import
cycle, and monkeypatches of ``adapter._redact_telegram_error_text`` keep
working.  The module-level ``logger`` keeps the adapter's exact logger name
(``"plugins.platforms.telegram.adapter"``) so log records are unchanged.
"""

from __future__ import annotations

import logging
import os

from gateway.platforms.base import MessageEvent, ProcessingOutcome
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

# Same logger object as the adapter module: log records keep identical
# provenance (name = plugins.platforms.telegram.adapter).
logger = logging.getLogger("plugins.platforms.telegram.adapter")


class TelegramReactionsMixin:
    """Reactions and processing-lifecycle hooks for TelegramAdapter."""

    async def _ensure_forum_commands(self, message) -> None:
        """Lazy-register bot commands for forum supergroups.

        Forum topics don't inherit AllGroupChats scope — Telegram resolves
        via BotCommandScopeChat(chat_id).  Register on first message so the
        command menu works in topic views.
        """
        async with self._forum_lock:
            try:
                chat = getattr(message, "chat", None)
                if not chat or not getattr(chat, "is_forum", False):
                    return
                chat_id = int(chat.id)
                if chat_id in self._forum_command_registered:
                    return
                from telegram import BotCommand, BotCommandScopeChat
                from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands
                menu_commands, _ = telegram_menu_commands(max_commands=telegram_menu_max_commands())
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                await self._bot.set_my_commands(bot_commands, scope=BotCommandScopeChat(chat_id=chat_id))
                self._forum_command_registered.add(chat_id)
                logger.info("[%s] Lazy-registered %d commands for forum chat %s", self.name, len(bot_commands), chat_id)
            except Exception as e:
                from plugins.platforms.telegram.adapter import _redact_telegram_error_text
                logger.warning("[%s] Forum command lazy-registration failed: %s", self.name, _redact_telegram_error_text(e))

    # ── Message reactions (processing lifecycle) ──────────────────────────

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("TELEGRAM_REACTIONS", "false").lower() not in {"false", "0", "no"}

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a Telegram message."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as e:
            from plugins.platforms.telegram.adapter import _redact_telegram_error_text
            logger.debug("[%s] set_message_reaction failed (%s): %s", self.name, emoji, _redact_telegram_error_text(e))
            return False

    async def _clear_reactions(self, chat_id: str, message_id: str) -> bool:
        """Clear all reactions from a Telegram message.

        Calling ``set_message_reaction`` with ``reaction=None`` (or an empty
        sequence) is the documented Bot API way to remove all bot-set
        reactions on a message — equivalent to Bot API 10.0's
        ``deleteMessageReaction`` but supported in PTB 22.6 already.
        """
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=None,
            )
            return True
        except Exception as e:
            from plugins.platforms.telegram.adapter import _redact_telegram_error_text
            logger.debug("[%s] clear reactions failed: %s", self.name, _redact_telegram_error_text(e))
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction.

        Unlike Discord (additive reactions), Telegram's set_message_reaction
        replaces all existing reactions in one call — no remove step needed.

        On CANCELLED outcomes (e.g. the user runs ``/stop``, or a session is
        interrupted mid-flight), we explicitly clear the 👀 in-progress
        reaction so it doesn't linger on the user's message indefinitely.
        Without this clear, the only way to remove the 👀 was to wait for
        another agent run to swap it to 👍/👎 — which never happens if the
        cancellation was the last activity in the chat.
        """
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            await self._clear_reactions(chat_id, message_id)
        else:
            await self._set_reaction(
                chat_id,
                message_id,
                "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e",
            )
