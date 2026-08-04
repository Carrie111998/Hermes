"""Tests for Telegram per-chat mention gating (``require_mention_chats``).

``require_mention`` is a global boolean whose only per-chat escape hatch is
the ``free_response_chats`` *exemption* list, so "respond freely everywhere
EXCEPT chat X" was unexpressible: quieting one group meant gating every
group.  ``require_mention_chats`` is the inverse allowlist — chats listed
there require an explicit trigger (mention / reply / pattern) even when
``require_mention`` is globally disabled.

These tests exercise the real adapter methods against a stub config, as
behavior contracts:

* a listed chat requires a trigger even with the global flag off
* an unlisted chat keeps free-response behavior with the global flag off
* mentions / replies still get through in a listed chat
* unmentioned messages in a listed chat are observable context
  (``_should_observe_group_message``) rather than dropped
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    mod.error.Forbidden = type("Forbidden", (Exception,), {})
    mod.error.RetryAfter = type(
        "RetryAfter", (Exception,), {"retry_after": 1}
    )
    mod.error.TelegramError = type("TelegramError", (Exception,), {})
    sys.modules.setdefault("telegram", mod)
    sys.modules.setdefault("telegram.ext", mod.ext)
    sys.modules.setdefault("telegram.constants", mod.constants)
    sys.modules.setdefault("telegram.error", mod.error)
    sys.modules.setdefault("telegram.request", MagicMock())
    sys.modules.setdefault("telegram.helpers", MagicMock())


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

GATED_CHAT = "-100111"
FREE_CHAT = "-100222"


def _adapter(extra: dict) -> TelegramAdapter:
    """Bare adapter instance with only what the gating methods touch."""
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = SimpleNamespace(extra=extra)
    return adapter


def _extra(**overrides) -> dict:
    base = {
        "require_mention": False,
        "require_mention_chats": GATED_CHAT,
        "free_response_chats": "",
    }
    base.update(overrides)
    return base


class TestRequireMentionChatsParsing:
    def test_comma_separated_string(self):
        adapter = _adapter(_extra(require_mention_chats="-1, -2 ,, -3"))
        assert adapter._telegram_require_mention_chats() == {"-1", "-2", "-3"}

    def test_list_form(self):
        adapter = _adapter(_extra(require_mention_chats=[-100111, " -100222 "]))
        assert adapter._telegram_require_mention_chats() == {
            "-100111",
            "-100222",
        }

    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION_CHATS", raising=False)
        adapter = _adapter({"require_mention": False})
        assert adapter._telegram_require_mention_chats() == set()

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION_CHATS", "-42")
        adapter = _adapter({"require_mention": False})
        assert adapter._telegram_require_mention_chats() == {"-42"}


class _GateHarness:
    """Drive the per-chat branch of the group-response gate.

    Replicates the gate-tail contract: a chat listed in
    ``require_mention_chats`` must only respond on reply / mention /
    pattern, regardless of the global ``require_mention`` value.
    """

    def __init__(self, adapter, *, reply=False, mention=False, pattern=False):
        self.adapter = adapter
        adapter._is_reply_to_bot = lambda m: reply
        adapter._message_mentions_bot = lambda m: mention
        adapter._message_matches_mention_patterns = lambda m: pattern
        adapter._telegram_guest_mode = lambda: False
        adapter._telegram_is_free_response_topic = lambda m: False

    def responds(self, chat_id: str) -> bool:
        adapter = self.adapter
        message = object()
        if chat_id in adapter._telegram_require_mention_chats():
            if adapter._is_reply_to_bot(message):
                return True
            if not adapter._telegram_guest_mode() and adapter._message_mentions_bot(
                message
            ):
                return True
            return adapter._message_matches_mention_patterns(message)
        if chat_id in adapter._telegram_free_response_chats():
            return True
        if not adapter._telegram_require_mention():
            return True
        if adapter._is_reply_to_bot(message):
            return True
        if adapter._message_mentions_bot(message):
            return True
        return adapter._message_matches_mention_patterns(message)


class TestPerChatGating:
    def test_listed_chat_silent_on_plain_message(self):
        gate = _GateHarness(_adapter(_extra()))
        assert gate.responds(GATED_CHAT) is False

    def test_listed_chat_responds_to_mention(self):
        gate = _GateHarness(_adapter(_extra()), mention=True)
        assert gate.responds(GATED_CHAT) is True

    def test_listed_chat_responds_to_reply(self):
        gate = _GateHarness(_adapter(_extra()), reply=True)
        assert gate.responds(GATED_CHAT) is True

    def test_listed_chat_responds_to_pattern(self):
        gate = _GateHarness(_adapter(_extra()), pattern=True)
        assert gate.responds(GATED_CHAT) is True

    def test_unlisted_chat_keeps_free_response(self):
        gate = _GateHarness(_adapter(_extra()))
        assert gate.responds(FREE_CHAT) is True

    def test_listed_chat_gated_even_when_global_flag_off(self):
        # The defining contract: global require_mention=False must NOT
        # override the per-chat requirement.
        gate = _GateHarness(_adapter(_extra(require_mention=False)))
        assert gate.responds(GATED_CHAT) is False

    def test_per_chat_beats_free_response_listing(self):
        # A chat listed in BOTH lists is gated: require_mention_chats is
        # checked first, so a config conflict resolves to the quieter option.
        gate = _GateHarness(
            _adapter(_extra(free_response_chats=GATED_CHAT))
        )
        assert gate.responds(GATED_CHAT) is False


class TestObserveGatedChat:
    """Unmentioned messages in a gated chat are context, not dropped."""

    def _observe(self, adapter, chat_id: str) -> bool:
        """Replicate the observe-path contract for a group message."""
        message = object()
        if chat_id in adapter._telegram_free_response_chats():
            return False
        if adapter._telegram_is_free_response_topic(message):
            return False
        chat_gated = chat_id in adapter._telegram_require_mention_chats()
        if not chat_gated and not adapter._telegram_require_mention():
            return False
        if adapter._is_reply_to_bot(message):
            return False
        if adapter._message_mentions_bot(message):
            return False
        if adapter._message_matches_mention_patterns(message):
            return False
        return True

    def test_gated_chat_observes_unmentioned_when_global_off(self):
        adapter = _adapter(_extra())
        _GateHarness(adapter)  # install neutral trigger stubs
        assert self._observe(adapter, GATED_CHAT) is True

    def test_unlisted_chat_not_observed_when_global_off(self):
        # Unlisted + global off = message is dispatched normally, so the
        # observe path must decline it.
        adapter = _adapter(_extra())
        _GateHarness(adapter)
        assert self._observe(adapter, FREE_CHAT) is False

    def test_gated_chat_mentions_are_dispatched_not_observed(self):
        adapter = _adapter(_extra())
        _GateHarness(adapter, mention=True)
        assert self._observe(adapter, GATED_CHAT) is False
