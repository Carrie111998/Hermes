"""Seam-identity regression for the config/mention/identity mixin slice (A5).

The adapter god-file extraction moved the config-getter, mention-gating, and
bot-identity methods into ``TelegramConfigMentionMixin`` and made
``TelegramAdapter`` inherit it.  This test pins the seam: every moved name
must resolve from ``TelegramAdapter`` to the SAME object (or, for
classmethods/staticmethods, the same underlying function) that the mixin
defines, and must NOT be redefined directly on ``TelegramAdapter``.
"""

from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.telegram_config_mention import TelegramConfigMentionMixin

# Every method moved by the A5 slice (config getters, mention machinery,
# bot-identity machinery, group-observe attribution helpers).
_MOVED_METHODS = [
    "format_message",
    "_telegram_require_mention",
    "_telegram_observe_unmentioned_group_messages",
    "_telegram_guest_mode",
    "_telegram_exclusive_bot_mentions",
    "_telegram_free_response_chats",
    "_telegram_free_response_topics",
    "_telegram_is_free_response_topic",
    "_telegram_allowed_chats",
    "_telegram_group_allowed_chats",
    "_telegram_observe_allowed_chats",
    "_telegram_allowed_topics",
    "_telegram_ignored_threads",
    "_compile_mention_patterns",
    "_is_group_chat",
    "_effective_message_thread_id",
    "_current_bot_username",
    "_note_bot_username",
    "_observe_bot_identity_from_message",
    "_bot_identity_is_fresh",
    "_refresh_bot_identity",
    "_is_reply_to_bot",
    "_extract_bot_mention_usernames",
    "_message_mentions_bot",
    "_schedule_bot_identity_recheck",
    "_explicit_bot_mentions_exclude_self",
    "_message_matches_mention_patterns",
    "_is_guest_mention",
    "_clean_bot_trigger_text",
    "_should_observe_unmentioned_group_message",
    "_telegram_group_observe_shared_source",
    "_telegram_group_observe_attributed_text",
    "_telegram_group_observe_channel_prompt",
    "_apply_telegram_group_observe_attribution",
    "_append_observed_note",
]


def test_adapter_inherits_config_mention_mixin():
    assert issubclass(TelegramAdapter, TelegramConfigMentionMixin)


def test_moved_methods_resolve_through_the_mixin_seam():
    """Every moved method resolves to the mixin's definition via MRO."""
    for name in _MOVED_METHODS:
        assert name not in TelegramAdapter.__dict__, (
            f"{name} must not be redefined directly on TelegramAdapter"
        )
        assert name in TelegramConfigMentionMixin.__dict__, (
            f"{name} missing from TelegramConfigMentionMixin"
        )
        mixin_entry = TelegramConfigMentionMixin.__dict__[name]
        resolved = getattr(TelegramAdapter, name)
        # Plain functions: getattr returns the same object.  Classmethods:
        # getattr returns a fresh bound method; staticmethods: getattr
        # unwraps to the plain function.  In both descriptor cases compare
        # against the underlying function — the descriptor itself lives in
        # the mixin's ``__dict__``.
        if isinstance(mixin_entry, classmethod):
            assert resolved.__func__ is mixin_entry.__func__, name
        elif isinstance(mixin_entry, staticmethod):
            assert resolved is mixin_entry.__func__, name
        else:
            assert resolved is mixin_entry, name
