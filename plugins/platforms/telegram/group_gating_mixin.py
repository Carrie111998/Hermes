"""GroupGatingMixin.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the mechanical mixin lift
that produced ``TelegramAuthorizationMixin`` (PR #75742). This mixin
holds the group-chat mention-gating cluster: ``require_mention``, guest mode, exclusive bot mentions, free-response chats/topics, mention-pattern compilation, group-chat classification and thread-id normalization, plus the ``_scoped_gate_env`` helper those config readers share. The chat and topic allowlist readers (``_telegram_allowed_chats``, ``_telegram_allowed_topics``, ``_telegram_group_allowed_chats``, ``_telegram_ignored_threads``, ``_telegram_observe_allowed_chats``) stay on the adapter: they were already lifted into ``TelegramAuthorizationMixin`` by the open extraction PR #75742, so lifting them again here would conflict. ``_scoped_gate_env`` is re-exported by the adapter module for its own stay-behind authorization readers.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO, and ``GroupGatingMixin`` precedes
``BasePlatformAdapter`` in the bases so resolution order is what it was
when these methods sat on the class.

Two details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``.
* ``Message`` (where used) is imported under the same ``ImportError`` guard
  the adapter uses, falling back to ``Any``.
"""

import json
import logging
import os
import re
from typing import Any, List, Optional

from gateway.platforms.helpers import compile_mention_patterns

try:
    from telegram import Message
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any



logger = logging.getLogger("plugins.platforms.telegram.adapter")


def _scoped_gate_env(name: str, default: str = "") -> str:
    """Read a TELEGRAM_*/GATEWAY_* authorization gate env var per-profile.

    Under gateway.multiplex_profiles the process env is first-writer-wins
    (the YAML→env bridge in ``_apply_yaml_config``), so a raw ``os.getenv``
    can return ANOTHER profile's allowlist (issue #72348, Telegram mirror).
    Reads the active profile's secret scope when installed; falls back to
    ``os.getenv`` outside multiplex — identical single-profile behavior.
    """
    try:
        from gateway.authz_mixin import _platform_gate_env

        return _platform_gate_env(name, default)
    except Exception:
        return (os.getenv(name) or default).strip()


class GroupGatingMixin:
    """Group mention-gating cluster lifted verbatim from ``TelegramAdapter``."""


    # ── Group mention gating ──────────────────────────────────────────────

    def _telegram_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}


    def _telegram_observe_unmentioned_group_messages(self) -> bool:
        """Return whether skipped unmentioned group messages are stored as context.

        When enabled with ``require_mention``, Telegram matches the Yuanbao /
        OpenClaw-style group UX: observe ordinary group chatter in the session
        transcript, but only dispatch the agent when the bot is explicitly
        addressed.
        """
        configured = self.config.extra.get("observe_unmentioned_group_messages")
        if configured is None:
            configured = self.config.extra.get("ingest_unmentioned_group_messages")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", "false").lower() in {"true", "1", "yes", "on"}


    def _telegram_guest_mode(self) -> bool:
        """Return whether non-allowlisted groups may trigger via direct @mention."""
        configured = self.config.extra.get("guest_mode")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_GUEST_MODE", "false").lower() in {"true", "1", "yes", "on"}


    def _telegram_exclusive_bot_mentions(self) -> bool:
        """Return whether explicit @...bot mentions exclusively route group messages."""
        configured = self.config.extra.get("exclusive_bot_mentions")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS", "true").lower() in {"true", "1", "yes", "on"}


    def _telegram_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_FREE_RESPONSE_CHATS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}


    def _telegram_free_response_topics(self) -> set[str]:
        """Return topic-level free-response allowlist entries as ``<chat_id>:<thread_id>``.

        Unlike ``free_response_chats`` (whole-chat), each entry opens a single
        forum topic for free-response. A missing/omitted thread id on incoming
        messages is normalized to the General topic (``1``).
        """
        raw = self.config.extra.get("free_response_topics")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_FREE_RESPONSE_TOPICS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}


    def _telegram_is_free_response_topic(self, message: Message) -> bool:
        """True when the message's chat/topic pair is in ``free_response_topics``."""
        topics = self._telegram_free_response_topics()
        if not topics:
            return False
        chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
        if not chat_id:
            return False
        thread_id = self._effective_message_thread_id(message)
        topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
        return f"{chat_id}:{topic_id}" in topics


    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("TELEGRAM_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded

        if patterns is None:
            # Parity with the historical inline implementation: return before
            # evaluating ``self.name`` (tests construct bare adapters via
            # object.__new__ that lack the attributes ``name`` reads).
            return []

        return compile_mention_patterns(
            patterns,
            log_prefix=self.name,
            platform_label="telegram",
            display_label="Telegram",
            logger_=logger,
        )


    def _is_group_chat(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if not chat:
            return False
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        return chat_type in {"group", "supergroup"}


    @classmethod
    def _effective_message_thread_id(cls, message: Message) -> Optional[str]:
        """Return the routable thread id for a Telegram message.

        Forum supergroup messages posted in the General topic arrive with
        ``message_thread_id=None`` while Telegram itself addresses that topic
        as thread id ``1``.  Ordinary replies are the opposite footgun:
        Telegram populates ``message_thread_id`` with a reply-UI anchor id on
        plain group/DM replies, but those ids are not topic/session routing
        ids and must not be treated as such.  Gating, skill binding, and
        outbound routing must all agree on the same normalized value.
        """
        chat = getattr(message, "chat", None)
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower() if chat else ""
        raw = getattr(message, "message_thread_id", None)
        is_topic_message = bool(getattr(message, "is_topic_message", False))
        is_forum_group = chat_type in ("group", "supergroup") and getattr(chat, "is_forum", False) is True
        if raw is not None:
            if is_forum_group or (chat_type in ("group", "supergroup") and is_topic_message):
                return str(raw)
            if chat_type == "private" and is_topic_message:
                return str(raw)
            return None
        if is_forum_group:
            return cls._GENERAL_TOPIC_THREAD_ID
        return None
