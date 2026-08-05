"""DM forum-topic methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift that
produced ``gateway/authz_mixin.py``. This mixin owns the private-DM forum
topic lifecycle: creating topics (Bot API 9.4 ``createForumTopic`` on 1-on-1
chats), resolving/ensuring them from config or cache, renaming, persisting
``thread_id`` back into ``config.yaml``, and seeding the topic with a message
so it is visible in Telegram's client.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO, and ``DmTopicsMixin`` precedes
``BasePlatformAdapter`` in the bases so resolution order is what it was when
these methods sat on the class.

``logger`` is bound by explicit name rather than ``__name__``, so records
emitted from these methods keep the logger name
``"plugins.platforms.telegram.adapter"``. ``normalize_telegram_chat_id`` is
re-imported from its home module, exactly as the adapter imports it.
"""

import logging
from typing import Any, Dict, Optional

from plugins.platforms.telegram.adapter import _redact_telegram_error_text
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

# Bind the adapter's logger by name so log records lifted with these methods
# are emitted under exactly the name they were before.
logger = logging.getLogger("plugins.platforms.telegram.adapter")


class DmTopicsMixin:
    """DM forum-topic cluster lifted verbatim from ``TelegramAdapter``."""

    async def _create_dm_topic(
        self,
        chat_id: int,
        name: str,
        icon_color: Optional[int] = None,
        icon_custom_emoji_id: Optional[str] = None,
    ) -> Optional[int]:
        """Create a forum topic in a private (DM) chat.

        Uses Bot API 9.4's createForumTopic which now works for 1-on-1 chats.
        Returns the message_thread_id on success, None on failure.
        """
        if not self._bot:
            return None
        try:
            kwargs: Dict[str, Any] = {"chat_id": chat_id, "name": name}
            if icon_color is not None:
                kwargs["icon_color"] = icon_color
            if icon_custom_emoji_id:
                kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id

            topic = await self._bot.create_forum_topic(**kwargs)
            thread_id = topic.message_thread_id
            logger.info(
                "[%s] Created DM topic '%s' in chat %s -> thread_id=%s",
                self.name, name, chat_id, thread_id,
            )
            return thread_id
        except Exception as e:
            error_text = str(e).lower()
            # If topic already exists, try to find it via getForumTopicIconStickers
            # or we just log and skip — Telegram doesn't provide a "list topics" API
            if "topic_name_duplicate" in error_text or "already" in error_text:
                logger.info(
                    "[%s] DM topic '%s' already exists in chat %s (will be mapped from incoming messages)",
                    self.name, name, chat_id,
                )
            elif "not a forum" in error_text or "forums_disabled" in error_text:
                logger.warning(
                    "[%s] Cannot create DM topic '%s' in chat %s: Topics mode is not enabled. "
                    "The user must open the DM with this bot in Telegram, tap the bot name "
                    "at the top, and enable 'Topics' in chat settings before topics can be created.",
                    self.name, name, chat_id,
                )
            else:
                logger.warning(
                    "[%s] Failed to create DM topic '%s' in chat %s: %s",
                    self.name, name, chat_id, _redact_telegram_error_text(e),
                )
            return None

    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """Create a forum topic for a session handoff.

        Works for DM topics (Bot API 9.4+, requires user to enable Topics
        in their chat with the bot) and forum supergroups. Returns the
        ``message_thread_id`` as a string, or ``None`` on failure.
        """
        try:
            chat_id_int = int(parent_chat_id)
        except (TypeError, ValueError):
            return None
        thread_id = await self._create_dm_topic(chat_id_int, name=name)
        return str(thread_id) if thread_id else None

    async def ensure_dm_topic(self, chat_id: str, topic_name: str, force_create: bool = False) -> Optional[str]:
        """Return a private DM topic thread id, creating and persisting it if needed."""
        name = str(topic_name or "").strip()
        if not name:
            return None
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None

        cache_key = f"{chat_id_int}:{name}"
        cached = self._dm_topics.get(cache_key)
        if cached and not force_create:
            return str(cached)

        topic_conf: Optional[Dict[str, Any]] = None
        chat_entry: Optional[Dict[str, Any]] = None
        for entry in self._dm_topics_config:
            if str(entry.get("chat_id")) != str(chat_id_int):
                continue
            chat_entry = entry
            for candidate in entry.get("topics", []):
                if candidate.get("name") == name:
                    topic_conf = candidate
                    break
            break

        if topic_conf and topic_conf.get("thread_id") and not force_create:
            thread_id = int(topic_conf["thread_id"])
            self._dm_topics[cache_key] = thread_id
            return str(thread_id)

        if chat_entry is None:
            chat_entry = {"chat_id": chat_id_int, "topics": []}
            self._dm_topics_config.append(chat_entry)
        if topic_conf is None:
            topic_conf = {"name": name}
            chat_entry.setdefault("topics", []).append(topic_conf)

        thread_id = await self._create_dm_topic(
            chat_id_int,
            name=name,
            icon_color=topic_conf.get("icon_color"),
            icon_custom_emoji_id=topic_conf.get("icon_custom_emoji_id"),
        )
        if not thread_id:
            return None

        topic_conf["thread_id"] = thread_id
        self._dm_topics[cache_key] = int(thread_id)
        self._persist_dm_topic_thread_id(chat_id_int, name, int(thread_id), replace_existing=force_create)
        return str(thread_id)

    async def rename_dm_topic(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
    ) -> None:
        """Rename a forum topic in a private (DM) chat."""
        if not self._bot:
            return
        try:
            chat_id_arg = int(chat_id)
        except (TypeError, ValueError):
            chat_id_arg = chat_id
        await self._bot.edit_forum_topic(
            chat_id=chat_id_arg,
            message_thread_id=int(thread_id),
            name=name,
        )
        logger.info(
            "[%s] Renamed DM topic in chat %s thread_id=%s -> '%s'",
            self.name, chat_id, thread_id, name,
        )

    def _persist_dm_topic_thread_id(
        self,
        chat_id: int,
        topic_name: str,
        thread_id: int,
        replace_existing: bool = False,
    ) -> None:
        """Save a newly created thread_id back into config.yaml so it persists across restarts."""
        try:
            from hermes_constants import get_hermes_home
            config_path = get_hermes_home() / "config.yaml"
            if not config_path.exists():
                logger.warning("[%s] Config file not found at %s, cannot persist thread_id", self.name, config_path)
                return

            import yaml as _yaml
            with open(config_path, "r", encoding="utf-8") as f:
                config = _yaml.safe_load(f) or {}

            # Navigate to platforms.telegram.extra.dm_topics, creating the path
            # when a named delivery target asks us to create a topic that was
            # not predeclared in config.yaml.
            platforms = config.setdefault("platforms", {})
            telegram_config = platforms.setdefault("telegram", {})
            extra = telegram_config.setdefault("extra", {})
            dm_topics = extra.setdefault("dm_topics", [])

            changed = False
            matching_chat_entry = None
            for chat_entry in dm_topics:
                try:
                    chat_matches = int(chat_entry.get("chat_id", 0)) == int(chat_id)
                except (TypeError, ValueError):
                    chat_matches = False
                if not chat_matches:
                    continue
                matching_chat_entry = chat_entry
                for t in chat_entry.setdefault("topics", []):
                    if t.get("name") == topic_name:
                        if replace_existing or not t.get("thread_id"):
                            if t.get("thread_id") != thread_id:
                                t["thread_id"] = thread_id
                                changed = True
                        break
                else:
                    chat_entry.setdefault("topics", []).append(
                        {"name": topic_name, "thread_id": thread_id}
                    )
                    changed = True
                break

            if matching_chat_entry is None:
                dm_topics.append({
                    "chat_id": chat_id,
                    "topics": [{"name": topic_name, "thread_id": thread_id}],
                })
                changed = True

            if changed:
                from hermes_cli.config import atomic_config_write

                atomic_config_write(
                    config_path,
                    config,
                    default_flow_style=False,
                    sort_keys=False,
                )
                logger.info(
                    "[%s] Persisted thread_id=%s for topic '%s' in config.yaml",
                    self.name, thread_id, topic_name,
                )
        except Exception as e:
            logger.warning("[%s] Failed to persist thread_id to config: %s", self.name, e, exc_info=True)

    async def _setup_dm_topics(self) -> None:
        """Load or create configured DM topics for specified chats.

        Reads config.extra['dm_topics'] — a list of dicts:
        [
            {
                "chat_id": 123456789,
                "topics": [
                    {"name": "General", "icon_color": 7322096, "thread_id": 100},
                    {"name": "Accessibility Auditor", "icon_color": 9367192, "skill": "accessibility-auditor"}
                ]
            }
        ]

        If a topic already has a thread_id in the config (persisted from a previous
        creation), it is loaded into the cache without calling createForumTopic.
        Only topics without a thread_id are created via the API, and their thread_id
        is then saved back to config.yaml for future restarts.
        """
        if not self._dm_topics_config:
            return

        for chat_entry in self._dm_topics_config:
            chat_id = chat_entry.get("chat_id")
            topics = chat_entry.get("topics", [])
            if not chat_id or not topics:
                continue

            logger.info(
                "[%s] Setting up %d DM topic(s) for chat %s",
                self.name, len(topics), chat_id,
            )

            for topic_conf in topics:
                topic_name = topic_conf.get("name")
                if not topic_name:
                    continue

                cache_key = f"{chat_id}:{topic_name}"

                # If thread_id is already persisted in config, just load into cache
                existing_thread_id = topic_conf.get("thread_id")
                if existing_thread_id:
                    self._dm_topics[cache_key] = int(existing_thread_id)
                    logger.info(
                        "[%s] DM topic loaded from config: %s -> thread_id=%s",
                        self.name, cache_key, existing_thread_id,
                    )
                    continue

                # No persisted thread_id — create the topic via API
                icon_color = topic_conf.get("icon_color")
                icon_emoji = topic_conf.get("icon_custom_emoji_id")

                thread_id = await self._create_dm_topic(
                    chat_id=normalize_telegram_chat_id(chat_id),
                    name=topic_name,
                    icon_color=icon_color,
                    icon_custom_emoji_id=icon_emoji,
                )

                if thread_id:
                    self._dm_topics[cache_key] = thread_id
                    logger.info(
                        "[%s] DM topic cached: %s -> thread_id=%s",
                        self.name, cache_key, thread_id,
                    )
                    # Persist thread_id to config so we don't recreate on next restart
                    self._persist_dm_topic_thread_id(int(chat_id), topic_name, thread_id)

                    # Send a seed message so the topic is visible in Telegram's client.
                    # Empty topics are hidden by the client UI until they contain a message.
                    try:
                        await self._bot.send_message(
                            chat_id=normalize_telegram_chat_id(chat_id),
                            message_thread_id=thread_id,
                            text=f"\U0001f4cc {topic_name}",
                        )
                    except Exception as seed_err:
                        logger.debug(
                            "[%s] Could not send seed message to topic '%s': %s",
                            self.name, topic_name, seed_err,
                        )
