"""Behavioral coverage for plugin overrides through TUI method dispatch."""

import importlib
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_cli.plugins import PluginCommandOverrideError, PluginContext, PluginManager, PluginManifest


@pytest.fixture
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        module = importlib.import_module("tui_gateway.server")

    methods = dict(module._methods)
    yield module
    module._methods.clear()
    module._methods.update(methods)
    module._sessions.pop("plugin-override-session", None)


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
    handler = MagicMock(return_value="tui plugin help")
    if granted:
        context.register_command("help", handler, override=True)
    else:
        with pytest.raises(PluginCommandOverrideError):
            context.register_command("help", handler, override=True)
    monkeypatch.setattr(plugins_mod, "_ensure_plugins_discovered", lambda: manager)
    return handler


def _call_help(server):
    sid = "plugin-override-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}
    return server.handle_request(
        {
            "id": "plugin-override",
            "method": "slash.exec",
            "params": {"command": "/help raw arguments", "session_id": sid},
        }
    )


def test_authorized_plugin_help_override_wins_tui_dispatch(server, tmp_path, monkeypatch):
    handler = _register_help_override(tmp_path, monkeypatch, granted=True)
    built_in = MagicMock(side_effect=AssertionError("built-in /help ran"))
    monkeypatch.setattr(server, "_live_slash_command_output", built_in)

    response = _call_help(server)

    assert response["result"]["output"] == "tui plugin help"
    handler.assert_called_once_with("raw arguments")
    built_in.assert_not_called()


def test_ungranted_plugin_cannot_replace_tui_help(server, tmp_path, monkeypatch):
    handler = _register_help_override(tmp_path, monkeypatch, granted=False)
    built_in = MagicMock(return_value="built-in tui help")
    monkeypatch.setattr(server, "_live_slash_command_output", built_in)

    response = _call_help(server)

    assert response["result"]["output"] == "built-in tui help"
    handler.assert_not_called()
    built_in.assert_called_once()
