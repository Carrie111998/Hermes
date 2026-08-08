"""Tests for the light-mode terminal detection + color remap.

The detection + remap logic was extracted from cli.py into
hermes_cli/light_mode.py; cli.py re-exports the helpers and keeps the
import-time side effects (skin hook install + OSC 11 prime).  These tests
target the module that owns the logic (and the mutable cache global).

Covers the env-override path and the SkinConfig.get_color() wrapper that
the light-mode salvage installs at module import time.  We don't try to
fake an OSC 11 reply — the env-override branch short-circuits before the
terminal query, which is the path most users hit.
"""

from __future__ import annotations


import pytest


@pytest.fixture
def cli_mod(monkeypatch):
    """Import the light-mode module with its detection cache cleared.

    Importing cli first ensures the import-time _install_skin_light_mode_hook()
    has run (the SkinConfigHook tests assert on it).  We then reset the
    detection cache on the module that actually owns it so the per-test env
    override takes effect.
    """
    import cli  # noqa: F401  # trigger import-time hook install / prime
    import hermes_cli.light_mode as _lm

    monkeypatch.setattr(_lm, "_LIGHT_MODE_CACHE", None)
    return _lm


class TestLightModeDetection:
    def test_hermes_light_env_true_forces_light(self, cli_mod, monkeypatch):
        monkeypatch.setenv("HERMES_LIGHT", "1")
        assert cli_mod._detect_light_mode() is True

    def test_hermes_light_env_false_forces_dark(self, cli_mod, monkeypatch):
        monkeypatch.setenv("HERMES_LIGHT", "0")
        # Also blank out other signals so nothing else flips it light.
        monkeypatch.delenv("HERMES_TUI_LIGHT", raising=False)
        monkeypatch.delenv("HERMES_TUI_THEME", raising=False)
        monkeypatch.delenv("HERMES_TUI_BACKGROUND", raising=False)
        monkeypatch.delenv("COLORFGBG", raising=False)
        assert cli_mod._detect_light_mode() is False


    def test_background_hex_hint_light(self, cli_mod, monkeypatch):
        monkeypatch.delenv("HERMES_LIGHT", raising=False)
        monkeypatch.delenv("HERMES_TUI_LIGHT", raising=False)
        monkeypatch.delenv("HERMES_TUI_THEME", raising=False)
        monkeypatch.setenv("HERMES_TUI_BACKGROUND", "#FFFFFF")
        assert cli_mod._detect_light_mode() is True


    def test_colorfgbg_light_bg_slot(self, cli_mod, monkeypatch):
        monkeypatch.delenv("HERMES_LIGHT", raising=False)
        monkeypatch.delenv("HERMES_TUI_LIGHT", raising=False)
        monkeypatch.delenv("HERMES_TUI_THEME", raising=False)
        monkeypatch.delenv("HERMES_TUI_BACKGROUND", raising=False)
        monkeypatch.setenv("COLORFGBG", "0;15")  # bg slot 15 = light
        assert cli_mod._detect_light_mode() is True

    def test_cache_is_sticky(self, cli_mod, monkeypatch):
        monkeypatch.setenv("HERMES_LIGHT", "1")
        assert cli_mod._detect_light_mode() is True
        # Even if the env flips, the cached result wins until reset.
        monkeypatch.setenv("HERMES_LIGHT", "0")
        assert cli_mod._detect_light_mode() is True




class TestLightModeRemap:

    def test_remap_known_dark_color(self, cli_mod, monkeypatch):
        monkeypatch.setenv("HERMES_LIGHT", "1")
        # Force the detect cache to True for this test.
        cli_mod._LIGHT_MODE_CACHE = True
        assert cli_mod._maybe_remap_for_light_mode("#FFF8DC") == "#1A1A1A"
        assert cli_mod._maybe_remap_for_light_mode("#FFD700") == "#9A6B00"





class TestSkinConfigHook:
    """The salvage wraps SkinConfig.get_color at module import time so
    every skin color read goes through the light-mode remap.  Verify
    the hook installed and functions correctly.
    """

    def test_hook_installed(self, cli_mod):
        from hermes_cli.skin_engine import SkinConfig

        assert getattr(SkinConfig, "_hermes_light_mode_hook_installed", False) is True


    def test_skin_color_remaps_through_wrapper_in_light_mode(
        self, cli_mod, monkeypatch
    ):
        from hermes_cli.skin_engine import SkinConfig

        cli_mod._LIGHT_MODE_CACHE = True
        skin = SkinConfig(
            name="test",
            colors={"banner_text": "#FFF8DC", "response_border": "#FFD700"},
        )
        # The wrapper kicks in at get_color, not at construction time.
        assert skin.get_color("banner_text") == "#1A1A1A"
        assert skin.get_color("response_border") == "#9A6B00"

