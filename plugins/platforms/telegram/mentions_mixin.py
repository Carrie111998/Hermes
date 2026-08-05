"""Bot-mention detection and exclusive-mention routing methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c1 cluster: whether a message explicitly addresses this bot (or another bot) via @-mention, bot_command entity, or configured wake-word patterns, plus the TTL-bounded identity re-check that keeps routing correct after a BotFather rename.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (``_FOREIGN_BOT_HANDLE_RE``) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``MentionsMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

import asyncio
import logging
import re
from typing import Any, Optional

try:
    from telegram import Message
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any

logger = logging.getLogger("plugins.platforms.telegram.adapter")

class MentionsMixin:
    """Bot-mention detection and exclusive-mention routing cluster lifted verbatim from ``TelegramAdapter``."""

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

    def _message_mentions_bot(self, message: Message) -> bool:
        if not self._bot:
            return False

        bot_username = self._current_bot_username()
        bot_id = getattr(self._bot, "id", None)
        expected = f"@{bot_username}" if bot_username else None

        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []

        # Telegram parses mentions server-side and emits MessageEntity objects
        # (type=mention for @username, type=text_mention for @FirstName targeting
        # a user without a public username). Those entities are authoritative:
        # raw substring matches like "foo@hermes_bot.example" are not mentions
        # (bug #12545). Entities also correctly handle @handles inside URLs, code
        # blocks, and quoted text, where a regex scan would over-match.
        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type == "mention" and expected:
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    if source_text[offset:offset + length].strip().lower() == expected:
                        return True
                elif entity_type == "text_mention":
                    user = getattr(entity, "user", None)
                    if user and getattr(user, "id", None) == bot_id:
                        return True
                elif entity_type == "bot_command" and expected:
                    # Telegram's official group-disambiguation form for slash
                    # commands (``/cmd@botname``) is emitted as a single
                    # ``bot_command`` entity covering the whole span — there
                    # is no accompanying ``mention`` entity. Treat it as a
                    # direct address to this bot when the ``@botname`` suffix
                    # matches. This is the form Telegram's own command menu
                    # autocomplete produces in groups, so dropping it at the
                    # mention gate would break /new, /reset, /help, ... for
                    # every group that has ``require_mention`` enabled (#15415).
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    command_text = source_text[offset:offset + length]
                    at_index = command_text.find("@")
                    if at_index < 0:
                        continue
                    if command_text[at_index:].strip().lower() == expected:
                        return True
        if bot_username:
            return bot_username in self._extract_bot_mention_usernames(message, bot_username)
        return False

    def _schedule_bot_identity_recheck(self) -> None:
        """Fire a TTL-guarded identity refresh in the background.

        Called when routing is about to discard a message because the bot
        handles it names don't include ours — the exact symptom of a stale
        username after a BotFather rename. The TTL in
        ``_refresh_bot_identity`` bounds this to one getMe per
        ``_BOT_IDENTITY_TTL_SECONDS``, so a busy group that legitimately
        addresses other bots cannot turn this into per-message API traffic.
        Fire-and-forget: the current message still routes on what we know now.
        """
        existing = getattr(self, "_bot_identity_refresh_task", None)
        if existing is not None and not existing.done():
            return
        if self._bot_identity_is_fresh():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._refresh_bot_identity())
        self._bot_identity_refresh_task = task
        tracked = getattr(self, "_background_tasks", None)
        if isinstance(tracked, set):
            tracked.add(task)
            task.add_done_callback(tracked.discard)

    def _explicit_bot_mentions_exclude_self(self, message: Message) -> bool:
        """Return True when explicit bot handles target other bots, not this one.

        Telegram groups can contain several Hermes bot profiles. A message like
        ``@bot3 hi @bot4`` must not wake ``@bot1`` through reply/wake-word
        fallbacks. Treat explicit bot-handle mentions as an exclusive routing
        hint: if at least one @...bot username is present and none matches this
        adapter's own bot username, this adapter should ignore the message.

        MessageEntity values are preferred, but some Telegram clients expose
        selected bot handles as plain text in group messages. Foreign handles
        are limited to the ``...bot`` shape so human @handles never suppress
        this bot; our own handle is matched by identity, so a collectible
        username without that suffix still counts as addressing us.
        """
        if not self._bot:
            return False

        bot_username = self._current_bot_username()
        if not bot_username:
            return False

        mentioned_bot_usernames = self._extract_bot_mention_usernames(message, bot_username)
        excludes_self = bool(mentioned_bot_usernames) and bot_username not in mentioned_bot_usernames
        if excludes_self:
            # Either the message really is for another bot, or our cached
            # handle is stale after a rename and we are about to ignore a
            # message addressed to us. Re-check identity out of band (TTL
            # bounded) so the mistake self-corrects instead of persisting.
            self._schedule_bot_identity_recheck()
        return excludes_self

    def _message_matches_mention_patterns(self, message: Message) -> bool:
        if not self._mention_patterns:
            return False
        for candidate in (getattr(message, "text", None), getattr(message, "caption", None)):
            if not candidate:
                continue
            for pattern in self._mention_patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _is_guest_mention(self, message: Message) -> bool:
        """Return True for the narrow guest-mode bypass: explicit bot mention.

        The caller (:meth:`_should_process_message`) has already verified
        the message is a group chat, so that check is not repeated here.
        """
        return self._telegram_guest_mode() and self._message_mentions_bot(message)

    def _clean_bot_trigger_text(self, text: Optional[str]) -> Optional[str]:
        bot_username = self._current_bot_username()
        if not text or not bot_username:
            return text
        username = re.escape(bot_username)
        cleaned = re.sub(rf"(?i)@{username}\b[,:\-]*\s*", "", text).strip()
        return cleaned or text
