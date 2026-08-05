"""BotIdentityMixin.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the mechanical mixin lift
that produced ``TelegramAuthorizationMixin`` (PR #75742). This mixin
holds the bot-identity cluster: observing the bot's own @handle from inbound updates, the TTL-freshness probe, the ``get_me()`` refresh, reply-to-bot detection and mention-username extraction. Class attributes the methods reference (``_FOREIGN_BOT_HANDLE_RE``, ``_BOT_IDENTITY_TTL_SECONDS``, ``_BOT_IDENTITY_PROBE_TIMEOUT``) stay on ``TelegramAdapter`` and resolve via the MRO.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO, and ``BotIdentityMixin`` precedes
``BasePlatformAdapter`` in the bases so resolution order is what it was
when these methods sat on the class.

Two details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``.
* ``Message`` (where used) is imported under the same ``ImportError`` guard
  the adapter uses, falling back to ``Any``.
"""

import asyncio
import logging
import re
import time
from typing import Any, Optional

try:
    from telegram import Message
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any



logger = logging.getLogger("plugins.platforms.telegram.adapter")


class BotIdentityMixin:
    """Bot-identity cluster lifted verbatim from ``TelegramAdapter``."""


    def _current_bot_username(self) -> str:
        """Return this bot's live @username (lowercased, no leading ``@``).

        Prefers the most recently observed handle over PTB's ``get_me()``
        cache. ``Bot.username`` reads ``Bot._bot_user``, which is written only
        by ``get_me()`` — after a BotFather rename it keeps returning the old
        handle, so every mention comparison silently stops matching and the
        exclusive-mention gate concludes the message is addressed to a
        different bot. Observing the handle from inbound updates closes that
        window without an extra Bot API round-trip.
        """
        observed = getattr(self, "_bot_username_observed", None)
        if observed:
            return observed
        return (getattr(self._bot, "username", None) or "").lstrip("@").lower()


    def _note_bot_username(self, username: Optional[str]) -> None:
        """Record the bot's current @username, logging real renames."""
        handle = (username or "").lstrip("@").lower()
        if not handle:
            return
        previous = getattr(self, "_bot_username_observed", None)
        if previous == handle:
            return
        self._bot_username_observed = handle
        self._bot_identity_checked_at = time.monotonic()
        if previous:
            logger.info(
                "[%s] Telegram bot username changed: @%s -> @%s "
                "(mention routing now follows the new handle)",
                self.name, previous, handle,
            )


    def _observe_bot_identity_from_message(self, message: Message) -> None:
        """Learn our own handle from a message Telegram says we authored.

        Telegram stamps the *current* username on the bot's own outgoing
        messages and on ``reply_to_message`` when a user replies to us, so a
        rename is observable from the update stream itself — no getMe needed.
        Only trusted when the user id matches this bot, so another account's
        handle can never be adopted as our own.
        """
        bot_id = getattr(self._bot, "id", None)
        if bot_id is None:
            return
        for candidate in (
            getattr(message, "from_user", None),
            getattr(getattr(message, "reply_to_message", None), "from_user", None),
        ):
            if candidate is None:
                continue
            if getattr(candidate, "id", None) != bot_id:
                continue
            self._note_bot_username(getattr(candidate, "username", None))


    def _bot_identity_is_fresh(self) -> bool:
        """True when identity was re-read within the TTL.

        ``None`` means never checked, which is always stale. Do not fold the
        sentinel into ``0.0``: monotonic clocks have an arbitrary epoch that
        can legitimately be smaller than the TTL on a freshly-booted host,
        which would make "never" look like "just now".
        """
        checked_at = getattr(self, "_bot_identity_checked_at", None)
        if checked_at is None:
            return False
        return (time.monotonic() - checked_at) < self._BOT_IDENTITY_TTL_SECONDS


    async def _refresh_bot_identity(self, *, force: bool = False) -> None:
        """Re-read the bot's identity from Telegram when the cache may be stale.

        ``get_me()`` rewrites PTB's ``Bot._bot_user`` in place, so this also
        repairs every other consumer of ``self._bot.username``. Best-effort:
        a failed probe leaves the last known handle in place.
        """
        bot = self._bot
        if bot is None or not callable(getattr(bot, "get_me", None)):
            return
        if not force and self._bot_identity_is_fresh():
            return
        try:
            me = await asyncio.wait_for(bot.get_me(), self._BOT_IDENTITY_PROBE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "[%s] Telegram identity refresh failed (keeping @%s): %s",
                self.name, self._current_bot_username() or "unknown", exc,
            )
            return
        self._bot_identity_checked_at = time.monotonic()
        self._note_bot_username(getattr(me, "username", None))


    def _is_reply_to_bot(self, message: Message) -> bool:
        if not self._bot or not getattr(message, "reply_to_message", None):
            return False
        reply_user = getattr(message.reply_to_message, "from_user", None)
        return bool(reply_user and getattr(reply_user, "id", None) == getattr(self._bot, "id", None))


    @classmethod
    def _extract_bot_mention_usernames(cls, message: Message, self_username: str = "") -> set[str]:
        """Extract explicit Telegram bot usernames mentioned in text/captions.

        Foreign handles are only treated as bot mentions when they look
        bot-shaped (``...bot``), which keeps human ``@handles`` from acting as
        routing hints. ``self_username`` opts our OWN handle into the same set
        regardless of shape: collectible (Fragment) usernames can be assigned
        to bots and need not end in "bot" (@jarvis, @pic), and a bot addressed
        by such a handle must still recognise itself.

        Entity mentions are authoritative. The raw-text fallback is intentionally narrow so
        entity-less mobile/client variants still work without treating email
        addresses or arbitrary substrings as bot mentions.
        """
        mentioned_bot_usernames: set[str] = set()
        own = (self_username or "").lstrip("@").lower()

        def _is_bot_handle(handle: str) -> bool:
            if not handle:
                return False
            if own and handle == own:
                return True
            return bool(cls._FOREIGN_BOT_HANDLE_RE.fullmatch(handle))

        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []

        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type not in {"mention", "bot_command"}:
                    continue
                offset = int(getattr(entity, "offset", -1))
                length = int(getattr(entity, "length", 0))
                if offset < 0 or length <= 0:
                    continue

                entity_text = source_text[offset:offset + length].strip()
                if entity_type == "mention":
                    handle = entity_text.lstrip("@").lower()
                    if _is_bot_handle(handle):
                        mentioned_bot_usernames.add(handle)
                    continue

                # Telegram emits /cmd@botname as one bot_command entity, not as
                # a separate mention entity. Treat that suffix as an explicit
                # bot address for exclusive multi-bot routing even when the
                # group has require_mention/free-response disabled.
                at_index = entity_text.find("@")
                if at_index < 0:
                    continue
                command_target = entity_text[at_index + 1:].strip().lower()
                if _is_bot_handle(command_target):
                    mentioned_bot_usernames.add(command_target)

        # Entity-less fallback for older/client-specific updates. If Telegram
        # supplied entities for a source, trust them and do not regex-rescue
        # malformed/URL/code spans that the server did not mark as mentions.
        for raw_text, entities in _iter_sources():
            if not raw_text or entities:
                continue
            for match in re.finditer(r"(?i)(?<![A-Za-z0-9_`/])@([A-Za-z0-9_]{2,31})\b", raw_text):
                handle = match.group(1).lower()
                if _is_bot_handle(handle):
                    mentioned_bot_usernames.add(handle)

        return mentioned_bot_usernames
