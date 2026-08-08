"""Regression tests for the group-gating cluster extracted to
``GroupGatingMixin`` (shard s4 of the adapter god-file decomposition).

These cover the PURE helpers that moved verbatim from ``TelegramAdapter``:
config/env parsing (``require_mention``, guest mode, exclusive bot mentions,
free-response chats/topics), the ``_scoped_gate_env`` env reader, mention
pattern compilation, group-chat classification and thread-id normalization.
Bare adapters are constructed via ``object.__new__`` + stub config, matching
the seam documented on ``_compile_mention_patterns``.
"""

from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _scoped_gate_env as adapter_scoped_gate_env,
)
from plugins.platforms.telegram.group_gating_mixin import (
    GroupGatingMixin,
    _scoped_gate_env,
)


def _make_adapter(**extra):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    return adapter


def _group_message(*, thread_id=None, chat_type="supergroup", is_forum=None,
                   is_topic=None):
    if is_forum is None:
        is_forum = thread_id is not None
    if is_topic is None:
        is_topic = thread_id is not None
    return SimpleNamespace(
        message_thread_id=thread_id,
        is_topic_message=is_topic,
        chat=SimpleNamespace(id=-100, type=chat_type, is_forum=is_forum),
    )


# ---------------------------------------------------------------------------
# MRO wiring
# ---------------------------------------------------------------------------

def test_group_gating_mixin_wired_into_adapter_mro():
    adapter = _make_adapter()
    assert isinstance(adapter, GroupGatingMixin)
    # The 5 allowlist readers stay on the adapter (lifted in PR #75742)
    for name in ("_telegram_allowed_chats", "_telegram_allowed_topics",
                 "_telegram_group_allowed_chats", "_telegram_ignored_threads",
                 "_telegram_observe_allowed_chats"):
        assert hasattr(adapter, name), name


def test_scoped_gate_env_is_the_same_function_reexported_by_adapter():
    assert _scoped_gate_env is adapter_scoped_gate_env


# ---------------------------------------------------------------------------
# Boolean config gates
# ---------------------------------------------------------------------------

def test_require_mention_from_config_and_env(monkeypatch):
    assert _make_adapter(require_mention=True)._telegram_require_mention() is True
    assert _make_adapter(require_mention="on")._telegram_require_mention() is True
    assert _make_adapter(require_mention=False)._telegram_require_mention() is False
    monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "yes")
    assert _make_adapter()._telegram_require_mention() is True
    monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION")
    assert _make_adapter()._telegram_require_mention() is False


def test_guest_mode_defaults_off_and_env_override(monkeypatch):
    # Hermetic: ambient TELEGRAM_GUEST_MODE (e.g. from a developer .env
    # loaded by another test's import chain) must not flip the default.
    monkeypatch.delenv("TELEGRAM_GUEST_MODE", raising=False)
    assert _make_adapter()._telegram_guest_mode() is False
    assert _make_adapter(guest_mode="1")._telegram_guest_mode() is True
    monkeypatch.setenv("TELEGRAM_GUEST_MODE", "true")
    assert _make_adapter()._telegram_guest_mode() is True


def test_exclusive_bot_mentions_defaults_true():
    assert _make_adapter()._telegram_exclusive_bot_mentions() is True
    assert _make_adapter(exclusive_bot_mentions=False)._telegram_exclusive_bot_mentions() is False


def test_observe_unmentioned_group_messages_config_fallback_key(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)
    assert _make_adapter(observe_unmentioned_group_messages=True)._telegram_observe_unmentioned_group_messages() is True
    # legacy key
    assert _make_adapter(ingest_unmentioned_group_messages=True)._telegram_observe_unmentioned_group_messages() is True
    assert _make_adapter()._telegram_observe_unmentioned_group_messages() is False


# ---------------------------------------------------------------------------
# Free-response parsing
# ---------------------------------------------------------------------------

def test_free_response_chats_config_list_and_env_csv(monkeypatch):
    adapter = _make_adapter(free_response_chats=["-100", "-200"])
    assert adapter._telegram_free_response_chats() == {"-100", "-200"}
    monkeypatch.setenv("TELEGRAM_FREE_RESPONSE_CHATS", "-100, -300")
    assert _make_adapter()._telegram_free_response_chats() == {"-100", "-300"}


