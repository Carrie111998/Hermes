"""Seam-identity + aggressive failure-mode tests for TelegramReactionsMixin (shard A6).

``TelegramReactionsMixin`` (plugins/platforms/telegram/telegram_reactions.py) is
the final slice of the Telegram adapter god-file decomposition: forum-command
lazy registration plus the message-reaction processing lifecycle
(``_reactions_enabled`` / ``_set_reaction`` / ``_clear_reactions`` /
``on_processing_start`` / ``on_processing_complete``).

The seam-identity tests pin the regression this extraction is meant to prevent:
``TelegramAdapter`` must resolve every moved method to the *same function
object* as the mixin (``getattr(TelegramAdapter, name) is
getattr(TelegramReactionsMixin, name)``) — a duplicated/copied method would
silently diverge.  The aggressive tests then exercise the failure modes the
feature must survive: reactions disabled (no-op), no bot attached, bad IDs,
Bot API errors, non-forum chats, duplicate registration, and swallowed
registration failures.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


def _make_adapter(**extra_env):
    """Build a TelegramAdapter without running __init__ (existing test pattern)."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token")
    adapter._bot = AsyncMock()
    adapter._bot.set_message_reaction = AsyncMock()
    adapter._forum_command_registered = set()
    adapter._forum_lock = asyncio.Lock()
    return adapter


def _make_event(chat_id: str = "123", message_id: str = "456") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="private",
            user_id="42",
            user_name="TestUser",
        ),
        message_id=message_id,
    )


# ── Seam identity (the extraction regression) ────────────────────────────

_MOVED_METHODS = [
    "_ensure_forum_commands",
    "_reactions_enabled",
    "_set_reaction",
    "_clear_reactions",
    "on_processing_start",
    "on_processing_complete",
]


@pytest.mark.parametrize("name", _MOVED_METHODS)
def test_seam_identity_moved_methods_resolve_to_mixin(name):
    """getattr(TelegramAdapter, name) is getattr(TelegramReactionsMixin, name).

    The whole point of the extraction: the adapter must expose the *same*
    function objects the mixin defines.  A copy-paste divergence would break
    this identity and let the two sides drift.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.telegram.telegram_reactions import TelegramReactionsMixin

    adapter_attr = getattr(TelegramAdapter, name)
    mixin_attr = getattr(TelegramReactionsMixin, name)
    # Unwrap classmethod/staticmethod bindings if a future slice introduces them.
    adapter_fn = getattr(adapter_attr, "__func__", adapter_attr)
    mixin_fn = getattr(mixin_attr, "__func__", mixin_attr)

    assert adapter_fn is mixin_fn


def test_seam_identity_mixin_sits_ahead_of_base_in_mro():
    """The mixin must be in the MRO ahead of the base so its hooks win."""
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.telegram.telegram_reactions import TelegramReactionsMixin

    mro = TelegramAdapter.__mro__
    assert TelegramReactionsMixin in mro
    from gateway.platforms.base import BasePlatformAdapter

    assert mro.index(TelegramReactionsMixin) < mro.index(BasePlatformAdapter)


# ── _reactions_enabled: gate parsing failure modes ───────────────────────


@pytest.mark.parametrize("value", ["0", "no", "FALSE", "False", "nO"])
def test_reactions_enabled_falsy_variants(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_REACTIONS", value)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES"])
def test_reactions_enabled_truthy_variants(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_REACTIONS", value)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is True


def test_reactions_enabled_absent_env_is_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


# ── on_processing_start: failure modes ───────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_start_disabled_is_noop(monkeypatch):
    """Reactions disabled -> the hook must not touch the bot at all."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    await adapter.on_processing_start(_make_event())
    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_start_happy_path_sets_eyes(monkeypatch):
    """Enabled + complete event -> 👀 (U+1F440) in-progress reaction."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    await adapter.on_processing_start(_make_event())
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )


@pytest.mark.asyncio
async def test_on_processing_start_without_bot_does_not_raise(monkeypatch):
    """No bot attached (e.g. pre-startup) -> _set_reaction no-ops silently."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot = None
    await adapter.on_processing_start(_make_event())  # must not raise


