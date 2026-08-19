"""Behavior contracts for complete tool-surface assembly."""

from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.tool_surface import (
    AgentToolSurfaceSnapshot,
    assemble_full_tool_surface,
    get_agent_tool_surface,
    publish_agent_tool_surface,
)
from tools.tool_search import ToolSearchConfig


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": name,
        "parameters": {"type": "object", "properties": {}},
    }


def test_full_surface_is_non_mutating_and_deduplicates_injected_families():
    base = [_tool("read_file")]
    memory = [_schema("read_file"), _schema("memory_recall")]
    context = [_schema("context_expand")]
    original_base = deepcopy(base)
    original_memory = deepcopy(memory)
    original_context = deepcopy(context)

    surface = assemble_full_tool_surface(
        base,
        enabled_toolsets=["file", "memory", "context_engine"],
        memory_tool_schemas=memory,
        context_engine_tool_schemas=context,
        tool_search_config=ToolSearchConfig.from_raw({"enabled": "off"}),
    )

    names = [tool["function"]["name"] for tool in surface.tool_defs]
    assert names == ["read_file", "memory_recall", "context_expand"]
    assert surface.injected_names == {
        "memory": ["memory_recall"],
        "context_engine": ["context_expand"],
    }
    assert surface.skipped["memory"] == [
        {"tool": "read_file", "reason": "duplicate tool name"}
    ]
    assert base == original_base
    assert memory == original_memory
    assert context == original_context


def test_published_ownership_excludes_rejected_memory_collision():
    memory_manager = object()
    agent = SimpleNamespace(_memory_manager=memory_manager)
    surface = assemble_full_tool_surface(
        [_tool("read_file")],
        enabled_toolsets=["file", "memory"],
        memory_tool_schemas=[
            _schema("read_file"),
            _schema("memory_recall"),
        ],
        tool_search_config=ToolSearchConfig.from_raw({"enabled": "off"}),
    )

    snapshot = publish_agent_tool_surface(
        agent,
        surface.tool_defs,
        catalog_tool_defs=surface.pre_assembly_tool_defs,
        memory_provider_tool_names=surface.injected_names["memory"],
        context_engine_tool_names=set(),
        deferred_tool_names=surface.deferred_names,
        registry_generation=1,
        selection_revision=0,
        enabled_toolsets=["file", "memory"],
        disabled_toolsets=None,
    )

    assert snapshot.memory_provider_tool_names == frozenset({"memory_recall"})
    assert "read_file" not in snapshot.memory_provider_tool_names
    assert snapshot.memory_manager is memory_manager


def test_legacy_valid_name_addition_pins_current_registry_entry():
    from tools.registry import registry

    name = "legacy_valid_name_probe"
    registry.register(
        name=name,
        handler=lambda args, **kwargs: "{}",
        schema=_schema(name),
        toolset="legacy-surface-test",
    )
    try:
        entry = registry.get_entry(name)
        agent = SimpleNamespace()
        publish_agent_tool_surface(
            agent,
            [],
            context_engine_tool_names=set(),
            registry_entries=[],
            registry_generation=registry._generation,
            selection_revision=0,
            enabled_toolsets=None,
            disabled_toolsets=None,
        )
        agent.valid_tool_names.add(name)

        snapshot = get_agent_tool_surface(agent)
    finally:
        registry.deregister(name)

    assert dict(snapshot.registry_entries)[name] is entry


def test_legacy_context_engine_replacement_recomputes_provenance():
    class EngineA:
        name = "engine-a"

        def get_tool_schemas(self):
            return []

        def handle_tool_call(self, _name, _args, **_kwargs):
            return "a"

    class EngineB:
        name = "engine-b"

        def get_tool_schemas(self):
            return []

        def handle_tool_call(self, _name, _args, **_kwargs):
            return "b"

    engine_a = EngineA()
    engine_b = EngineB()
    agent = SimpleNamespace(
        tools=[],
        valid_tool_names=set(),
        _memory_provider_tool_names=set(),
        _context_engine_tool_names=set(),
        _memory_manager=None,
        context_compressor=engine_a,
        _tool_snapshot_generation=0,
        _tool_selection_revision=0,
        enabled_toolsets=[],
        disabled_toolsets=[],
    )
    original = publish_agent_tool_surface(
        agent,
        [],
        context_engine_tool_names=set(),
        registry_generation=0,
        selection_revision=0,
        enabled_toolsets=(),
        disabled_toolsets=(),
    )

    agent.context_compressor = engine_b
    replaced = get_agent_tool_surface(agent)

    assert replaced.context_engine is engine_b
    assert replaced.context_engine_provenance != original.context_engine_provenance


