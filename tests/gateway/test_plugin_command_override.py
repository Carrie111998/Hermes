"""Behavioral coverage for plugin overrides through gateway dispatch."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from hermes_cli.plugins import PluginCommandOverrideError, PluginContext, PluginManager, PluginManifest
from tests.gateway.test_gateway_command_dispatch_minimal import _make_event, _make_runner


def _register_help_override(tmp_path, monkeypatch, *, granted: bool):
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "home"
    home.mkdir()
    entry = {"granted_capabilities": ["commands.override"]} if granted else {}
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {"help-plugin": entry}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager(scope_key=str(home))
    context = PluginContext(
        PluginManifest(name="help-plugin", key="help-plugin", source="user"), manager
    )
    handler = MagicMock(return_value="gateway plugin help")
    if granted:
        context.register_command("help", handler, override=True)
    else:
        with pytest.raises(PluginCommandOverrideError):
            context.register_command("help", handler, override=True)
    monkeypatch.setattr(plugins_mod, "_ensure_plugins_discovered", lambda: manager)
    return handler


@pytest.mark.asyncio
async def test_authorized_plugin_help_override_wins_gateway_dispatch(tmp_path, monkeypatch):
    handler = _register_help_override(tmp_path, monkeypatch, granted=True)
    runner, _adapter = _make_runner()
    runner._handle_help_command = AsyncMock(side_effect=AssertionError("built-in /help ran"))

    result = await runner._handle_message(_make_event("/help raw arguments"))

    assert result == "gateway plugin help"
    handler.assert_called_once_with("raw arguments")
    runner._handle_help_command.assert_not_called()


@pytest.mark.asyncio
async def test_ungranted_plugin_cannot_replace_gateway_help(tmp_path, monkeypatch):
    handler = _register_help_override(tmp_path, monkeypatch, granted=False)
    runner, _adapter = _make_runner()
    runner._handle_help_command = AsyncMock(return_value="built-in gateway help")

    result = await runner._handle_message(_make_event("/help"))

    assert result == "built-in gateway help"
    handler.assert_not_called()
    runner._handle_help_command.assert_called_once()
