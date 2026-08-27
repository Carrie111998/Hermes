"""Gateway command help rendering tests."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, platform: Platform) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_help_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Telegram help output must not expose invalid uppercase/hyphenated slashes."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {
            "/Linear": {"description": "Open Linear"},
            "/Custom-Thing": {"description": "Run a custom thing"},
        },
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/custom_thing`" in result
    assert "`/Linear`" not in result
    assert "`/Custom-Thing`" not in result


@pytest.mark.asyncio
async def test_commands_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Paginated Telegram /commands output uses Telegram-valid slash mentions."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/Linear": {"description": "Open Linear"}},
    )

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 999", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/Linear`" not in result


@pytest.mark.asyncio
async def test_help_uses_matrix_safe_bang_command_mentions(monkeypatch):
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/arxiv": {"description": "Search arXiv"}},
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.MATRIX)
    )

    assert "`!model" in result
    assert "`!commands" in result
    assert "`!arxiv`" in result
    assert "`/model" not in result
    assert "`/commands" not in result
    assert "`/arxiv`" not in result


@pytest.mark.asyncio
async def test_commands_uses_matrix_safe_bang_entries_and_navigation(monkeypatch):
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/arxiv": {"description": "Search arXiv"}},
    )

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 1", Platform.MATRIX)
    )

    assert "`!" in result
    assert "`!commands 2`" in result
    assert "`/commands 2`" not in result


@pytest.mark.asyncio
async def test_commands_keeps_slash_prefix_on_non_matrix_gateway(monkeypatch):
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 1", Platform.DISCORD)
    )

    assert "`/start" in result
    assert "`!start" not in result


