"""Tests for the shared MCP agent-tool refresh helper and discovery-wait bound.

``refresh_agent_mcp_tools`` is the single rebuild path used by the TUI
``reload.mcp`` RPC, the gateway reload, and the late-binding refresh thread —
so a slow MCP server that connects after the agent's one-time tool snapshot is
picked up everywhere identically.  These assert the *contracts* those callers
rely on (name-based diff, in-place mutation, agent-scoped filtering) rather than
freezing any particular tool list.
"""

import threading
import time
import types

from tools import mcp_tool


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


def _agent(tool_names, *, enabled=None, disabled=None):
    a = types.SimpleNamespace()
    a.tools = [_tool(n) for n in tool_names]
    a.valid_tool_names = set(tool_names)
    a.enabled_toolsets = enabled
    a.disabled_toolsets = disabled
    return a


def test_refresh_adds_late_landing_tools(monkeypatch):
    """A server that registers after build → its tools land in the snapshot."""
    agent = _agent(["read_file", "terminal"])

    new_defs = [_tool(n) for n in ("read_file", "terminal", "mcp_granola_get_account_info")]
    monkeypatch.setattr(mcp_tool, "get_tool_definitions", lambda **kw: new_defs, raising=False)
    # get_tool_definitions is imported inside the helper from model_tools, so patch there too.
    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: new_defs)

    added = mcp_tool.refresh_agent_mcp_tools(agent)

    assert added == {"mcp_granola_get_account_info"}
    assert "mcp_granola_get_account_info" in agent.valid_tool_names
    assert len(agent.tools) == 3


def test_refresh_preserves_memory_provider_and_context_engine_tools(monkeypatch):
    """B1 regression: a rebuild must NOT drop post-build-injected tools.

    get_tool_definitions() returns only the registry-derived tools. agent_init
    appends memory-provider tools (mem0/honcho/…) and context-engine tools
    (lcm_*) directly onto agent.tools AFTER that. A naive
    `agent.tools = get_tool_definitions()` would silently delete them on every
    refresh. The helper must re-inject them.
    """
    # Agent already carries: a built-in, a memory-provider tool, a context tool.
    agent = _agent(["read_file", "memory_search", "lcm_grep"])

    # Provider exposes its schemas; context compressor exposes lcm_*.
    agent._memory_manager = types.SimpleNamespace(
        get_all_tool_schemas=lambda: [
            {"name": "memory_search", "description": "", "parameters": {}}
        ]
    )
    agent.context_compressor = types.SimpleNamespace(
        get_tool_schemas=lambda: [
            {"name": "lcm_grep", "description": "", "parameters": {}}
        ]
    )
    agent._context_engine_tool_names = {"lcm_grep"}

    import model_tools
    # The registry now ALSO has a newly-connected MCP tool, but does NOT contain
    # the memory/context tools (they're never in get_tool_definitions output).
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file"), _tool("mcp_new_server_tool")],
    )

    added = mcp_tool.refresh_agent_mcp_tools(agent)

    # The new MCP tool landed AND the injected families survived.
    assert "mcp_new_server_tool" in agent.valid_tool_names
    assert "memory_search" in agent.valid_tool_names   # not clobbered
    assert "lcm_grep" in agent.valid_tool_names         # not clobbered
    assert added == {"mcp_new_server_tool"}


def test_refresh_does_not_reinject_disabled_memory_provider_tools(monkeypatch):
    """A refresh removes stale provider tools when memory becomes disabled."""
    agent = _agent(
        ["read_file", "memory_search"],
        enabled=["all"],
        disabled=["memory"],
    )
    agent._memory_manager = types.SimpleNamespace(
        get_all_tool_schemas=lambda: [
            {"name": "memory_search", "description": "", "parameters": {}}
        ]
    )

    import model_tools
    monkeypatch.setattr(
        model_tools,
        "get_tool_definitions",
        lambda **kw: [_tool("read_file")],
    )

    mcp_tool.refresh_agent_mcp_tools(agent)

    assert "memory_search" not in agent.valid_tool_names
    assert all(t["function"]["name"] != "memory_search" for t in agent.tools)


def test_refresh_respects_context_engine_toolset_gate(monkeypatch):
    """#5544: context-engine tools must NOT be re-injected on a restricted
    toolset. A platform with enabled_toolsets that excludes context_engine
    must not get lcm_* leaked back in by a refresh."""
    agent = _agent(["read_file"], enabled=["coding"])  # context_engine NOT enabled
    agent.context_compressor = types.SimpleNamespace(
        get_tool_schemas=lambda: [{"name": "lcm_grep", "description": "", "parameters": {}}]
    )
    agent._context_engine_tool_names = set()

    import model_tools
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file"), _tool("mcp_new_tool")],
    )

    mcp_tool.refresh_agent_mcp_tools(agent)

    assert "mcp_new_tool" in agent.valid_tool_names  # MCP tool still lands
    assert "lcm_grep" not in agent.valid_tool_names   # gated out (#5544)


