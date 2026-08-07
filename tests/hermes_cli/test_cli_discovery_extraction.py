"""Regression tests for the c8 extraction (wave-1 shard s5, implementer w1b).

``hermes_cli/cli_discovery.py`` now owns the plugin-CLI discovery gating
helpers that used to live in ``hermes_cli/main.py`` (``_first_positional_argv``,
``_plugin_cli_discovery_needed``, ``_resolve_deferred_platform_cli_command``,
plus the ``_BUILTIN_SUBCOMMANDS`` / ``_TOP_LEVEL_VALUE_FLAGS`` constants).
Bodies were lifted verbatim; ``hermes_cli.main`` re-imports every name so the
historical ``hermes_cli.main.<name>`` test-patch surface still resolves.

These tests pin the extracted behavior AND the re-export contract.
"""

from __future__ import annotations

import sys

from hermes_cli import cli_discovery
from hermes_cli import main as main_mod


# ── _first_positional_argv ─────────────────────────────────────────────────


def test_first_positional_argv_plain_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "chat", "hello"])
    assert cli_discovery._first_positional_argv() == "chat"


def test_first_positional_argv_skips_flag_values(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "-m", "gpt5", "chat"])
    assert cli_discovery._first_positional_argv() == "chat"


def test_first_positional_argv_inline_flag_value(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "--model=gpt5", "chat"])
    assert cli_discovery._first_positional_argv() == "chat"


def test_first_positional_argv_double_dash(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "--", "--not-a-flag"])
    assert cli_discovery._first_positional_argv() == "--not-a-flag"


def test_first_positional_argv_bare_or_flags_only(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes"])
    assert cli_discovery._first_positional_argv() is None
    monkeypatch.setattr(sys, "argv", ["hermes", "--verbose"])
    assert cli_discovery._first_positional_argv() is None


def test_first_positional_argv_continue_optional_value(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "-c", "my-session", "chat"])
    assert cli_discovery._first_positional_argv() == "chat"


# ── _plugin_cli_discovery_needed ───────────────────────────────────────────


def test_plugin_discovery_not_needed_for_builtin(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "logs", "--tail"])
    assert cli_discovery._plugin_cli_discovery_needed() is False


def test_plugin_discovery_not_needed_for_bare_hermes(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes"])
    assert cli_discovery._plugin_cli_discovery_needed() is False


def test_plugin_discovery_needed_for_unknown_token(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "photon", "serve"])
    assert cli_discovery._plugin_cli_discovery_needed() is True


def test_plugin_discovery_needed_for_chat_prompt_word(monkeypatch):
    # A non-flag word that is not a builtin subcommand could be a chat prompt;
    # discovery must run (amortized over the agent run anyway).
    monkeypatch.setattr(sys, "argv", ["hermes", "explain", "quantum"])
    assert cli_discovery._plugin_cli_discovery_needed() is True


# ── _BUILTIN_SUBCOMMANDS ↔ parser surface parity (subset) ─────────────────


def test_builtin_subcommands_covers_core_commands():
    for name in ("chat", "gateway", "setup", "status", "cron", "doctor",
                 "update", "version", "logs", "mcp", "memory", "tools"):
        assert name in cli_discovery._BUILTIN_SUBCOMMANDS, name


def test_top_level_value_flags_cover_model_and_oneshot():
    assert "-m" in cli_discovery._TOP_LEVEL_VALUE_FLAGS
    assert "--model" in cli_discovery._TOP_LEVEL_VALUE_FLAGS
    assert "-z" in cli_discovery._TOP_LEVEL_VALUE_FLAGS
    assert "--oneshot" in cli_discovery._TOP_LEVEL_VALUE_FLAGS


# ── _resolve_deferred_platform_cli_command (issue #54678) ──────────────────


def test_resolve_deferred_platform_cli_command_targets_matching_platform(monkeypatch):
    class _FakeRegistry:
        def __init__(self):
            self.resolved: list[str] = []

        def get(self, name):
            self.resolved.append(name)
            return None

    fake = _FakeRegistry()
    fake_module = type(sys)("gateway.platform_registry")
    fake_module.platform_registry = fake
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", fake_module)

    cli_discovery._resolve_deferred_platform_cli_command("photon")
    assert fake.resolved == ["photon"]


def test_resolve_deferred_platform_cli_command_noop_on_empty(monkeypatch):
    calls: list[str] = []

    class _FakeRegistry:
        def get(self, name):
            calls.append(name)

    fake_module = type(sys)("gateway.platform_registry")
    fake_module.platform_registry = _FakeRegistry()
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", fake_module)

    cli_discovery._resolve_deferred_platform_cli_command(None)
    cli_discovery._resolve_deferred_platform_cli_command("")
    assert calls == []


def test_resolve_deferred_platform_cli_command_survives_registry_errors(monkeypatch):
    class _BoomRegistry:
        def get(self, name):
            raise RuntimeError("registry exploded")

    fake_module = type(sys)("gateway.platform_registry")
    fake_module.platform_registry = _BoomRegistry()
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", fake_module)

    # Must not raise; failure is logged at debug level.
    cli_discovery._resolve_deferred_platform_cli_command("photon")


# ── re-export contract: hermes_cli.main.<name> still resolves ─────────────


def test_main_reexports_cli_discovery_names():
    assert main_mod._first_positional_argv is cli_discovery._first_positional_argv
    assert main_mod._plugin_cli_discovery_needed is cli_discovery._plugin_cli_discovery_needed
    assert main_mod._resolve_deferred_platform_cli_command is cli_discovery._resolve_deferred_platform_cli_command
    assert main_mod._BUILTIN_SUBCOMMANDS is cli_discovery._BUILTIN_SUBCOMMANDS
    assert main_mod._TOP_LEVEL_VALUE_FLAGS is cli_discovery._TOP_LEVEL_VALUE_FLAGS


def test_main_patch_surface_still_patchable(monkeypatch):
    """test_startup_plugin_gating patches ``hermes_cli.main._plugin_cli_discovery_needed``;
    the same patch must keep working against the re-exported name."""
    patched = []

    def _fake():
        patched.append(1)
        return False

    monkeypatch.setattr(main_mod, "_plugin_cli_discovery_needed", _fake)
    assert main_mod._plugin_cli_discovery_needed() is False
    assert patched == [1]