def test_free_response_topics_and_is_free_response_topic():
    adapter = _make_adapter(free_response_topics=["-100:1", "-100:7"])
    assert adapter._telegram_free_response_topics() == {"-100:1", "-100:7"}
    # General topic: message_thread_id None in a forum -> normalized to "1"
    assert adapter._telegram_is_free_response_topic(
        _group_message(thread_id=None, is_forum=True)) is True
    assert adapter._telegram_is_free_response_topic(
        _group_message(thread_id=7)) is True
    assert adapter._telegram_is_free_response_topic(
        _group_message(thread_id=8)) is False
    # DM / chatless messages are never free-response topics
    assert adapter._telegram_is_free_response_topic(
        SimpleNamespace(message_thread_id=None, is_topic_message=False,
                        chat=SimpleNamespace(id=123, type="private", is_forum=False))) is False


# ---------------------------------------------------------------------------
# Mention patterns
# ---------------------------------------------------------------------------

def test_compile_mention_patterns_config_list():
    adapter = _make_adapter(mention_patterns=["\\bhermes\\b", "hey bot"])
    patterns = adapter._compile_mention_patterns()
    assert len(patterns) == 2
    assert patterns[0].search("call hermes now")
    assert patterns[1].search("hey bot")


def test_compile_mention_patterns_env_json(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MENTION_PATTERNS", '["\\\\bhey\\\\b"]')
    patterns = _make_adapter()._compile_mention_patterns()
    assert len(patterns) == 1
    assert patterns[0].search("hey there")


def test_compile_mention_patterns_empty_returns_empty_list(monkeypatch):
    # documented test seam: adapter with an empty config (no mention_patterns
    # key, no env var) returns [] before touching self.name
    monkeypatch.delenv("TELEGRAM_MENTION_PATTERNS", raising=False)
    assert _make_adapter()._compile_mention_patterns() == []


# ---------------------------------------------------------------------------
# Group classification + thread id normalization
# ---------------------------------------------------------------------------

def test_is_group_chat_classification():
    adapter = _make_adapter()
    assert adapter._is_group_chat(_group_message(chat_type="group")) is True
    assert adapter._is_group_chat(_group_message(chat_type="supergroup")) is True
    assert adapter._is_group_chat(_group_message(chat_type="private")) is False
    assert adapter._is_group_chat(_group_message(chat_type="channel")) is False
    assert adapter._is_group_chat(SimpleNamespace(chat=None)) is False


def test_effective_message_thread_id_normalization():
    adapter = _make_adapter()
    # Forum General topic: None -> "1"
    assert adapter._effective_message_thread_id(
        _group_message(thread_id=None, is_forum=True)) == "1"
    # Forum topic with explicit id
    assert adapter._effective_message_thread_id(
        _group_message(thread_id=42, is_forum=True)) == "42"
    # Supergroup topic message (is_topic_message) with explicit id
    assert adapter._effective_message_thread_id(
        _group_message(thread_id=7)) == "7"
    # Plain group reply anchor is NOT a routable topic id (no topic flag)
    assert adapter._effective_message_thread_id(
        _group_message(thread_id=7, is_forum=False, chat_type="group",
                       is_topic=False)) is None
    # Private DM-topic lane keeps its topic id
    dm = SimpleNamespace(
        message_thread_id=9,
        is_topic_message=True,
        chat=SimpleNamespace(id=123, type="private", is_forum=False),
    )
    assert adapter._effective_message_thread_id(dm) == "9"
    # Private DM without topic
    dm_plain = SimpleNamespace(
        message_thread_id=None, is_topic_message=False,
        chat=SimpleNamespace(id=123, type="private", is_forum=False),
    )
    assert adapter._effective_message_thread_id(dm_plain) is None


# ---------------------------------------------------------------------------
# _scoped_gate_env
# ---------------------------------------------------------------------------

def test_scoped_gate_env_reads_and_strips_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_S4_TEST_GATE", "  a, b  ")
    assert _scoped_gate_env("TELEGRAM_S4_TEST_GATE") == "a, b"
    monkeypatch.delenv("TELEGRAM_S4_TEST_GATE")
    assert _scoped_gate_env("TELEGRAM_S4_TEST_GATE") == ""
    assert _scoped_gate_env("TELEGRAM_S4_TEST_GATE", "fallback") == "fallback"


def test_scoped_gate_env_used_by_free_response_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_FREE_RESPONSE_CHATS", "-77")
    assert _make_adapter()._telegram_free_response_chats() == {"-77"}