def test_refreshed_tool_is_callable_through_valid_tool_names_guard(monkeypatch):
    """The whole point: a late tool, once refreshed, passes the name guard the
    run loop uses to accept/reject tool calls (agent.valid_tool_names)."""
    agent = _agent(["read_file"])

    import model_tools
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file"), _tool("mcp_granola_list_meetings")],
    )

    # Before refresh the run loop would reject the call ("Tool does not exist").
    assert "mcp_granola_list_meetings" not in agent.valid_tool_names

    mcp_tool.refresh_agent_mcp_tools(agent)

    # After refresh the same guard accepts it AND it's in the tools= payload.
    assert "mcp_granola_list_meetings" in agent.valid_tool_names
    assert any(t["function"]["name"] == "mcp_granola_list_meetings" for t in agent.tools)


def test_refresh_is_thread_safe_under_concurrent_calls(monkeypatch):
    """Concurrent refreshes keep tools / valid_tool_names coherent.

    The registry alternates between two DIFFERENT tool sets every call, so the
    write path (publish) runs repeatedly rather than short-circuiting on the
    no-change early return — this actually exercises the lock. The invariant:
    a reader of ``valid_tool_names`` must always match ``agent.tools``, and the
    final published pair must be one of the two valid sets (never a mix).
    """
    agent = _agent(["a"])

    import itertools
    set_a = [_tool("a"), _tool("b")]
    set_b = [_tool("a"), _tool("c")]
    flip = itertools.cycle([set_a, set_b])
    flip_lock = threading.Lock()

    def _gtd(**kw):
        with flip_lock:
            return list(next(flip))

    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions", _gtd)

    errors = []

    def _worker():
        try:
            for _ in range(50):
                mcp_tool.refresh_agent_mcp_tools(agent)
                # Coherence invariant: the name set must match the tool list
                # at every observation, never a torn cross-attribute state.
                names = {t["function"]["name"] for t in agent.tools}
                assert agent.valid_tool_names == names
                assert names in ({"a", "b"}, {"a", "c"})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert agent.valid_tool_names in ({"a", "b"}, {"a", "c"})


# ── SEP-2549 TTL-driven re-list (connected servers) ──────────────────────────


class _FakeListResult:
    """Stand-in for an MCP ``tools/list`` page with an optional ttlMs hint."""

    def __init__(self, tools, ttl_ms=None):
        self.tools = tools
        if ttl_ms is not None:
            self.ttlMs = ttl_ms


def _fake_mcp_tool(name):
    return types.SimpleNamespace(name=name, description="", inputSchema={})


def _fake_server(name, tool_names, *, ttl_ms=300000, listed_at_age_s=0.0):
    """A connected MCPServerTask stand-in whose session serves ``tool_names``.

    The served list is the MUTABLE ``tools`` list the test can extend or
    shrink to simulate a server-side tool-list change between lists.
    ``_discover_tools`` mirrors the real method's observable contract: fresh
    list → ``_tools`` replaced, cache meta repopulated, TTL anchor advanced.
    """
    import asyncio

    tools = [_fake_mcp_tool(n) for n in tool_names]

    async def list_tools():
        return _FakeListResult(tools, ttl_ms=ttl_ms)

    srv = types.SimpleNamespace(
        name=name,
        initialize_result=None,  # no capability info → tools advertised
        session=types.SimpleNamespace(list_tools=list_tools),
        _rpc_lock=asyncio.Lock(),
        _ready=types.SimpleNamespace(is_set=lambda: True),
        _config={},
        _tools=tools,
        _registered_tool_names=list(tool_names),
        _list_cache_meta={
            "ttl_ms": ttl_ms,
            "listed_at": time.time() - listed_at_age_s,
        },
    )

    async def _discover_tools():
        async with srv._rpc_lock:
            srv._list_cache_meta = {}
            result = await srv.session.list_tools()
            srv._tools = list(getattr(result, "tools", None) or [])
            fresh_ttl = getattr(result, "ttlMs", None)
            if fresh_ttl is not None:
                srv._list_cache_meta["ttl_ms"] = fresh_ttl
            srv._list_cache_meta["listed_at"] = time.time()

    srv._discover_tools = _discover_tools
    return srv


def _patch_ttl_test_harness(monkeypatch, server):
    """Shared patches for the TTL re-list tests: run the re-list inline on a
    throwaway loop, isolate ``_servers``, stub registration, and record
    deregistrations."""
    import asyncio

    def _run_on_loop(coro_or_factory, timeout=None):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", _run_on_loop)
    monkeypatch.setattr(mcp_tool, "_servers", {server.name: server})
    # Registration stub: the live manifest is whatever the re-list snapshot
    # holds — the real _register_server_tools would need real SDK objects.
    monkeypatch.setattr(
        mcp_tool, "_register_server_tools",
        lambda name, srv, config: [t.name for t in srv._tools],
        raising=False,
    )
    deregistered = []
    from tools import registry as registry_mod

    monkeypatch.setattr(
        registry_mod.registry, "deregister", lambda n: deregistered.append(n)
    )
    monkeypatch.setattr(
        mcp_tool, "_forget_mcp_tool_server", lambda n: deregistered.append(n)
    )
    return deregistered


