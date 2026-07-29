"""Tests for the configurable Discord interactive-view timeout.

``_read_discord_prompt_timeout()`` backs the view lifetime of every Discord
interactive prompt (ExecApprovalView, SlashConfirmView, UpdatePromptView,
ClarifyChoiceView, ModelPickerView, ChoicePickerView). Resolution order is
explicit ``approvals.discord_prompt_timeout`` → backend clarify timeout →
300s fallback, with ``<= 0`` meaning *never expire* (``None``) and a MIN
clamp guarding against typos. There is no MAX clamp: the views hang off a
normal bot message, so each click mints a fresh interaction token and the
view can outlive Discord's ~15-minute token expiry.
"""

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

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import (  # noqa: E402
    _DISCORD_PROMPT_TIMEOUT_DEFAULT,
    _DISCORD_PROMPT_TIMEOUT_MIN,
    _read_discord_prompt_timeout,
)


def _patch_config(monkeypatch, cfg):
    """Stub ``hermes_cli.config.read_raw_config`` to return ``cfg``."""
    import hermes_cli.config
    monkeypatch.setattr(hermes_cli.config, "read_raw_config", lambda: cfg)


def test_falls_back_to_backend_default_when_config_absent(monkeypatch):
    """With nothing configured at all, the backend clarify default (3600s)
    is what the agent will actually wait, so that is what the buttons use.
    """
    from tools.clarify_gateway import resolve_clarify_timeout
    _patch_config(monkeypatch, {})
    assert _read_discord_prompt_timeout() == resolve_clarify_timeout({})


def test_default_when_approvals_block_missing(monkeypatch):
    from tools.clarify_gateway import resolve_clarify_timeout
    _patch_config(monkeypatch, {"other": {}})
    assert _read_discord_prompt_timeout() == resolve_clarify_timeout({"other": {}})


def test_default_when_key_missing(monkeypatch):
    cfg = {"approvals": {"mode": "manual"}}
    from tools.clarify_gateway import resolve_clarify_timeout
    _patch_config(monkeypatch, cfg)
    assert _read_discord_prompt_timeout() == resolve_clarify_timeout(cfg)


def test_derives_from_agent_clarify_timeout(monkeypatch):
    """Unset discord key → track ``agent.clarify_timeout``."""
    _patch_config(monkeypatch, {"agent": {"clarify_timeout": 7200}})
    assert _read_discord_prompt_timeout() == 7200


def test_derives_from_legacy_clarify_timeout(monkeypatch):
    """``clarify.timeout`` wins over ``agent.clarify_timeout``, matching
    ``resolve_clarify_timeout``'s own precedence.
    """
    _patch_config(
        monkeypatch,
        {"clarify": {"timeout": 1234}, "agent": {"clarify_timeout": 7200}},
    )
    assert _read_discord_prompt_timeout() == 1234


def test_unlimited_backend_wait_yields_never_expiring_buttons(monkeypatch):
    """The headline bug: an operator who tells the agent to wait forever
    used to still get buttons that died after 5 minutes, with a footer
    claiming "no action taken" while the agent was in fact still waiting.
    """
    _patch_config(monkeypatch, {"agent": {"clarify_timeout": 0}})
    assert _read_discord_prompt_timeout() is None


def test_explicit_discord_key_overrides_backend(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "approvals": {"discord_prompt_timeout": 600},
            "agent": {"clarify_timeout": 0},
        },
    )
    assert _read_discord_prompt_timeout() == 600


def test_explicit_int_value(monkeypatch):
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": 600}})
    assert _read_discord_prompt_timeout() == 600


def test_numeric_string_accepted(monkeypatch):
    """YAML parsers occasionally return numbers as strings; tolerate it."""
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": "450"}})
    assert _read_discord_prompt_timeout() == 450


def test_malformed_value_falls_back_to_default(monkeypatch):
    _patch_config(
        monkeypatch,
        {"approvals": {"discord_prompt_timeout": "five minutes"}},
    )
    assert _read_discord_prompt_timeout() == _DISCORD_PROMPT_TIMEOUT_DEFAULT


def test_value_clamped_to_minimum(monkeypatch):
    """A typo of e.g. 5 seconds must not make prompts disappear."""
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": 5}})
    assert _read_discord_prompt_timeout() == _DISCORD_PROMPT_TIMEOUT_MIN


def test_large_value_not_clamped(monkeypatch):
    """No MAX clamp: each button click mints a fresh interaction token, so a
    view attached to a normal bot message can outlive Discord's ~15-minute
    interaction-token expiry.
    """
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": 99999}})
    assert _read_discord_prompt_timeout() == 99999


def test_zero_means_never_expire(monkeypatch):
    """Zero = wait forever, matching the Hermes convention everywhere else.
    ``None`` is discord.py's "this view never times out".
    """
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": 0}})
    assert _read_discord_prompt_timeout() is None


def test_negative_means_never_expire(monkeypatch):
    _patch_config(monkeypatch, {"approvals": {"discord_prompt_timeout": -300}})
    assert _read_discord_prompt_timeout() is None


def test_empty_string_falls_back_to_backend(monkeypatch):
    cfg = {"approvals": {"discord_prompt_timeout": ""}, "agent": {"clarify_timeout": 900}}
    _patch_config(monkeypatch, cfg)
    assert _read_discord_prompt_timeout() == 900


def test_config_read_exception_falls_back_to_default(monkeypatch):
    """A crashing read_raw_config must not bring down view construction —
    falling back to the historical 300s default preserves existing behavior.
    """
    import hermes_cli.config
    def _boom():
        raise RuntimeError("config file corrupt")
    monkeypatch.setattr(hermes_cli.config, "read_raw_config", _boom)
    assert _read_discord_prompt_timeout() == _DISCORD_PROMPT_TIMEOUT_DEFAULT


def test_fallback_used_when_config_read_raises(monkeypatch):
    """The 300s constant is now a last-resort fallback for an unreadable
    config, not the routine default. Pinned so a refactor can't silently
    turn a config-read failure into a 30s or forever-alive view.
    """
    assert _DISCORD_PROMPT_TIMEOUT_DEFAULT == 300


def test_min_clamp_below_default():
    """Sanity: the fallback default must sit above the MIN clamp, or every
    fresh install would hit the clamp on its very first read.
    """
    assert _DISCORD_PROMPT_TIMEOUT_MIN <= _DISCORD_PROMPT_TIMEOUT_DEFAULT
