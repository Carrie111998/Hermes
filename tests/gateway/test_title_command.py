"""Tests for /title gateway slash command.

Tests the _handle_title_command handler (set/show session titles)
across all gateway messenger platforms.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/title", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner(session_db=None):
    """Create a bare GatewayRunner with a mock session_store and optional session_db."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    # Gateway holds the async facade; the slash handlers await it.
    if session_db is not None:
        from hermes_state import AsyncSessionDB
        session_db = AsyncSessionDB(session_db)
    runner._session_db = session_db

    # Mock session_store that returns a session entry with a known session_id
    mock_session_entry = MagicMock()
    mock_session_entry.session_id = "test_session_123"
    mock_session_entry.session_key = "telegram:12345:67890"
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_session_entry
    runner.session_store = mock_store

    return runner


# ---------------------------------------------------------------------------
# _handle_title_command
# ---------------------------------------------------------------------------


class TestHandleTitleCommand:
    """Tests for GatewayRunner._handle_title_command."""


    @pytest.mark.asyncio
    async def test_title_conflict(self, tmp_path):
        """Setting a title already used by another session returns error."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("other_session", "telegram")
        db.set_session_title("other_session", "Taken Title")
        db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title Taken Title")
        result = await runner._handle_title_command(event)
        assert "already in use" in result
        assert "⚠️" in result
        db.close()


    @pytest.mark.asyncio
    async def test_title_control_chars_sanitized(self, tmp_path):
        """Control characters are stripped and sanitized title is stored."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title hello\x00world")
        result = await runner._handle_title_command(event)
        assert "helloworld" in result
        assert db.get_session_title("test_session_123") == "helloworld"
        db.close()


    @pytest.mark.asyncio
    async def test_set_title_propagates_to_telegram_topic_rename(self, tmp_path):
        """/title <name> also renames the visible Telegram topic, not just the DB."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        runner._schedule_telegram_topic_title_rename = MagicMock()

        event = _make_event(text="/title My Topic Name")
        result = await runner._handle_title_command(event)

        assert "My Topic Name" in result
        runner._schedule_telegram_topic_title_rename.assert_called_once_with(
            event.source, "test_session_123", "My Topic Name"
        )
        db.close()

    @pytest.mark.asyncio
    async def test_show_title_does_not_rename_topic(self, tmp_path):
        """Showing the title (no arg) must not trigger a topic rename."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("test_session_123", "telegram")
        db.set_session_title("test_session_123", "Existing Title")

        runner = _make_runner(session_db=db)
        runner._schedule_telegram_topic_title_rename = MagicMock()

        event = _make_event(text="/title")
        await runner._handle_title_command(event)

        runner._schedule_telegram_topic_title_rename.assert_not_called()
        db.close()


# ---------------------------------------------------------------------------
# Telegram topic duplicate title handling
# ---------------------------------------------------------------------------


class TestTelegramTopicDuplicateTitle:
    """Tests for Telegram topic duplicate-title lineage handling."""

    @pytest.mark.asyncio
    async def test_telegram_topic_duplicate_title_reserves_lineage_alias(self, tmp_path):
        """On Telegram topic lane, duplicate /title reserves #2 alias, renames topic to base."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("first_session", "telegram")
        db.set_session_title("first_session", "Workshop")
        db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        # Mock the telegram lane check to return True
        runner._is_telegram_topic_lane = lambda _: True
        runner._schedule_telegram_topic_title_rename = MagicMock()

        event = _make_event(text="/title Workshop")
        result = await runner._handle_title_command(event)

        # Should succeed with a suffixed alias but visible label "Workshop"
        assert "Workshop" in result
        assert "internal alias" in result
        assert "#2" in result
        runner._schedule_telegram_topic_title_rename.assert_called_once()
        # The visible label passed to rename should be "Workshop", not "Workshop #2"
        rename_call = runner._schedule_telegram_topic_title_rename.call_args[0]
        assert rename_call[2] == "Workshop", f"Expected visible label 'Workshop', got {rename_call[2]}"
        # The stored title should be the suffixed alias
        assert db.get_session_title("test_session_123") == "Workshop #2"
        db.close()

    @pytest.mark.asyncio
    async def test_non_telegram_duplicate_title_still_raises(self, tmp_path):
        """Non-Telegram lanes still raise ValueError on duplicate title."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("other_session", "api_server")
        db.set_session_title("other_session", "Taken")
        db.create_session("test_session_123", "api_server")

        runner = _make_runner(session_db=db)
        # Not a telegram topic lane
        runner._is_telegram_topic_lane = lambda _: False

        event = _make_event(text="/title Taken", platform=Platform.API_SERVER)
        result = await runner._handle_title_command(event)

        assert "already in use" in result
        assert "⚠️" in result
        db.close()


# ---------------------------------------------------------------------------
# /title in help and known_commands
# ---------------------------------------------------------------------------


class TestTitleInHelp:
    """Verify /title appears in help text and known commands."""
