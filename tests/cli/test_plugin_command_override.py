"""Behavioral coverage for plugin overrides through CLI dispatch."""

from unittest.mock import MagicMock

import pytest
import yaml

from hermes_cli.plugins import PluginCommandOverrideError, PluginContext, PluginManager, PluginManifest


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
    handler = MagicMock(return_value="plugin help result")
    if granted:
        context.register_command("help", handler, override=True)
    else:
        with pytest.raises(PluginCommandOverrideError):
            context.register_command("help", handler, override=True)
    monkeypatch.setattr(plugins_mod, "_ensure_plugins_discovered", lambda: manager)
    return handler


def _make_cli():
    import cli as cli_mod

    instance = object.__new__(cli_mod.HermesCLI)
    instance.session_id = "plugin-override-cli"
    instance._pending_resume_sessions = None
    return instance


def test_authorized_plugin_help_override_wins_cli_dispatch(tmp_path, monkeypatch, capsys):
    handler = _register_help_override(tmp_path, monkeypatch, granted=True)
    cli = _make_cli()
    cli.show_help = MagicMock(side_effect=AssertionError("built-in /help ran"))

    assert cli.process_command("/help raw arguments") is True

    handler.assert_called_once_with("raw arguments")
    cli.show_help.assert_not_called()
    assert "plugin help result" in capsys.readouterr().out


def test_ungranted_plugin_cannot_replace_cli_help(tmp_path, monkeypatch):
    handler = _register_help_override(tmp_path, monkeypatch, granted=False)
    cli = _make_cli()
    cli.show_help = MagicMock()

    assert cli.process_command("/help") is True

    handler.assert_not_called()
    cli.show_help.assert_called_once()
