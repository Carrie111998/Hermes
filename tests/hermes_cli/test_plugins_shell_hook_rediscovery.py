"""Regression: shell-hook callbacks survive a force=True re-discovery.

Reproduces the race where a background MCP-discovery sweep
(discover_and_load(force=True)) cleared _hooks and dropped shell hooks
wired by register_from_config, so pre_tool_call hooks stopped firing.
"""

from unittest.mock import patch

from hermes_cli.plugins import PluginManager


def _make_shell_hook_callback(event: str, command: str):
    """Mirror agent.shell_hooks._make_callback's shell_hook[...] tag."""
    def _callback(**kwargs):
        return None
    _callback.__name__ = f"shell_hook[{event}:{command}]"
    _callback.__qualname__ = _callback.__name__
    return _callback


def test_shell_hook_survives_forced_rediscovery():
    manager = PluginManager()
    cb = _make_shell_hook_callback(
        "pre_tool_call", "powershell -File edit-doc-check.ps1"
    )
    manager._hooks.setdefault("pre_tool_call", []).append(cb)

    with patch.object(manager, "_discover_and_load_inner", lambda: None):
        manager.discover_and_load(force=True)

    assert cb in manager._hooks.get("pre_tool_call", [])


def test_plugin_hook_dropped_by_forced_rediscovery():
    """Non-shell (plugin) callbacks are still cleared and re-registered."""
    manager = PluginManager()

    def plugin_cb(**kwargs):
        return None
    plugin_cb.__name__ = "audit_tool_call"
    manager._hooks.setdefault("pre_tool_call", []).append(plugin_cb)

    with patch.object(manager, "_discover_and_load_inner", lambda: None):
        manager.discover_and_load(force=True)

    assert plugin_cb not in manager._hooks.get("pre_tool_call", [])
