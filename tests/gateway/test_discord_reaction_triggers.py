"""Reaction-trigger feature: emoji normalization, gating, synthesis."""
import asyncio
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# discord.py is an optional dep; mock it so the adapter imports
# (same shim as test_discord_attachment_download).
# ---------------------------------------------------------------------------
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
    discord_mod.ui = SimpleNamespace(
        View=object, button=lambda *a, **k: (lambda fn: fn), Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()


class _FakeEmoji:
    def __init__(self, s: str) -> None:
        self._s = s

    def __str__(self) -> str:
        return self._s


def _make_adapter() -> "Any":
    # EXACT mirror of test_discord_platform_events.py::_adapter (:79-86).
    # Bare instance skips heavy __init__ but MUST still set the three
    # attributes below: build_source reads self.platform/self.gateway_runner
    # (base.py:7152/:7145) and the `name` log-property derives from
    # self.platform (base.py:3627-3629).
    import plugins.platforms.discord.adapter as discord_adapter_module
    adapter_cls = discord_adapter_module.DiscordAdapter
    a = object.__new__(adapter_cls)
    a.platform = Platform.DISCORD
    a.config = SimpleNamespace(extra={})
    a.gateway_runner = None
    return a


def test_normalize_unicode_emoji_passthrough():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("👍")) == "👍"


def test_normalize_custom_emoji_strips_wrapper():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("<:paw:1234567890>")) == "paw"


def test_normalize_animated_custom_emoji():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(_FakeEmoji("<a:zoom:42>")) == "zoom"


def test_normalize_empty_is_empty():
    adapter = _make_adapter()
    assert adapter._normalize_reaction_emoji(None) == ""


def test_triggers_default_off(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    assert adapter._reaction_trigger_config() == (False, None)


def test_triggers_all_emoji_when_true(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", "true")
    assert adapter._reaction_trigger_config() == (True, None)


def test_triggers_allowlist_parsed(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", "👍, paw ,✅")
    enabled, allowlist = adapter._reaction_trigger_config()
    assert enabled is True
    assert allowlist == {"👍", "paw", "✅"}


def test_triggers_empty_allowlist_is_disabled(monkeypatch):
    # ",,," parses to an EMPTY allowlist -> must be (False, None), never
    # (True, set()): an inverted signal would read as "enabled for ALL emoji"
    # under the Slack-style convention where empty set == allow-all.
    adapter = _make_adapter()
    monkeypatch.setenv("DISCORD_REACTION_TRIGGERS", ",,,")
    assert adapter._reaction_trigger_config() == (False, None)


def test_yaml_list_maps_to_comma_env(monkeypatch):
    # exercise _apply_yaml_config the way cli-config loads it
    import os

    import plugins.platforms.discord.adapter as m
    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    yaml_cfg = {"discord": {"reaction_triggers": ["👍", "❤️"]}}
    discord_cfg = yaml_cfg["discord"]
    try:
        # call the real mapper with the same shape other keys use (mirror its callers)
        m._apply_yaml_config(yaml_cfg, discord_cfg)
        assert os.getenv("DISCORD_REACTION_TRIGGERS") == "👍,❤️"
    finally:
        # _apply_yaml_config writes os.environ DIRECTLY, which monkeypatch
        # does not track — pop manually or the value leaks into later tests.
        os.environ.pop("DISCORD_REACTION_TRIGGERS", None)


def test_yaml_bridge_skips_env_write_under_profile_scope(monkeypatch):
    # Multiplex profile loads (#72348) must seed PlatformConfig.extra WITHOUT
    # writing process-global env: a secondary profile's gates can never become
    # another profile's policy. Simulate _profile_scoped_config_load() -> True
    # and assert seed lands while env stays unset.
    import os

    import plugins.platforms.discord.adapter as m

    monkeypatch.delenv("DISCORD_REACTION_TRIGGERS", raising=False)
    monkeypatch.setattr(m, "_profile_scoped_config_load", lambda: True)
    yaml_cfg = {"discord": {"reaction_triggers": ["👍"]}}
    try:
        seeded = m._apply_yaml_config(yaml_cfg, yaml_cfg["discord"])
        assert os.getenv("DISCORD_REACTION_TRIGGERS") is None
    finally:
        os.environ.pop("DISCORD_REACTION_TRIGGERS", None)
    assert seeded is not None
    assert seeded.get("reaction_triggers") == "👍"
