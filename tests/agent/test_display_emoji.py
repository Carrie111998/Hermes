"""Tests for get_tool_emoji in agent/display.py — skin + registry integration."""

from unittest.mock import patch as mock_patch, MagicMock

from agent.display import get_tool_emoji


class TestGetToolEmoji:
    """Verify the skin → registry → fallback resolution chain."""





    def test_custom_default(self):
        """Custom default is returned when nothing matches."""
        with mock_patch("agent.display._get_skin", return_value=None):
            mock_reg = MagicMock()
            mock_reg.get_emoji.return_value = ""
            import sys
            mock_module = MagicMock()
            mock_module.registry = mock_reg
            with mock_patch.dict(sys.modules, {"tools.registry": mock_module}):
                result = get_tool_emoji("x", default="⚙️")
                assert result == "⚙️"

    def test_skin_override_only_for_matching_tool(self):
        """Skin override for one tool doesn't affect others."""
        skin = MagicMock()
        skin.tool_emojis = {"terminal": "⚔"}
        mock_reg = MagicMock()
        mock_reg.get_emoji.return_value = "🔍"
        import sys
        mock_module = MagicMock()
        mock_module.registry = mock_reg
        with mock_patch("agent.display._get_skin", return_value=skin), \
             mock_patch.dict(sys.modules, {"tools.registry": mock_module}):
            assert get_tool_emoji("terminal") == "⚔"  # skin override
            assert get_tool_emoji("web_search") == "🔍"  # registry fallback


class TestSkinConfigToolEmojis:
    """Verify SkinConfig handles tool_emojis field correctly."""


    def test_skin_config_accepts_tool_emojis(self):
        from hermes_cli.skin_engine import SkinConfig
        emojis = {"terminal": "⚔", "web_search": "🔮"}
        skin = SkinConfig(name="test", tool_emojis=emojis)
        assert skin.tool_emojis == emojis

    def test_build_skin_config_includes_tool_emojis(self):
        from hermes_cli.skin_engine import _build_skin_config
        data = {
            "name": "custom",
            "tool_emojis": {"terminal": "🗡️", "patch": "⚒️"},
        }
        skin = _build_skin_config(data)
        assert skin.tool_emojis == {"terminal": "🗡️", "patch": "⚒️"}



class TestRegistryEmojiValues:
    """Every declared tool emoji must actually be a glyph.

    The TUI prints ``registry.get_emoji()`` verbatim into the tool row, so a
    plain word slipped into ``emoji=`` renders as that word next to real icons.
    This stayed invisible while the TUI drew a generic bullet for every tool.
    The assertion is an invariant over whatever the registry holds, not a
    snapshot of it: tools added later are covered with no edit here.
    """

    def test_every_registered_tool_emoji_is_a_glyph(self):
        import model_tools  # noqa: F401  -- triggers tools/*.py auto-discovery
        from tools.registry import registry

        offenders = []
        for name in sorted(getattr(registry, "_tools", {})):
            emoji = registry.get_emoji(name, default="")
            if emoji and not any(ord(ch) > 0x2000 for ch in emoji):
                offenders.append((name, emoji))

        assert not offenders, (
            "these tools declare a non-glyph emoji= value, which the TUI would "
            f"print literally into the tool row: {offenders}"
        )
