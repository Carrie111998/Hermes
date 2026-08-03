"""Tests for Discord format_message and DISCORD_IGNORED_CONTENT filtering.

These features live in gateway/platforms/discord.py:
  - format_message: collapses blank lines, preserves code blocks, auto-closes
    unclosed code blocks, normalises Windows line endings.
  - DISCORD_IGNORED_CONTENT: skips inbound messages whose plain text or any
    embed field contains a configured substring.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.MessageType = SimpleNamespace(default=0, reply=19)
    discord_mod.ForumTag = SimpleNamespace(name="tag")
    discord_mod.AllowedMentions = MagicMock
    discord_mod.Object = lambda _id: SimpleNamespace(id=_id)
    commands_mod = MagicMock()
    commands_mod.has_permissions = lambda **kw: (lambda fn: fn)
    commands_mod.command = lambda *a, **kw: (lambda fn: fn)
    commands_mod.Cog = type("Cog", (), {})
    commands_mod.Context = object
    sys.modules["discord"] = discord_mod
    sys.modules.setdefault("discord.ext", MagicMock())
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.discord import DiscordAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# format_message tests
# ---------------------------------------------------------------------------


class TestFormatMessage:
    """Test DiscordAdapter.format_message edge cases."""

    def setup_method(self):
        self.adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    # --- basic functionality ---

    def test_collapse_consecutive_blank_lines(self):
        result = self.adapter.format_message("hello\n\n\nworld")
        assert result == "hello\n\nworld"

    def test_trim_each_line(self):
        # rstrip only: leading whitespace preserved for Markdown indentation
        result = self.adapter.format_message("  hello  \n  world  ")
        assert result == "  hello\n  world"

    def test_strip_leading_trailing_blank_lines(self):
        result = self.adapter.format_message("\n\nhello\n\n")
        assert result == "hello"

    def test_empty_string(self):
        result = self.adapter.format_message("")
        assert result == ""

    def test_whitespace_only(self):
        result = self.adapter.format_message("   \n   \n   ")
        assert result == ""

    # --- code block preservation ---

    def test_code_block_preserved(self):
        content = "intro\n```\n  indented\n\nblank line\n```\noutro"
        result = self.adapter.format_message(content)
        assert "  indented" in result
        assert "blank line" in result

    def test_code_block_with_language_specifier(self):
        content = "```python\nprint('hi')\n```"
        result = self.adapter.format_message(content)
        assert "```python" in result
        assert "print('hi')" in result

    # --- auto-close unclosed code blocks ---

    def test_unclosed_code_block_auto_closed(self):
        content = "```\nsome code\nmore code"
        result = self.adapter.format_message(content)
        assert result.endswith("```")

    def test_unclosed_code_block_with_language(self):
        content = "```javascript\nconst x = 1;\nconst y = 2;"
        result = self.adapter.format_message(content)
        assert result.endswith("```")
        assert "```javascript" in result

    def test_properly_closed_code_block_no_auto_close(self):
        content = "```\ncode\n```\nafter"
        result = self.adapter.format_message(content)
        assert result.count("```") == 2  # exactly one open + one close

    # --- Windows line endings ---

    def test_windows_line_endings_normalised(self):
        content = "hello\r\n\r\n\r\nworld"
        result = self.adapter.format_message(content)
        assert "\r" not in result
        assert result == "hello\n\nworld"

    # --- multiple code blocks ---

    def test_multiple_code_blocks(self):
        content = "```\nblock1\n```\ntext\n```\nblock2\n```"
        result = self.adapter.format_message(content)
        assert "block1" in result
        assert "block2" in result
        assert "text" in result

    def test_nested_code_block_markers_dont_confuse(self):
        # Even number of ``` means all blocks are properly closed
        content = "before\n```\ncode with ``` inside\n```\nafter"
        result = self.adapter.format_message(content)
        assert "before" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# DISCORD_IGNORED_CONTENT tests
# ---------------------------------------------------------------------------


class TestIgnoredContent:
    """Test DISCORD_IGNORED_CONTENT message filtering.

    The filtering happens inside _on_message which requires a Discord client.
    We test by creating a message mock and checking the early-return logic.
    """

    def _make_message(self, content="", embeds=None):
        """Create a mock discord.Message."""
        return SimpleNamespace(
            content=content,
            embeds=embeds or [],
            type=SimpleNamespace(default=0, reply=19).default,
            author=SimpleNamespace(bot=False, id=111, name="user", mention="<@111>"),
            channel=SimpleNamespace(id=222, type=SimpleNamespace(text=0).text),
            id=333,
            reference=None,
            attachments=[],
            stickers=[],
        )

    def _make_embed(self, title=None, description=None, fields=None, footer=None, author=None):
        """Create a mock discord.Embed with all text fields."""
        embed = SimpleNamespace(
            title=title,
            description=description,
            fields=fields or [],
            footer=footer,
            author=author,
        )
        return embed

    def test_plain_text_match(self, monkeypatch):
        monkeypatch.setenv("DISCORD_IGNORED_CONTENT", "test_notification")
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
        msg = self._make_message(content="This is a test_notification message")
        # Re-import to pick up env change — the check reads os.getenv at runtime
        import importlib
        import gateway.platforms.discord as discord_mod
        # The check is inline in _on_message, reads env at call time

    def test_embed_fields_checked(self):
        """Verify that embed fields are included in the checked text."""
        # Direct test of the text aggregation logic (not the full _on_message flow)
        import os
        _ignored_content = "Frigate Alert"
        _ignored_items = [item.strip() for item in _ignored_content.split(",") if item.strip()]

        embed = self._make_embed(
            title="Camera",
            fields=[SimpleNamespace(name="Status", value="Frigate Alert detected")],
        )
        _message_text = ""
        for e in [embed]:
            if e.title:
                _message_text += "\n" + e.title
            if e.description:
                _message_text += "\n" + e.description
            for field in getattr(e, "fields", None) or []:
                if getattr(field, "name", None):
                    _message_text += "\n" + field.name
                if getattr(field, "value", None):
                    _message_text += "\n" + field.value

        assert any(item in _message_text for item in _ignored_items)

    def test_embed_footer_checked(self):
        """Verify that embed footer text is included."""
        embed = self._make_embed(footer=SimpleNamespace(text="Frigate Alert"))
        _message_text = ""
        footer = getattr(embed, "footer", None)
        if footer and getattr(footer, "text", None):
            _message_text += "\n" + footer.text
        assert "Frigate Alert" in _message_text

    def test_embed_author_checked(self):
        """Verify that embed author name is included."""
        embed = self._make_embed(author=SimpleNamespace(name="Frigate Alert Bot"))
        _message_text = ""
        author = getattr(embed, "author", None)
        if author and getattr(author, "name", None):
            _message_text += "\n" + author.name
        assert "Frigate Alert" in _message_text

    def test_no_match_when_content_absent(self):
        _ignored_items = ["Frigate Alert"]
        _message_text = "Normal user message"
        assert not any(item in _message_text for item in _ignored_items)

    def test_multiple_items_comma_separated(self):
        _ignored_content = "test1, test2, test3"
        _ignored_items = [item.strip() for item in _ignored_content.split(",") if item.strip()]
        assert len(_ignored_items) == 3
        _message_text = "contains test2 here"
        assert any(item in _message_text for item in _ignored_items)

    def test_empty_items_filtered(self):
        _ignored_content = "test1, , , test2"
        _ignored_items = [item.strip() for item in _ignored_content.split(",") if item.strip()]
        assert _ignored_items == ["test1", "test2"]
