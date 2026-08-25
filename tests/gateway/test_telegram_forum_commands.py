"""Tests for lazy forum command registration in TelegramAdapter."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


def _make_test_adapter():
    """Build a TelegramAdapter without running __init__."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    # ``name`` is a property derived from platform.value.title()
    adapter._bot = MagicMock()
    adapter._bot.set_my_commands = AsyncMock()
    adapter._forum_command_registered = set()
    adapter._forum_lock = asyncio.Lock()
    return adapter


def _forum_message(chat_id=-100, is_forum=True):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )


def _write_quick_commands_only_config(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    extra:\n"
        "      command_menu:\n"
        "        mode: quick_commands_only\n"
        "quick_commands:\n"
        "  agent-health:\n"
        "    type: exec\n"
        "    command: scripts/health.sh\n"
        "    description: Show agent health\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_startup_registers_quick_commands_only_menu(tmp_path, monkeypatch):
    """Startup registers the focused menu for every global Telegram scope."""
    adapter = _make_test_adapter()
    adapter._post_connect_task = None
    adapter._set_status_indicator = AsyncMock()
    adapter._setup_dm_topics = AsyncMock()
    _write_quick_commands_only_config(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    command_factory = MagicMock(
        side_effect=lambda name, desc: SimpleNamespace(
            command=name,
            description=desc,
        )
    )
    scope_factories = {
        name: MagicMock(
            side_effect=lambda scope_name=name: SimpleNamespace(kind=scope_name)
        )
        for name in (
            "BotCommandScopeDefault",
            "BotCommandScopeAllPrivateChats",
            "BotCommandScopeAllGroupChats",
        )
    }

    with (
        patch("telegram.BotCommand", command_factory),
        patch("telegram.BotCommandScopeDefault", scope_factories["BotCommandScopeDefault"]),
        patch(
            "telegram.BotCommandScopeAllPrivateChats",
            scope_factories["BotCommandScopeAllPrivateChats"],
        ),
        patch(
            "telegram.BotCommandScopeAllGroupChats",
            scope_factories["BotCommandScopeAllGroupChats"],
        ),
    ):
        await adapter._run_post_connect_housekeeping()

    assert adapter._bot.set_my_commands.await_count == 3
    assert {
        call.kwargs["scope"].kind
        for call in adapter._bot.set_my_commands.await_args_list
    } == set(scope_factories)
    for scope_factory in scope_factories.values():
        scope_factory.assert_called_once_with()
    for call in adapter._bot.set_my_commands.await_args_list:
        commands = call.args[0]
        assert [(cmd.command, cmd.description) for cmd in commands] == [
            ("agent_health", "Show agent health")
        ]


@pytest.mark.asyncio
async def test_ensure_forum_commands_registers_once():
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-123, is_forum=True)

    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session"), ("help", "Show help")], 0)
        with patch("telegram.BotCommand") as MockBotCommand:
            instances = []

            def _make_cmd(name, desc):
                cmd = MagicMock()
                cmd.name = name
                cmd.description = desc
                instances.append(cmd)
                return cmd

            MockBotCommand.side_effect = _make_cmd
            with patch("telegram.BotCommandScopeChat") as MockScope:
                # Track the chat_id passed to the BotCommandScopeChat constructor
                # so the assertions below see an int instead of a bare MagicMock.
                def _make_scope(chat_id):
                    s = MagicMock()
                    s.chat_id = chat_id
                    return s
                MockScope.side_effect = _make_scope
                await adapter._ensure_forum_commands(msg)

    assert -123 in adapter._forum_command_registered
    adapter._bot.set_my_commands.assert_awaited_once()
    args, kwargs = adapter._bot.set_my_commands.call_args
    assert len(args[0]) == 2  # two BotCommand instances
    assert kwargs["scope"] is not None
    assert isinstance(kwargs["scope"].chat_id, int)
    assert kwargs["scope"].chat_id == -123


@pytest.mark.asyncio
async def test_forum_registers_quick_commands_only_menu(tmp_path, monkeypatch):
    """A forum chat receives the same focused menu built from live config."""
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-456, is_forum=True)
    _write_quick_commands_only_config(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    command_factory = MagicMock(
        side_effect=lambda name, desc: SimpleNamespace(
            command=name,
            description=desc,
        )
    )
    scope_factory = MagicMock(
        side_effect=lambda chat_id: SimpleNamespace(chat_id=chat_id)
    )

    with (
        patch("telegram.BotCommand", command_factory),
        patch("telegram.BotCommandScopeChat", scope_factory),
    ):
        await adapter._ensure_forum_commands(msg)

    adapter._bot.set_my_commands.assert_awaited_once()
    args, kwargs = adapter._bot.set_my_commands.call_args
    assert [(cmd.command, cmd.description) for cmd in args[0]] == [
        ("agent_health", "Show agent health")
    ]
    assert kwargs["scope"].chat_id == -456


@pytest.mark.asyncio
async def test_ensure_forum_commands_race_safety():
    """Two concurrent coroutines must not double-register the same chat."""
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-789, is_forum=True)

    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session")], 0)
        with patch("telegram.BotCommand"):
            with patch("telegram.BotCommandScopeChat"):
                coro1 = adapter._ensure_forum_commands(msg)
                coro2 = adapter._ensure_forum_commands(msg)
                await asyncio.gather(coro1, coro2)

    # The lock should make this exactly 1 call, not 2.
    assert adapter._bot.set_my_commands.await_count == 1