def test_ttl_refresh_relists_expired_server_and_adds_new_tool(monkeypatch):
    """A connected server whose tools/list TTL expired is re-probed; the new
    server-side tool lands in the registry-backed agent snapshot."""
    import model_tools

    server = _fake_server(
        "dev_sitepro_server",
        ["mcp__dev_sitepro_server__get_site_languages"],
        listed_at_age_s=600.0,  # 10 min > 5 min TTL → expired
    )
    # The server gained a tool since the last list.
    server._tools.append(_fake_mcp_tool("mcp__dev_sitepro_server__get_site_countries"))

    deregistered = _patch_ttl_test_harness(monkeypatch, server)
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool(n) for n in (
            "read_file",
            "mcp__dev_sitepro_server__get_site_languages",
            "mcp__dev_sitepro_server__get_site_countries",
        )],
    )

    agent = _agent(["read_file", "mcp__dev_sitepro_server__get_site_languages"])
    added = mcp_tool.refresh_agent_mcp_tools(agent)

    assert "mcp__dev_sitepro_server__get_site_countries" in added
    assert "mcp__dev_sitepro_server__get_site_countries" in agent.valid_tool_names
    assert set(server._registered_tool_names) == {
        "mcp__dev_sitepro_server__get_site_languages",
        "mcp__dev_sitepro_server__get_site_countries",
    }
    assert deregistered == []  # nothing removed — the old tool is still served
    # The re-list advanced the TTL anchor so the next refresh is a no-op.
    assert abs(server._list_cache_meta["listed_at"] - time.time()) < 5.0


def test_ttl_refresh_deregisters_phantom_tools_no_longer_served(monkeypatch):
    """A tool the server stopped serving is deregistered after the re-list, so
    the model stops seeing a tool that can never succeed."""
    import model_tools

    server = _fake_server(
        "dev_sitepro_server",
        [
            "mcp__dev_sitepro_server__get_site_languages",
            "mcp__dev_sitepro_server__test_connection",
        ],
        listed_at_age_s=600.0,  # expired
    )
    # The server dropped test_connection since the last list.
    server._tools[:] = [_fake_mcp_tool("mcp__dev_sitepro_server__get_site_languages")]

    deregistered = _patch_ttl_test_harness(monkeypatch, server)
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file"),
                      _tool("mcp__dev_sitepro_server__get_site_languages")],
    )

    agent = _agent([
        "read_file",
        "mcp__dev_sitepro_server__get_site_languages",
        "mcp__dev_sitepro_server__test_connection",
    ])
    mcp_tool.refresh_agent_mcp_tools(agent)

    assert "mcp__dev_sitepro_server__test_connection" in deregistered
    assert "mcp__dev_sitepro_server__test_connection" not in agent.valid_tool_names
    assert server._registered_tool_names == [
        "mcp__dev_sitepro_server__get_site_languages"
    ]


def test_ttl_refresh_skips_server_within_ttl(monkeypatch):
    """A server whose tools/list TTL still holds is NOT re-probed — the whole
    point of the cache hint."""
    import model_tools

    server = _fake_server(
        "dev_sitepro_server",
        ["mcp__dev_sitepro_server__get_site_languages"],
        listed_at_age_s=60.0,  # 1 min < 5 min TTL → still valid
    )
    calls = []

    async def list_tools():
        calls.append(1)
        return _FakeListResult(server._tools, ttl_ms=300000)

    server.session.list_tools = list_tools
    _patch_ttl_test_harness(monkeypatch, server)
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file")],
    )

    mcp_tool.refresh_agent_mcp_tools(_agent(["read_file"]))

    assert calls == []  # tools/list never hit the server


def test_ttl_refresh_skips_server_without_ttl_hint(monkeypatch):
    """A pre-SEP-2549 server (no ttlMs) keeps the old never-expires behavior."""
    import model_tools

    server = _fake_server(
        "dev_sitepro_server",
        ["mcp__dev_sitepro_server__get_site_languages"],
        ttl_ms=None,
        listed_at_age_s=99999.0,
    )
    calls = []

    async def list_tools():
        calls.append(1)
        return _FakeListResult(server._tools)

    server.session.list_tools = list_tools
    _patch_ttl_test_harness(monkeypatch, server)
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [_tool("read_file")],
    )

    mcp_tool.refresh_agent_mcp_tools(_agent(["read_file"]))

    assert calls == []


# ── discovery-wait bound (mcp_discovery_timeout config) ──────────────────────


def test_resolve_discovery_timeout_explicit_wins(monkeypatch):
    from hermes_cli import mcp_startup

    assert mcp_startup._resolve_discovery_timeout(2.5) == 2.5


def test_wait_returns_instantly_when_no_discovery_thread(monkeypatch):
    """The common case (no MCP / discovery done) pays ~0s regardless of bound."""
    import time
    from hermes_cli import mcp_startup

    monkeypatch.setattr(mcp_startup, "_mcp_discovery_thread", None)
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"mcp_discovery_timeout": 999.0})

    t0 = time.time()
    mcp_startup.wait_for_mcp_discovery()
    assert time.time() - t0 < 0.2  # never blocks on the bound when nothing's pending
