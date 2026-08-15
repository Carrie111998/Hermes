"""Cross-profile MCP runtime isolation (multiplexed gateway).

``gateway.multiplex_profiles`` serves several profiles from ONE process.
OAuth token stores are keyed by ``(HERMES_HOME, server_name)``, but the MCP
runtime (live connections, discovery/lazy/breaker/provenance state) used to
be keyed by server name alone, and MCP tool handlers were registered in the
process-global slot of :class:`~tools.registry.ToolRegistry`.

Consequence: two profiles that each configure a server called ``linear`` end
up sharing ONE ``MCPServerTask``. A tool call made on behalf of profile A
executes over profile B's already-connected session, using B's OAuth
identity.

These tests pin the contract: the MCP runtime is scoped to the profile's
canonical HERMES_HOME, an active profile scope selects only that profile's
connection, and a missing scope under multiplexing fails closed BEFORE any
transport work.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp
from agent.secret_scope import (
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from hermes_constants import (
    hermes_home_key,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.registry import registry


# ---------------------------------------------------------------------------
# Fakes — a session that reports which profile's credentials it holds.
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal ClientSession stand-in that echoes its owning identity."""

    def __init__(self, identity: str):
        self.identity = identity
        self.calls: list = []

    async def call_tool(self, tool_name, arguments=None):
        self.calls.append((tool_name, dict(arguments or {})))
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=self.identity)],
            structuredContent=None,
        )


def _fake_mcp_tool(name: str):
    return SimpleNamespace(
        name=name,
        description=f"{name} tool",
        inputSchema={"type": "object", "properties": {}},
        annotations=None,
    )


def _make_server(name: str, identity: str) -> "mcp.MCPServerTask":
    """A connected-looking MCPServerTask backed by a fake session."""
    server = mcp.MCPServerTask(name)
    server.session = _FakeSession(identity)
    server.tool_timeout = 10
    server._tools = [_fake_mcp_tool("whoami")]
    # HTTP-shaped config keeps _is_recycled_stdio() False (no stdio revival).
    server._config = {"url": "https://example.invalid/mcp"}
    return server


class _home_scope:
    """Context manager mirroring how the gateway scopes a profile's turn."""

    def __init__(self, home):
        self.home = str(home)
        self._token = None

    def __enter__(self):
        self._token = set_hermes_home_override(self.home)
        return self

    def __exit__(self, *exc):
        reset_hermes_home_override(self._token)
        return False


class _Profile:
    """One temp HERMES_HOME plus the MCP server registered under it."""

    def __init__(self, home, identity: str):
        self.home = home
        self.identity = identity
        self.server = None
        self.tool_names: list = []

    @property
    def scope(self) -> str:
        return hermes_home_key(self.home)

    def scoped(self):
        return _home_scope(self.home)


def _register_profile(profile: _Profile, server_name: str = "linear") -> None:
    """Connect + register ``server_name`` the way discovery does for a profile."""
    with profile.scoped():
        server = _make_server(server_name, profile.identity)
        mcp._servers[server_name] = server
        profile.server = server
        profile.tool_names = mcp._register_server_tools(server_name, server, {})
        server._registered_tool_names = list(profile.tool_names)


def _dispatch(profile: _Profile, tool_name: str, args: dict | None = None):
    """Dispatch a tool exactly as the agent does: ambient trusted scope only.

    No profile argument is threaded through the call — the model never sees
    one. The scope comes solely from the gateway's HERMES_HOME override.
    """
    with profile.scoped():
        return registry.dispatch(tool_name, args or {})


def _result_text(raw) -> str:
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert "error" not in payload, payload
    return payload["result"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_runtime(tmp_path, monkeypatch):
    """Isolated MCP runtime + registry, with a live MCP event loop."""
    launch_home = tmp_path / "launch-home"
    launch_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))

    mcp._ensure_mcp_loop()
    registered: list = []
    try:
        yield registered
    finally:
        for scope, name in registered:
            try:
                registry.deregister(name, scope=scope)
            except TypeError:  # pre-fix registry has no scope parameter
                registry.deregister(name)
            except Exception:
                pass
            mcp._forget_mcp_tool_server(name)
        reset = getattr(mcp, "_reset_mcp_runtimes_for_tests", None)
        if reset is not None:
            reset()
        else:  # pre-fix module state
            mcp._servers.clear()
        mcp._stop_mcp_loop()


