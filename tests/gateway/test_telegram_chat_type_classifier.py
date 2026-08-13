"""Tests for TelegramAdapter._classify_telegram_chat_type.

Pins each call site's current forum semantics so the helper preserves
intent rather than collapsing to one predicate.
"""

import sys
from pathlib import Path

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# tests/gateway/conftest.py auto-loads and installs the telegram mock.

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


class TestClassifyTelegramChatType:
    """The shared helper must produce the same result each call site had inline."""

    # --- callback-auth semantics (thread_id only, is_forum=thread_id is not None) ---

    def test_callback_private_to_dm(self):
        assert TelegramAdapter._classify_telegram_chat_type("private") == "dm"

    def test_callback_supergroup_no_thread_is_group(self):
        assert TelegramAdapter._classify_telegram_chat_type("supergroup") == "group"

    def test_callback_supergroup_with_thread_is_forum(self):
        assert TelegramAdapter._classify_telegram_chat_type("supergroup", thread_id=42, is_forum=True) == "forum"

    def test_callback_group_type_no_thread_is_group(self):
        assert TelegramAdapter._classify_telegram_chat_type("group") == "group"

    def test_callback_group_type_with_thread_is_forum(self):
        """The helper treats group and supergroup identically (both can be
        forum-capable). This matches the post-refactor unified behavior."""
        assert TelegramAdapter._classify_telegram_chat_type("group", thread_id=42, is_forum=True) == "forum"

    # --- message-auth semantics (thread_id + is_topic/is_forum) ---

    def test_message_supergroup_thread_is_topic_is_forum(self):
        assert (
            TelegramAdapter._classify_telegram_chat_type(
                "supergroup", thread_id=42, is_topic_message=True
            )
            == "forum"
        )

    def test_message_supergroup_thread_not_topic_not_forum_is_group(self):
        assert (
            TelegramAdapter._classify_telegram_chat_type(
                "supergroup", thread_id=42, is_topic_message=False, is_forum=False
            )
            == "group"
        )

    def test_message_supergroup_thread_is_forum_no_topic(self):
        assert (
            TelegramAdapter._classify_telegram_chat_type(
                "supergroup", thread_id=42, is_forum=True
            )
            == "forum"
        )

    # --- reaction-auth semantics (is_forum only, no thread_id) ---

    def test_reaction_supergroup_is_forum(self):
        assert (
            TelegramAdapter._classify_telegram_chat_type("supergroup", is_forum=True)
            == "forum"
        )

    def test_reaction_supergroup_not_forum(self):
        assert (
            TelegramAdapter._classify_telegram_chat_type("supergroup", is_forum=False)
            == "group"
        )

    # --- channel ---

    def test_channel(self):
        assert TelegramAdapter._classify_telegram_chat_type("channel") == "channel"

    # --- edge cases ---

    def test_empty_string_defaults_to_dm(self):
        assert TelegramAdapter._classify_telegram_chat_type("") == "dm"

    def test_none_defaults_to_dm(self):
        assert TelegramAdapter._classify_telegram_chat_type(None) == "dm"

    def test_uppercase_normalized(self):
        assert TelegramAdapter._classify_telegram_chat_type("SUPERGROUP", is_forum=True) == "forum"
