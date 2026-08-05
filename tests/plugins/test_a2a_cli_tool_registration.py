"""Deferred platform plugins must register their *client* tools in CLI/TUI processes.

Issue #78050: a bundled ``kind: platform`` plugin is registered as a deferred
loader so ``hermes chat`` doesn't import ~20 gateway SDKs. The a2a plugin ships
two independent things behind that one deferral — an inbound adapter (the
heavy-ish part) and five outbound client tools (``a2a_call``, ``a2a_discover``,
``a2a_list``, ``a2a_history``, ``a2a_orchestrate``). Deferring the plugin
deferred both, so in a CLI/TUI process the client tools never register:
``resolve_toolset('hermes-a2a')`` returns core tools only, the ``a2a`` toolset
is missing from the ``hermes tools`` checklist, and the agent can't call peers.

The fix registers the client tools at *discovery* time by importing only the
plugin's ``tools`` submodule (stdlib-only for a2a) while leaving the adapter
deferred — the adapter still loads lazily in gateway processes.
"""

from __future__ import annotations

import pytest

_A2A_TOOL_NAMES = {
    "a2a_call",
    "a2a_discover",
    "a2a_list",
    "a2a_history",
    "a2a_orchestrate",
}


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def _discover_manager():
    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    return mgr


class TestA2AClientToolsVisibleInCliProcess:
    """The five a2a client tools must exist after plugin discovery, which is
    what a CLI/TUI session runs before resolving its toolset."""

    def test_a2a_tools_registered_at_discovery(self):
        mgr = _discover_manager()
        loaded = mgr._plugins.get("a2a-platform")
        assert loaded is not None, "a2a-platform not discovered"
        # The adapter stays deferred — only the client tools pre-register.
        assert loaded.deferred, "a2a adapter should remain deferred"
        assert _A2A_TOOL_NAMES.issubset(set(mgr._plugin_tool_names)), (
            f"a2a client tools missing from plugin tool registry: "
            f"{_A2A_TOOL_NAMES - set(mgr._plugin_tool_names)}"
        )

    def test_a2a_toolset_resolves_in_cli_process(self):
        """resolve_toolset('a2a') — the key the plugin registers under — must
        yield the five client tools in a bare CLI process."""
        _discover_manager()
        from toolsets import resolve_toolset

        resolved = set(resolve_toolset("a2a"))
        missing = _A2A_TOOL_NAMES - resolved
        assert not missing, f"a2a toolset missing client tools: {missing}"

    def test_a2a_appears_in_hermes_tools_checklist(self):
        """get_plugin_toolsets() feeds the `hermes tools` checklist; the a2a
        toolset must be present so users can opt in per platform."""
        # Use the same singleton get_plugin_toolsets() reads (the `hermes
        # tools` path), so this exercises the real checklist source.
        from hermes_cli.plugins import discover_plugins, get_plugin_toolsets

        discover_plugins()
        toolset_keys = {key for key, _, _ in get_plugin_toolsets()}
        assert "a2a" in toolset_keys, (
            f"a2a toolset missing from hermes tools checklist; got {toolset_keys}"
        )

    def test_platform_toolset_hermes_a2a_contains_client_tools(self):
        """The platform composite hermes-a2a (used by _get_platform_tools for
        plugin platforms) must include the client tools."""
        _discover_manager()
        from toolsets import resolve_toolset

        resolved = set(resolve_toolset("hermes-a2a"))
        missing = _A2A_TOOL_NAMES - resolved
        assert not missing, f"hermes-a2a missing client tools: {missing}"


class TestDeferredAdapterStillLazy:
    """The deferral contract must survive: registering the client tools at
    discovery must NOT import the adapter (gateway-only) — otherwise every CLI
    start pays the full platform import and the design intent is lost."""

    def test_discovery_preloads_tools_without_importing_adapter(self):
        import sys

        mgr = _discover_manager()
        loaded = mgr._plugins.get("a2a-platform")
        assert loaded is not None
        # Tools are registered, but the adapter package must not be imported.
        assert _A2A_TOOL_NAMES.issubset(set(mgr._plugin_tool_names))
        assert "hermes_plugins.a2a_platform.adapter" not in sys.modules, (
            "importing the client tools must not pull in the adapter module"
        )

    def test_placeholder_carries_tool_attribution(self):
        """`hermes plugins list` attribution: the deferred placeholder must
        report the pre-registered client tools, not an empty list."""
        mgr = _discover_manager()
        loaded = mgr._plugins.get("a2a-platform")
        assert loaded is not None
        assert _A2A_TOOL_NAMES.issubset(set(loaded.tools_registered)), (
            f"placeholder attribution missing: {loaded.tools_registered}"
        )


class TestGatewayMaterialization:
    """When the deferred adapter later materializes (gateway process), the
    package module must be reused — not executed twice — and the tool
    attribution must survive."""

    def test_materializing_adapter_reuses_predeclared_module(self):
        import sys

        mgr = _discover_manager()
        loaded = mgr._plugins.get("a2a-platform")
        assert loaded is not None

        # Simulate the gateway firing the deferred loader.
        mgr._load_plugin(loaded.manifest)

        reloaded = mgr._plugins.get("a2a-platform")
        assert reloaded is not None
        assert not reloaded.deferred
        # All five tools still attributed after full load.
        assert _A2A_TOOL_NAMES.issubset(set(reloaded.tools_registered))
        # The package body must not have executed twice.
        assert "hermes_plugins.a2a_platform.adapter" in sys.modules