def test_context_engine_provenance_does_not_recurse_through_dynamic_mock_attrs():
    from agent.tool_surface import context_engine_provenance

    provenance = context_engine_provenance(MagicMock())

    assert provenance is not None


def test_full_surface_records_toolset_gates_without_mutating_schemas():
    memory = [_schema("memory_recall")]
    context = [_schema("context_expand")]

    surface = assemble_full_tool_surface(
        [_tool("read_file")],
        enabled_toolsets=["file"],
        memory_tool_schemas=memory,
        context_engine_tool_schemas=context,
        tool_search_config=ToolSearchConfig.from_raw({"enabled": "off"}),
    )

    names = {tool["function"]["name"] for tool in surface.tool_defs}
    assert names == {"read_file"}
    assert surface.skipped == {
        "memory": [{"tool": "memory_recall", "reason": "toolset disabled"}],
        "context_engine": [{"tool": "context_expand", "reason": "toolset disabled"}],
    }


def test_tool_search_runs_after_external_families_are_injected():
    from tools.registry import registry

    deferred_name = "surface_deferred_mcp_tool"
    registry.register(
        name=deferred_name,
        handler=lambda args, **kwargs: "{}",
        schema=_schema(deferred_name),
        toolset="mcp-surface-test",
    )
    try:
        surface = assemble_full_tool_surface(
            [_tool(deferred_name)],
            enabled_toolsets=["mcp-surface-test", "memory", "context_engine"],
            memory_tool_schemas=[_schema("surface_memory_recall")],
            context_engine_tool_schemas=[_schema("surface_context_expand")],
            tool_search_config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
    finally:
        registry.deregister(deferred_name)

    names = {tool["function"]["name"] for tool in surface.tool_defs}
    assert deferred_name not in names
    assert {"tool_search", "tool_describe", "tool_call"}.issubset(names)
    assert {"surface_memory_recall", "surface_context_expand"}.issubset(names)
    assert surface.tool_search_activated is True
    assert surface.deferred_names == [deferred_name]


def test_disabled_toolsets_override_enabled_external_families():
    surface = assemble_full_tool_surface(
        [_tool("read_file")],
        enabled_toolsets=["file", "memory", "context_engine"],
        disabled_toolsets=["memory", "context_engine"],
        memory_tool_schemas=[_schema("disabled_memory")],
        context_engine_tool_schemas=[_schema("disabled_context")],
        tool_search_config=ToolSearchConfig.from_raw({"enabled": "off"}),
    )

    names = {tool["function"]["name"] for tool in surface.tool_defs}
    assert names == {"read_file"}
    assert surface.skipped == {
        "memory": [{"tool": "disabled_memory", "reason": "toolset disabled"}],
        "context_engine": [{"tool": "disabled_context", "reason": "toolset disabled"}],
    }


def test_schema_sanitization_failure_is_fail_soft(monkeypatch):
    def _raise(_schemas):
        raise TypeError("non-copyable schema")

    monkeypatch.setattr("tools.schema_sanitizer.sanitize_tool_schemas", _raise)

    surface = assemble_full_tool_surface(
        [_tool("read_file")],
        apply_tool_search=False,
    )

    assert [tool["function"]["name"] for tool in surface.tool_defs] == ["read_file"]


def test_published_snapshot_stays_atomic_during_legacy_attribute_updates():
    """Readers block across publication and observe one complete generation."""
    setter_blocked = threading.Event()
    release_setter = threading.Event()
    reader_finished = threading.Event()
    observed = []

    class InterleavingAgent:
        block_publication = False

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if self.block_publication and name == "tools":
                setter_blocked.set()
                assert release_setter.wait(timeout=5)

    agent = InterleavingAgent()
    publish_agent_tool_surface(
        agent,
        [_tool("old")],
        context_engine_tool_names=set(),
        registry_generation=1,
        selection_revision=0,
        enabled_toolsets=["all"],
        disabled_toolsets=None,
    )
    old_snapshot = get_agent_tool_surface(agent)
    agent.block_publication = True

    publisher = threading.Thread(
        target=publish_agent_tool_surface,
        kwargs={
            "agent": agent,
            "tool_defs": [_tool("new")],
            "context_engine_tool_names": {"new"},
            "registry_generation": 2,
            "selection_revision": 1,
            "enabled_toolsets": ["file"],
            "disabled_toolsets": ["terminal"],
        },
    )
    publisher.start()
    assert setter_blocked.wait(timeout=5)

    def _read():
        observed.append(get_agent_tool_surface(agent))
        reader_finished.set()

    reader = threading.Thread(target=_read)
    reader.start()
    assert not reader_finished.wait(timeout=0.05)
    assert old_snapshot.valid_tool_names == frozenset({"old"})

    release_setter.set()
    publisher.join(timeout=5)
    reader.join(timeout=5)
    assert not publisher.is_alive()
    assert not reader.is_alive()

    new_snapshot = observed[0]
    assert isinstance(new_snapshot, AgentToolSurfaceSnapshot)
    assert new_snapshot is get_agent_tool_surface(agent)
    assert new_snapshot.valid_tool_names == frozenset({"new"})
    assert new_snapshot.context_engine_tool_names == frozenset({"new"})
    assert new_snapshot.selection_revision == 1


def test_snapshot_is_not_visible_until_global_name_publication_finishes(monkeypatch):
    import model_tools

    agent = type("Agent", (), {})()
    old_snapshot = publish_agent_tool_surface(
        agent,
        [_tool("old")],
        context_engine_tool_names=set(),
        registry_generation=1,
        selection_revision=0,
        enabled_toolsets=["all"],
        disabled_toolsets=None,
    )
    record_blocked = threading.Event()
    release_record = threading.Event()
    reader_finished = threading.Event()
    observed = []

    def _record(_tool_defs):
        record_blocked.set()
        assert release_record.wait(timeout=5)

    monkeypatch.setattr(model_tools, "record_resolved_tool_names", _record)
    publisher = threading.Thread(
        target=publish_agent_tool_surface,
        kwargs={
            "agent": agent,
            "tool_defs": [_tool("new")],
            "context_engine_tool_names": set(),
            "registry_generation": 2,
            "selection_revision": 1,
            "enabled_toolsets": ["all"],
            "disabled_toolsets": None,
        },
    )
    publisher.start()
    assert record_blocked.wait(timeout=5)
    assert getattr(agent, "_tool_surface_snapshot") is old_snapshot

    def _read():
        observed.append(get_agent_tool_surface(agent))
        reader_finished.set()

    reader = threading.Thread(target=_read)
    reader.start()
    assert not reader_finished.wait(timeout=0.05)

    release_record.set()
    publisher.join(timeout=5)
    reader.join(timeout=5)
    assert not publisher.is_alive()
    assert not reader.is_alive()
    assert observed[0].valid_tool_names == frozenset({"new"})


def test_published_snapshot_does_not_share_mutable_schemas_with_legacy_list():
    agent = type("Agent", (), {})()
    source = [_tool("stable")]

    snapshot = publish_agent_tool_surface(
        agent,
        source,
        context_engine_tool_names=set(),
        registry_generation=1,
        selection_revision=0,
        enabled_toolsets=["all"],
        disabled_toolsets=None,
    )
    source[0]["function"]["description"] = "source-mutated"
    agent.tools[0]["function"]["description"] = "legacy-mutated"

    assert snapshot.tool_defs[0]["function"]["description"] == "stable"


def test_explicit_legacy_surface_replacement_gets_a_coherent_compatibility_view():
    agent = type("Agent", (), {})()
    publish_agent_tool_surface(
        agent,
        [_tool("old")],
        context_engine_tool_names={"old"},
        registry_generation=1,
        selection_revision=0,
        enabled_toolsets=["all"],
        disabled_toolsets=None,
    )

    agent.tools = [_tool("new")]
    agent.valid_tool_names = {"new"}
    agent._context_engine_tool_names = {"new"}

    compat = get_agent_tool_surface(agent)
    assert [tool["function"]["name"] for tool in compat.tool_defs] == ["new"]
    assert compat.valid_tool_names == frozenset({"new"})
    assert compat.context_engine_tool_names == frozenset({"new"})
