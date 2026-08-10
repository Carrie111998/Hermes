"""Exact static command-tool contracts for API-server agents."""

from __future__ import annotations

from uuid import uuid4

from hermes_cli.tools_config import _get_platform_tools
from model_tools import _clear_tool_defs_cache
from tools.registry import registry


APPROVED_COMMAND_TOOLS = {
    "terminal",
    "process",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "web_search",
    "web_extract",
    "vision_analyze",
    "delegate_task",
    "todo",
}


def _tool_names(agent) -> set[str]:
    return {tool["function"]["name"] for tool in agent.tools}


def _make_agent(
    enabled_toolsets: list[str],
    disabled_toolsets: list[str] | None = None,
):
    from run_agent import AIAgent

    return AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        model="test/model",
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets or [],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def _register_toolset_extension(name: str, toolset: str) -> None:
    registry.register(
        name=name,
        toolset=toolset,
        schema={
            "name": name,
            "description": "Dynamically registered toolset extension for testing.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: "{}",
    )
    _clear_tool_defs_cache()


def _register_terminal_extension(name: str) -> None:
    _register_toolset_extension(name, "terminal")


def test_restricted_command_platform_selection_is_exact_and_registry_stable(monkeypatch):
    # This contract tests membership, not host credential availability. Model a
    # correctly configured command agent so every approved schema is exposable.
    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    config = {
        "platform_toolsets": {
            # A static exact toolset is authoritative: unrelated entries must
            # not widen an API-server agent if config is merged incorrectly.
            "api_server": ["restricted-command", "terminal"],
        }
    }
    extension_name = f"test_dynamic_terminal_{uuid4().hex}"

    try:
        enabled_before = sorted(_get_platform_tools(config, "api_server"))
        agent_before = _make_agent(enabled_before)

        _register_terminal_extension(extension_name)

        enabled_after = sorted(_get_platform_tools(config, "api_server"))
        agent_after = _make_agent(enabled_after)

        assert enabled_before == ["restricted-command"]
        assert enabled_after == enabled_before
        assert _tool_names(agent_before) == APPROVED_COMMAND_TOOLS
        assert _tool_names(agent_after) == APPROVED_COMMAND_TOOLS
        assert extension_name not in getattr(agent_after, "valid_tool_names")
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_delegated_leaf_inherits_only_allowed_child_surface(monkeypatch):
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    extension_name = f"test_dynamic_terminal_{uuid4().hex}"
    parent = _make_agent(["restricted-command"])

    try:
        _register_terminal_extension(extension_name)
        child = _build_child_agent(
            task_index=0,
            goal="Inspect the requested files",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=3,
            task_count=1,
            parent_agent=parent,
            role="leaf",
        )

        # Leaf children inherit the exact parent surface, minus delegate_task
        # (children cannot recursively delegate unless explicitly orchestrators).
        assert getattr(child, "enabled_toolsets") == ["restricted-command"]
        assert _tool_names(child) == _tool_names(parent) - {"delegate_task"}
        assert extension_name not in getattr(child, "valid_tool_names")
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_parent_cannot_be_downgraded_to_registry_category(monkeypatch):
    from tools.delegate_tool import _build_child_agent, _expand_parent_toolsets

    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    extension_name = f"test_dynamic_terminal_{uuid4().hex}"
    parent = _make_agent(["restricted-command"])

    try:
        _register_terminal_extension(extension_name)
        assert _expand_parent_toolsets({"restricted-command"}) == {"restricted-command"}
        child = _build_child_agent(
            task_index=0,
            goal="Run a terminal command",
            context=None,
            toolsets=["terminal"],
            model=None,
            max_iterations=3,
            task_count=1,
            parent_agent=parent,
            role="leaf",
        )
        assert getattr(child, "enabled_toolsets") == []
        assert _tool_names(child) == set()
        assert extension_name not in getattr(child, "valid_tool_names")
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_ignores_implicit_kanban_widening(monkeypatch):
    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "exact-toolset-probe")
    _clear_tool_defs_cache()
    agent = _make_agent(["restricted-command"])
    assert _tool_names(agent) == APPROVED_COMMAND_TOOLS


def test_restricted_command_dominates_direct_mixed_selection(monkeypatch):
    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    extension_name = f"test_dynamic_terminal_{uuid4().hex}"
    try:
        _register_terminal_extension(extension_name)
        agent = _make_agent(["restricted-command", "terminal"])
        assert getattr(agent, "enabled_toolsets") == ["restricted-command"]
        assert _tool_names(agent) == APPROVED_COMMAND_TOOLS
        assert extension_name not in getattr(agent, "valid_tool_names")

        from tools.delegate_tool import _build_child_agent
        child = _build_child_agent(
            task_index=0,
            goal="Run a terminal command",
            context=None,
            toolsets=["terminal"],
            model=None,
            max_iterations=3,
            task_count=1,
            parent_agent=agent,
            role="leaf",
        )
        assert getattr(child, "enabled_toolsets") == []
        assert _tool_names(child) == set()
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_normalizes_mixed_refresh_override(monkeypatch):
    from tools.mcp_tool import refresh_agent_mcp_tools

    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    extension_name = f"test_dynamic_terminal_{uuid4().hex}"
    agent = _make_agent(["restricted-command"])
    try:
        _register_terminal_extension(extension_name)
        refresh_agent_mcp_tools(
            agent,
            enabled_override=["restricted-command", "terminal"],
            quiet_mode=True,
        )
        assert getattr(agent, "enabled_toolsets") == ["restricted-command"]
        assert _tool_names(agent) == APPROVED_COMMAND_TOOLS
        assert extension_name not in getattr(agent, "valid_tool_names")
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_orchestrator_preserves_exact_parent_boundary(monkeypatch):
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    monkeypatch.setattr("tools.delegate_tool._get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    extension_name = f"test_dynamic_delegation_{uuid4().hex}"
    parent = _make_agent(["restricted-command"])
    try:
        _register_toolset_extension(extension_name, "delegation")
        child = _build_child_agent(
            task_index=0,
            goal="Coordinate a bounded child task",
            context=None,
            toolsets=["terminal"],
            model=None,
            max_iterations=3,
            task_count=1,
            parent_agent=parent,
            role="orchestrator",
        )
        assert getattr(child, "enabled_toolsets") == ["restricted-command"]
        assert _tool_names(child) <= _tool_names(parent)
        assert "delegate_task" in _tool_names(child)
        assert extension_name not in getattr(child, "valid_tool_names")
    finally:
        registry.deregister(extension_name)
        _clear_tool_defs_cache()


def test_restricted_command_orchestrator_inherits_parent_delegation_denial(monkeypatch):
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setattr("tools.registry._check_fn_cached", lambda check_fn: True)
    monkeypatch.setattr("tools.delegate_tool._get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    parent = _make_agent(
        ["restricted-command"],
        disabled_toolsets=["delegation"],
    )
    child = _build_child_agent(
        task_index=0,
        goal="Coordinate without exceeding the parent's authority",
        context=None,
        toolsets=["terminal"],
        model=None,
        max_iterations=3,
        task_count=1,
        parent_agent=parent,
        role="orchestrator",
    )
    assert getattr(child, "enabled_toolsets") == ["restricted-command"]
    assert "delegate_task" not in _tool_names(parent)
    assert "delegate_task" not in _tool_names(child)
    assert _tool_names(child) <= _tool_names(parent)