@pytest.fixture
def profiles(tmp_path, mcp_runtime):
    """Two profiles that each configure an MCP server named ``linear``."""
    made = []
    for label in ("anirud", "rohan"):
        home = tmp_path / "profiles" / label
        home.mkdir(parents=True)
        made.append(_Profile(home, f"identity-{label}"))
    for profile in made:
        _register_profile(profile)
        for name in profile.tool_names:
            mcp_runtime.append((profile.scope, name))
    return made


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_same_server_name_registers_isolated_runtimes(profiles):
    """Both profiles keep their OWN MCPServerTask under the same name."""
    anirud, rohan = profiles

    assert anirud.server is not rohan.server
    assert mcp.mcp_prefixed_tool_name("linear", "whoami") in anirud.tool_names
    # Public tool names are identical across profiles — isolation is by
    # scope, not by mangling the model-visible name.
    assert anirud.tool_names == rohan.tool_names

    assert mcp._servers_for_scope(anirud.scope)["linear"] is anirud.server
    assert mcp._servers_for_scope(rohan.scope)["linear"] is rohan.server


def test_alternating_dispatch_never_crosses_profiles(profiles):
    """A call made under profile A must execute on A's connection only."""
    anirud, rohan = profiles
    tool = mcp.mcp_prefixed_tool_name("linear", "whoami")

    assert _result_text(_dispatch(anirud, tool)) == "identity-anirud"
    assert _result_text(_dispatch(rohan, tool)) == "identity-rohan"
    assert _result_text(_dispatch(anirud, tool)) == "identity-anirud"

    assert len(anirud.server.session.calls) == 2
    assert len(rohan.server.session.calls) == 1


