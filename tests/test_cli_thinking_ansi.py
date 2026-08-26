from unittest.mock import MagicMock, patch

from cli import _ThinkingAnsi


def _skin_with(color):
    fake = MagicMock()
    fake.get_color.return_value = color
    return fake


class TestThinkingAnsi:
    def test_default_returns_dim_italic_only(self):
        """ui_thinking unset -> dim+italic with no hue (legacy behavior)."""
        with patch("hermes_cli.skin_engine.get_active_skin",
                   return_value=_skin_with("")):
            assert str(_ThinkingAnsi()) == "\x1b[2;3m"

    def test_skin_color_tints_with_dim_italic(self):
        """ui_thinking set -> dim+italic plus a true-color tint."""
        with patch("hermes_cli.skin_engine.get_active_skin",
                   return_value=_skin_with("#BD93F9")), \
             patch("cli._maybe_remap_for_light_mode", side_effect=lambda c: c):
            assert str(_ThinkingAnsi()) == "\x1b[2;3;38;2;189;147;249m"

    def test_reset_re_resolves_after_skin_switch(self):
        """reset() clears the cache so a /skin switch re-reads the color."""
        t = _ThinkingAnsi()
        with patch("hermes_cli.skin_engine.get_active_skin",
                   return_value=_skin_with("")):
            assert str(t) == "\x1b[2;3m"  # cached

        with patch("hermes_cli.skin_engine.get_active_skin",
                   return_value=_skin_with("#00FF00")), \
             patch("cli._maybe_remap_for_light_mode", side_effect=lambda c: c):
            t.reset()
            assert str(t) == "\x1b[2;3;38;2;0;255;0m"
