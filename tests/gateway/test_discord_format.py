"""Discord format_message: tables and HTML details converted for Discord."""

import types
import sys


def _make_discord_adapter():
    """Construct a DiscordAdapter with discord.py stubbed out."""
    fake_discord = types.ModuleType("discord")
    fake_discord.Intents = type("Intents", (), {"default": classmethod(lambda cls: cls())})
    fake_discord.Message = object
    fake_ext = types.ModuleType("discord.ext")
    fake_commands = types.ModuleType("discord.ext.commands")
    fake_ext.commands = fake_commands
    fake_discord.ext = fake_ext
    sys.modules.setdefault("discord", fake_discord)
    sys.modules.setdefault("discord.ext", fake_ext)
    sys.modules.setdefault("discord.ext.commands", fake_commands)

    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    return adapter


class TestDiscordFormatMessage:

    def test_table_converted_to_bullets(self):
        adapter = _make_discord_adapter()
        text = (
            "Results:\n\n"
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 95   |\n"
            "| Bob   | 80   |\n"
            "\nDone."
        )
        out = adapter.format_message(text)
        assert "**Alice**" in out
        assert "• Score: 95" in out
        assert "**Bob**" in out
        assert "• Score: 80" in out
        assert out.startswith("Results:")
        assert out.rstrip().endswith("Done.")
        assert "|---" not in out

    def test_details_with_summary_is_expanded(self):
        adapter = _make_discord_adapter()

        out = adapter.format_message(
            "<details><summary>Notes</summary>Hidden content.</details>"
        )

        assert out == "📎 Notes\nHidden content."

    def test_multiline_and_uppercase_details_are_expanded(self):
        adapter = _make_discord_adapter()

        out = adapter.format_message(
            "<DETAILS><SUMMARY>Detailed analysis</SUMMARY>\n\nLine 1\nLine 2\n</DETAILS>"
        )

        assert out == "📎 Detailed analysis\nLine 1\nLine 2"

    def test_consecutive_same_line_details_are_expanded_independently(self):
        adapter = _make_discord_adapter()

        out = adapter.format_message(
            "<details><summary>First</summary>One.</details>"
            "<details><summary>Second</summary>Two.</details>"
        )

        assert out == "📎 First\nOne.\n📎 Second\nTwo."

    def test_nested_details_are_expanded(self):
        adapter = _make_discord_adapter()

        out = adapter.format_message(
            "<details><summary>Outer</summary>Before\n"
            "<details><summary>Inner</summary>Nested.</details>\n"
            "After</details>"
        )

        assert out == "📎 Outer\nBefore\n📎 Inner\nNested.\nAfter"

    def test_details_without_summary_keeps_body(self):
        adapter = _make_discord_adapter()

        assert adapter.format_message("<details open>Only body.</details>") == "Only body."

    def test_details_in_fenced_code_block_are_unchanged(self):
        adapter = _make_discord_adapter()
        text = "```html\n<details><summary>Example</summary>literal</details>\n```"

        assert adapter.format_message(text) == text

    def test_unterminated_details_is_preserved(self):
        adapter = _make_discord_adapter()
        text = "Before\n<details><summary>Incomplete</summary>Still here"

        assert adapter.format_message(text) == text

    def test_table_and_details_are_both_formatted(self):
        adapter = _make_discord_adapter()
        text = (
            "| Name | Score |\n"
            "|---|---|\n"
            "| Alice | 95 |\n\n"
            "<details><summary>Note</summary>Final score.</details>"
        )

        out = adapter.format_message(text)

        assert "**Alice**" in out
        assert "• Score: 95" in out
        assert "📎 Note\nFinal score." in out