def test_concurrent_dispatch_never_crosses_profiles(profiles):
    """Interleaved threads carrying different scopes stay on their own runtime."""
    anirud, rohan = profiles
    tool = mcp.mcp_prefixed_tool_name("linear", "whoami")
    results: dict = {}
    errors: list = []

    def worker(profile, key):
        try:
            observed = {
                _result_text(_dispatch(profile, tool)) for _ in range(8)
            }
            results[key] = observed
        except Exception as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(anirud, "anirud")),
        threading.Thread(target=worker, args=(rohan, "rohan")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert results == {
        "anirud": {"identity-anirud"},
        "rohan": {"identity-rohan"},
    }
    assert len(anirud.server.session.calls) == 8
    assert len(rohan.server.session.calls) == 8


def test_missing_scope_in_multiplex_mode_fails_before_transport(profiles):
    """A leaked scoped handler cannot fall back to the launch profile."""
    anirud, _rohan = profiles
    tool = mcp.mcp_prefixed_tool_name("linear", "whoami")
    entry = registry.get_entry(tool, scope=anirud.scope)
    assert entry is not None

    set_multiplex_active(True)
    try:
        assert registry.get_entry(tool) is None
        raw = entry.handler({})
    finally:
        set_multiplex_active(False)

    payload = json.loads(raw)
    assert payload["error_type"] == "mcp_profile_scope"
    assert anirud.server.session.calls == []


def test_unscoped_runtime_access_fails_closed_in_multiplex(profiles):
    """Discovery/status/reconnect cannot silently select the launch profile."""
    before = set(mcp._known_mcp_scopes())
    set_multiplex_active(True)
    try:
        with pytest.raises(mcp.MCPScopeError):
            mcp.get_mcp_status()
        with pytest.raises(mcp.MCPScopeError):
            mcp.reconnect_mcp_server("linear")
    finally:
        set_multiplex_active(False)

    assert set(mcp._known_mcp_scopes()) == before


def test_cross_profile_handler_reuse_fails_before_transport(profiles):
    """Even a directly leaked handler cannot execute under another profile."""
    anirud, rohan = profiles
    tool = mcp.mcp_prefixed_tool_name("linear", "whoami")
    entry = registry.get_entry(tool, scope=anirud.scope)
    assert entry is not None

    set_multiplex_active(True)
    try:
        with rohan.scoped():
            raw = entry.handler({})
    finally:
        set_multiplex_active(False)

    payload = json.loads(raw)
    assert payload["error_type"] == "mcp_profile_scope"
    assert anirud.server.session.calls == []
    assert rohan.server.session.calls == []


def test_profile_shutdown_leaves_other_profile_connected(profiles):
    """Reloading profile A cannot disconnect or deregister profile B."""
    anirud, rohan = profiles
    tool = mcp.mcp_prefixed_tool_name("linear", "whoami")

    mcp.shutdown_mcp_servers(scope=anirud.scope)

    with anirud.scoped():
        assert registry.get_entry(tool) is None
        assert "linear" not in mcp._servers
    with rohan.scoped():
        assert mcp._servers["linear"] is rohan.server
        assert _result_text(registry.dispatch(tool, {})) == "identity-rohan"
        assert registry.get_toolset_alias_target("linear") == "mcp-linear"


def test_global_mcp_fallback_is_rejected_in_multiplex(profiles):
    """A legacy global MCP handler cannot satisfy a profile-local lookup."""
    anirud, _rohan = profiles
    name = "mcp__legacy__whoami"
    calls = []
    registry.register(
        name=name,
        toolset="mcp-legacy",
        schema={"name": name, "description": "legacy", "parameters": {}},
        handler=lambda _args: calls.append(True) or json.dumps({"result": "wrong"}),
    )
    set_multiplex_active(True)
    try:
        with anirud.scoped():
            payload = json.loads(registry.dispatch(name, {}))
    finally:
        set_multiplex_active(False)
        registry.deregister(name)

    assert "Unknown tool" in payload["error"]
    assert calls == []


def test_cache_only_lazy_shutdown_clears_profile_state(tmp_path, mcp_runtime):
    """Reload removes lazy schemas/config even before a server connects."""
    home = tmp_path / "profiles" / "lazy"
    home.mkdir(parents=True)
    scope = hermes_home_key(home)
    tool = "mcp__linear__cached"
    with _home_scope(home):
        runtime = mcp._current_runtime()
        runtime.lazy_server_configs["linear"] = {"url": "https://old.invalid"}
        runtime.lazy_server_fingerprints["linear"] = "old"
        runtime.lazy_server_tool_names["linear"] = [tool]
        runtime.mcp_tool_server_names[tool] = "linear"
        registry.register(
            name=tool,
            toolset="mcp-linear",
            schema={"name": tool, "description": "cached", "parameters": {}},
            handler=lambda _args: "cached",
            scope=scope,
        )

    mcp.shutdown_mcp_servers(scope=scope)

    runtime = mcp._runtime_for(scope)
    assert runtime.lazy_server_configs == {}
    assert runtime.lazy_server_fingerprints == {}
    assert runtime.lazy_server_tool_names == {}
    assert runtime.mcp_tool_server_names == {}
    assert registry.get_entry(tool, scope=scope) is None


def test_stdio_stderr_logs_are_profile_local(profiles):
    anirud, rohan = profiles
    with anirud.scoped():
        anirud_log = mcp._get_mcp_stderr_log()
    with rohan.scoped():
        rohan_log = mcp._get_mcp_stderr_log()

    assert anirud_log is not rohan_log
    assert str(anirud.home) in anirud_log.name
    assert str(rohan.home) in rohan_log.name


def test_idle_probe_cannot_stop_another_profiles_shared_loop(profiles, tmp_path):
    anirud, _rohan = profiles
    empty_home = tmp_path / "empty-profile"
    empty_home.mkdir()

    with anirud.scoped():
        assert mcp._servers["linear"] is anirud.server

    with _home_scope(empty_home):
        # The active profile is empty, but the process-global loop is still
        # owned by another profile's registered server.
        assert mcp._stop_mcp_loop(only_if_idle=True) is False


def test_stdio_spawn_attribution_lock_is_process_global(profiles):
    anirud, rohan = profiles

    async def scenario():
        entered = []
        release_first = asyncio.Event()

        async def worker(profile, name):
            with profile.scoped():
                lock = mcp._get_stdio_spawn_attribution_lock()
                await lock.acquire()
                entered.append(name)
                if name == "anirud":
                    await release_first.wait()
                lock.release()

        first = asyncio.create_task(worker(anirud, "anirud"))
        await asyncio.sleep(0)
        second = asyncio.create_task(worker(rohan, "rohan"))
        await asyncio.sleep(0)
        assert entered == ["anirud"]
        release_first.set()
        await asyncio.gather(first, second)
        assert entered == ["anirud", "rohan"]

    asyncio.run(scenario())


def test_lazy_connect_and_shutdown_are_serialized_per_profile(tmp_path, monkeypatch):
    home = tmp_path / "lazy-race"
    home.mkdir()
    entered = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()

    def blocked_connect(_server_name):
        entered.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(mcp, "_ensure_lazy_server_connected_unlocked", blocked_connect)

    def connect_worker():
        with _home_scope(home):
            mcp._ensure_lazy_server_connected("linear")

    def shutdown_worker():
        with _home_scope(home):
            mcp.shutdown_mcp_servers(scope=mcp.current_mcp_scope(require=True))
            shutdown_done.set()

    connect_thread = threading.Thread(target=connect_worker)
    connect_thread.start()
    assert entered.wait(timeout=5)
    shutdown_thread = threading.Thread(target=shutdown_worker)
    shutdown_thread.start()
    assert not shutdown_done.wait(timeout=0.1)
    release.set()
    connect_thread.join(timeout=5)
    shutdown_thread.join(timeout=5)
    assert shutdown_done.is_set()


def test_stdio_safe_env_uses_profile_secret_not_process_value(monkeypatch):
    """Source-tagged stdio credentials must come from the routed profile."""
    from hermes_cli import env_loader

    monkeypatch.setenv("LINEAR_API_KEY", "wrong-process-profile")
    monkeypatch.setattr(
        env_loader,
        "get_secret_source",
        lambda key: "test-source" if key == "LINEAR_API_KEY" else None,
    )
    set_multiplex_active(True)
    token = set_secret_scope({"LINEAR_API_KEY": "anirud-profile"})
    try:
        safe_env = mcp._build_safe_env(None)
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)

    assert safe_env["LINEAR_API_KEY"] == "anirud-profile"
