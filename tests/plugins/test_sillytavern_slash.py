"""Unit tests for SillyTavern plugin slash parsers (no mic / no network)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def test_handle_rp_help():
    from plugins.sillytavern.slash import handle_rp

    text = handle_rp("help")
    assert "Usage: /rp" in text
    assert "list" in text


def test_handle_rp_list_calls_tool():
    from plugins.sillytavern.slash import handle_rp

    with patch(
        "plugins.sillytavern.st_character_list",
        return_value=json.dumps({"ok": True, "characters": [{"id": 1, "name": "A"}]}),
    ):
        out = handle_rp("list")
    data = json.loads(out)
    assert data["ok"] is True
    assert data["characters"][0]["name"] == "A"


def test_handle_rp_say_requires_message():
    from plugins.sillytavern.slash import handle_rp

    data = json.loads(handle_rp("say 1"))
    assert data["ok"] is False
    assert "Usage" in data["error"]


def test_handle_voice_help_and_unknown():
    from plugins.sillytavern.slash import handle_st_voice_roleplay

    assert "Usage: /st-voice-roleplay" in handle_st_voice_roleplay("help")
    data = json.loads(handle_st_voice_roleplay("nope"))
    assert data["ok"] is False


def test_core_cli_has_no_st_voice_builtin():
    """Regression: ST RP must not land in COMMAND_REGISTRY / core handlers."""
    from hermes_cli.commands import resolve_command

    assert resolve_command("st-voice-roleplay") is None
    assert resolve_command("rp") is None


def test_plugin_registers_slash_and_cli_commands():
    """register() wires slash + hermes CLI without core CommandDef."""
    from pathlib import Path
    import importlib.util
    import sys

    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "sillytavern"
    package_name = "sillytavern_slash_reg_test"
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = []
            self.commands = []
            self.cli_commands = {}

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_command(self, *args, **kwargs):
            self.commands.append((args, kwargs))

        def register_cli_command(self, name, **kwargs):
            self.cli_commands[name] = kwargs

    ctx = Ctx()
    module.register(ctx)
    slash_names = {args[0] for args, _ in ctx.commands}
    assert slash_names == {"rp", "st-voice-roleplay"}
    assert "sillytavern" in ctx.cli_commands
    assert callable(ctx.cli_commands["sillytavern"]["setup_fn"])
    assert callable(ctx.cli_commands["sillytavern"]["handler_fn"])

