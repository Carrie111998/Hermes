"""Test kanban env_vars forwarding in codex MCP entry (#92282)."""

from hermes_cli.codex_runtime_plugin_migration import _build_hermes_tools_mcp_entry


def test_hermes_tools_mcp_entry_forwards_kanban_env_vars():
    entry = _build_hermes_tools_mcp_entry()
    env_vars = entry.get("env_vars", [])
    expected = [
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_ROOT",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BRANCH",
    ]
    for var in expected:
        assert var in env_vars, f"{var} should be forwarded to codex MCP subprocess"