@pytest.mark.asyncio
async def test_on_processing_start_source_is_none_does_not_raise(monkeypatch):
    """Degenerate event with source=None -> getattr chain yields None -> no-op."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()
    event.source = None
    await adapter.on_processing_start(event)  # must not raise
    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_start_missing_message_id_does_not_call_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()
    event.message_id = None
    await adapter.on_processing_start(event)
    adapter._bot.set_message_reaction.assert_not_awaited()


# ── on_processing_complete: failure modes ────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_complete_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_complete_success_sets_thumbs_up(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    await adapter.on_processing_complete(_make_event(), ProcessingOutcome.SUCCESS)
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f44d",
    )


@pytest.mark.asyncio
async def test_on_processing_complete_failure_sets_thumbs_down(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    await adapter.on_processing_complete(_make_event(), ProcessingOutcome.FAILURE)
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f44e",
    )


@pytest.mark.asyncio
async def test_on_processing_complete_missing_ids_is_noop(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()
    event.message_id = None
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_complete_without_bot_does_not_raise(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot = None
    await adapter.on_processing_complete(_make_event(), ProcessingOutcome.CANCELLED)
    # must not raise


# ── _set_reaction / _clear_reactions: send failure modes ─────────────────


@pytest.mark.asyncio
async def test_set_reaction_without_bot_returns_false():
    adapter = _make_adapter()
    adapter._bot = None
    assert await adapter._set_reaction("123", "456", "\U0001f440") is False


@pytest.mark.asyncio
async def test_clear_reactions_without_bot_returns_false():
    adapter = _make_adapter()
    adapter._bot = None
    assert await adapter._clear_reactions("123", "456") is False


@pytest.mark.asyncio
async def test_set_reaction_malformed_message_id_returns_false():
    """int(message_id) raising must be swallowed into a False result, not thrown."""
    adapter = _make_adapter()
    assert await adapter._set_reaction("123", "not-an-int", "\U0001f440") is False


@pytest.mark.asyncio
async def test_clear_reactions_malformed_message_id_returns_false():
    """int(message_id) raising must be swallowed into a False result, not thrown.

    (chat_id is never malformed here: ``normalize_telegram_chat_id`` returns
    usernames as-is instead of raising, so the message_id conversion is the
    only int() that can fail.)
    """
    adapter = _make_adapter()
    assert await adapter._clear_reactions("123", "not-an-int") is False


@pytest.mark.asyncio
async def test_set_reaction_api_error_is_swallowed(monkeypatch):
    """Bot API errors must be downgraded to False + debug log, never raised."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("flood"))
    assert await adapter._set_reaction("123", "456", "\U0001f440") is False


# ── _ensure_forum_commands: registration failure modes ───────────────────


def _forum_message(chat_id=-100, is_forum=True):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )


@pytest.mark.asyncio
async def test_ensure_forum_commands_non_forum_chat_is_noop():
    adapter = _make_adapter()
    await adapter._ensure_forum_commands(_forum_message(chat_id=-100, is_forum=False))
    adapter._bot.set_my_commands.assert_not_awaited()
    assert adapter._forum_command_registered == set()


@pytest.mark.asyncio
async def test_ensure_forum_commands_message_without_chat_is_noop():
    adapter = _make_adapter()
    await adapter._ensure_forum_commands(SimpleNamespace())
    adapter._bot.set_my_commands.assert_not_awaited()
    assert adapter._forum_command_registered == set()


@pytest.mark.asyncio
async def test_ensure_forum_commands_already_registered_skips():
    """A chat already in _forum_command_registered must not re-register."""
    adapter = _make_adapter()
    adapter._forum_command_registered = {-555}
    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session")], 0)
        with patch("telegram.BotCommand"), patch("telegram.BotCommandScopeChat"):
            await adapter._ensure_forum_commands(_forum_message(chat_id=-555))
    adapter._bot.set_my_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_forum_commands_failure_is_swallowed(caplog):
    """A failing menu/registration path must log a warning and never raise."""
    adapter = _make_adapter()
    with patch(
        "hermes_cli.commands.telegram_menu_commands",
        side_effect=RuntimeError("menu broken"),
    ):
        await adapter._ensure_forum_commands(_forum_message(chat_id=-321))
    # The chat must NOT be marked registered on failure...
    assert adapter._forum_command_registered == set()
    # ...and the failure was surfaced through the adapter logger (redacted).
    assert any("Forum command lazy-registration failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_ensure_forum_commands_uses_monkeypatched_adapter_redaction(caplog, monkeypatch):
    """The lazy adapter import must observe runtime monkeypatches of adapter._redact_telegram_error_text.

    The polling/rich/messaging mixins promise that monkeypatching
    ``plugins.platforms.telegram.adapter._redact_telegram_error_text`` keeps
    working after the slice; this pins the same contract for the reactions
    mixin's lazy import.
    """
    adapter = _make_adapter()

    def fake_redact(error: object) -> str:
        return "REDACTED-SENTINEL"

    from plugins.platforms.telegram import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "_redact_telegram_error_text", fake_redact)
    with patch(
        "hermes_cli.commands.telegram_menu_commands",
        side_effect=RuntimeError("boom-secret-token"),
    ):
        await adapter._ensure_forum_commands(_forum_message(chat_id=-321))

    assert any("REDACTED-SENTINEL" in r.message for r in caplog.records)
    assert not any("boom-secret-token" in r.message for r in caplog.records)
