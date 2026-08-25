"""Per-profile MCP registration, state, and discovery slots (#67605).

Upstream's own note in ``tui_gateway/entry.py`` read: "MCP tool registration
is process-global, so in a multi-profile process the FIRST profile that builds
an agent wins the discovery slot."  In the dashboard / desktop backend one
compute-host process serves several ``HERMES_HOME`` profiles, so that meant a
session switched to profile B either saw profile A's servers or none at all.

These tests pin the contract in both directions:

* **Multi-profile** — server state, tool registration, the discovery slot, and
  the trust gate each resolve to the profile the caller is scoped to, and a
  process-wide teardown still reaps every profile.
* **Single-profile** — the scoping is inert: exactly one bucket ever exists and
  every operation is the plain dict/set operation it was before.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.mcp_tool as mcp
from hermes_constants import (
    hermes_home_key,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.registry import registry


@pytest.fixture
def profiles(tmp_path):
    """Two profile homes with DIFFERENT mcp_servers, as the dashboard serves.

    This is the #67605 reproduction shape: one process, two homes, and only
    one of them configuring the server the user is asking about.
    """
    a = tmp_path / "profile-a"
    b = tmp_path / "profile-b"
    a.mkdir()
    b.mkdir()
    (a / "config.yaml").write_text(
        "mcp_servers:\n  proxmox:\n    command: proxmox-mcp\n"
        "  github:\n    command: github-mcp\n"
    )
    (b / "config.yaml").write_text(
        "mcp_servers:\n  github:\n    command: github-mcp\n"
    )
    return a, b


class _scoped_to:
    """Run a block as a turn bound to one profile's HERMES_HOME."""

    def __init__(self, home):
        self._home = home
        self._token = None

    def __enter__(self):
        self._token = set_hermes_home_override(str(self._home))
        return self

    def __exit__(self, *exc):
        reset_hermes_home_override(self._token)
        return False


@pytest.fixture(autouse=True)
def _clean_mcp_state():
    """Drop every profile's MCP state before and after each test."""
    def _wipe():
        for container in (
            mcp._servers,
            mcp._server_connecting,
            mcp._server_connect_errors,
            mcp._lazy_server_configs,
            mcp._lazy_server_fingerprints,
            mcp._lazy_server_tool_names,
            mcp._server_trust_levels,
            mcp._tool_read_only_hints,
            mcp._mcp_tool_server_names,
        ):
            container._by_scope.clear()
        mcp._profile_lifecycle_generations.clear()
        mcp._profile_lifecycle_locks.clear()

    _wipe()
    yield
    _wipe()


def _fake_server(name):
    """Minimal stand-in for a connected MCPServerTask, as status code sees it."""
    return SimpleNamespace(
        name=name,
        session=object(),
        _tools=[],
        _registered_tool_names=[f"mcp__{name}_ping"],
        _config={},
        _error=None,
        _was_parked=False,
        _sampling=None,
        _registered_scope=None,
    )


# ---------------------------------------------------------------------------
# Multi-profile: the limitation this change closes
# ---------------------------------------------------------------------------


def test_server_state_does_not_leak_between_profiles(profiles):
    """Profile B must not inherit profile A's connected servers."""
    home_a, home_b = profiles

    with _scoped_to(home_a):
        mcp._servers["proxmox"] = _fake_server("proxmox")
        assert "proxmox" in mcp._servers

    with _scoped_to(home_b):
        assert "proxmox" not in mcp._servers
        assert list(mcp._servers) == []
        # B's own config has no proxmox at all, and A's connected instance
        # must not be borrowed to answer for it.
        assert [
            entry["name"] for entry in mcp.get_mcp_status() if entry["connected"]
        ] == []

    # ...and A still reports its own as connected.
    with _scoped_to(home_a):
        assert [
            entry["name"] for entry in mcp.get_mcp_status() if entry["connected"]
        ] == ["proxmox"]


def test_same_server_name_in_two_profiles_is_two_servers(profiles):
    """Equal display names in different profiles must not share one slot."""
    home_a, home_b = profiles

    with _scoped_to(home_a):
        mcp._servers["github"] = _fake_server("github")
    with _scoped_to(home_b):
        mcp._servers["github"] = _fake_server("github")

    with _scoped_to(home_a):
        server_a = mcp._servers["github"]
    with _scoped_to(home_b):
        server_b = mcp._servers["github"]

    assert server_a is not server_b
    assert mcp._servers.total_len() == 2


def test_registered_mcp_tools_are_scoped_to_their_profile(profiles):
    """A tool registered from profile A's config is invisible to profile B."""
    home_a, home_b = profiles
    entry = {
        "fingerprint": "fp-a",
        "tools": [
            {
                "name": "get_cluster_resources",
                "description": "List cluster resources",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        "utility_tools": [],
    }
    config = {"command": "proxmox-mcp", "lazy": True}

    with patch(
        "tools.mcp_schema_cache.config_fingerprint", return_value="fp-a"
    ):
        with _scoped_to(home_a):
            registered = mcp._register_from_cache_sync("proxmox", config, entry)

    assert registered, "expected the cached manifest to register a tool"
    tool_name = registered[0]

    try:
        with _scoped_to(home_a):
            assert registry.get_entry(tool_name) is not None
        with _scoped_to(home_b):
            assert registry.get_entry(tool_name) is None, (
                "profile B must not see profile A's MCP tool"
            )
    finally:
        with _scoped_to(home_a):
            registry.deregister(tool_name, scope=hermes_home_key(home_a))


def test_discovery_slot_is_claimed_per_profile(profiles):
    """The first profile to start discovery must not consume B's slot."""
    from hermes_cli import mcp_startup

    home_a, home_b = profiles
    logger = SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    discovered = []
    gate = threading.Event()

    def _fake_discover():
        discovered.append(hermes_home_key())
        gate.wait(timeout=5)

    started = mcp_startup._mcp_discovery_started
    threads = mcp_startup._mcp_discovery_threads
    prior_started, prior_threads = set(started), dict(threads)
    started.clear()
    threads.clear()
    try:
        with patch.object(
            mcp_startup, "_has_configured_mcp_servers", return_value=True
        ), patch.object(
            mcp_startup,
            "_discover_mcp_tools_without_interactive_oauth",
            _fake_discover,
        ):
            with _scoped_to(home_a):
                mcp_startup.start_background_mcp_discovery(
                    logger=logger, thread_name="test-a"
                )
                assert mcp_startup.mcp_discovery_in_flight() is True
            with _scoped_to(home_b):
                # Pre-fix this returned immediately: the process-global
                # "already started" flag was set by profile A.
                mcp_startup.start_background_mcp_discovery(
                    logger=logger, thread_name="test-b"
                )
                assert mcp_startup.mcp_discovery_in_flight() is True

            assert set(threads) == {
                hermes_home_key(home_a),
                hermes_home_key(home_b),
            }
        gate.set()
        for thread in list(threads.values()):
            thread.join(timeout=5)
        assert sorted(discovered) == sorted(
            [hermes_home_key(home_a), hermes_home_key(home_b)]
        )
    finally:
        gate.set()
        for thread in list(threads.values()):
            thread.join(timeout=5)
        started.clear()
        started.update(prior_started)
        threads.clear()
        threads.update(prior_threads)


def test_trust_gate_fails_closed_outside_the_registering_profile(profiles):
    """An unresolvable scope must not silently downgrade to trust: full."""
    home_a, home_b = profiles

    with _scoped_to(home_a):
        mcp._record_tool_trust_metadata(
            "risky",
            {"trust": "untrusted"},
            [SimpleNamespace(name="write_file", annotations=None)],
        )
        assert mcp._trust_gate_check("risky", "write_file") is not None

    with _scoped_to(home_b):
        # B has no record for "risky", but A does: we cannot prove the
        # operator marked it trusted, so the gate must stay on.
        assert mcp._trust_gate_check("risky", "write_file") is not None
        # A server no profile knows about keeps the historical default.
        assert mcp._trust_gate_check("unknown-server", "write_file") is None


def test_shutdown_reaps_every_profiles_servers(profiles):
    """Process teardown is cross-profile: the MCP event loop is shared."""
    home_a, home_b = profiles

    with _scoped_to(home_a):
        mcp._servers["a-server"] = _fake_server("a-server")
    with _scoped_to(home_b):
        mcp._servers["b-server"] = _fake_server("b-server")

    assert {s.name for s in mcp._servers.all_values()} == {"a-server", "b-server"}
    assert mcp._servers.total_len() == 2


def test_idle_check_sees_other_profiles_servers(profiles):
    """One profile's teardown must not close a loop another profile is using."""
    home_a, home_b = profiles

    with _scoped_to(home_a):
        mcp._servers["still-alive"] = _fake_server("still-alive")

    with _scoped_to(home_b):
        assert len(mcp._servers) == 0          # nothing in B's own scope...
        assert mcp._servers.total_len() == 1   # ...but the process is not idle


def test_reload_fences_inflight_eager_publication(profiles, monkeypatch):
    """A pre-reload connect cannot publish after the new generation wins."""
    home_a, _ = profiles
    scope_a = hermes_home_key(home_a)
    old_started = threading.Event()
    release_old = threading.Event()
    connect_calls = 0
    tool_name = "mcp__shared__ping"

    class _Connected:
        def __init__(self, label):
            self.label = label
            self.name = "shared"
            self.session = object()
            self._registered_tool_names = []
            self._registered_scope = scope_a
            self.shutdown_calls = 0

        async def shutdown(self):
            self.shutdown_calls += 1
            self.session = None

    async def _connect(_name, _config):
        nonlocal connect_calls
        connect_calls += 1
        label = "old" if connect_calls == 1 else "new"
        if label == "old":
            old_started.set()
            await asyncio.to_thread(release_old.wait, 10)
        return _Connected(label)

    def _register(_name, server, _config):
        registry.register(
            name=tool_name,
            toolset="mcp-shared",
            schema={"name": tool_name, "description": server.label},
            handler=lambda _args, label=server.label: label,
            scope=scope_a,
        )
        registry.register_toolset_alias(
            "shared", "mcp-shared", scope=scope_a
        )
        return [tool_name]

    monkeypatch.setattr(mcp, "_connect_server", _connect)
    monkeypatch.setattr(mcp, "_register_server_tools", _register)
    results = {}

    def _run(label):
        with _scoped_to(home_a):
            try:
                results[label] = asyncio.run(
                    mcp._discover_and_register_server("shared", {})
                )
            except BaseException as exc:  # exact stale-attempt witness
                results[label] = exc

    old = threading.Thread(target=_run, args=("old",), daemon=True)
    old.start()
    assert old_started.wait(timeout=10)

    with _scoped_to(home_a):
        mcp.shutdown_current_profile_mcp_servers()

    new = threading.Thread(target=_run, args=("new",), daemon=True)
    new.start()
    new.join(timeout=10)
    assert not new.is_alive()

    release_old.set()
    old.join(timeout=10)
    assert not old.is_alive()
    assert isinstance(results["old"], mcp._MCPProfileReloaded)
    assert results["new"] == [tool_name]
    assert registry.dispatch(tool_name, {}, scope=scope_a) == "new"

    with _scoped_to(home_a):
        mcp.shutdown_current_profile_mcp_servers()


def test_reload_serializes_lazy_cache_publication(profiles, monkeypatch):
    """Lazy registry/trust/provenance publication is atomic with reload."""
    home_a, _ = profiles
    scope_a = hermes_home_key(home_a)
    entry = {
        "fingerprint": "fp",
        "tools": [
            {
                "name": "ping",
                "description": "ping",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        "utility_tools": [],
    }
    config = {"command": "shared-mcp", "lazy": True, "trust": "untrusted"}
    register_entered = threading.Event()
    release_register = threading.Event()
    shutdown_finished = threading.Event()
    lazy_result = []
    original_register = registry.register

    def _blocking_register(*args, **kwargs):
        register_entered.set()
        assert release_register.wait(timeout=10)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register", _blocking_register)

    def _lazy():
        with _scoped_to(home_a), patch(
            "tools.mcp_schema_cache.config_fingerprint", return_value="fp"
        ):
            lazy_result.extend(
                mcp._register_from_cache_sync("shared", config, entry)
            )

    def _shutdown():
        with _scoped_to(home_a):
            mcp.shutdown_current_profile_mcp_servers()
        shutdown_finished.set()

    lazy = threading.Thread(target=_lazy, daemon=True)
    lazy.start()
    assert register_entered.wait(timeout=10)
    shutdown = threading.Thread(target=_shutdown, daemon=True)
    shutdown.start()
    assert not shutdown_finished.wait(timeout=0.1), (
        "reload must wait for the lazy lifecycle transaction"
    )

    release_register.set()
    lazy.join(timeout=10)
    shutdown.join(timeout=10)
    assert not lazy.is_alive() and not shutdown.is_alive()
    assert len(lazy_result) == 1
    tool_name = lazy_result[0]

    with _scoped_to(home_a):
        assert registry.get_entry(tool_name) is None
        assert "shared" not in mcp._lazy_server_configs
        assert "shared" not in mcp._server_trust_levels
        assert tool_name not in mcp._mcp_tool_server_names


def test_profile_alias_cleanup_preserves_sibling_alias(profiles):
    """Removing A's MCP toolset alias must not remove B's equal alias."""
    from toolsets import get_toolset

    home_a, home_b = profiles
    scope_a = hermes_home_key(home_a)
    scope_b = hermes_home_key(home_b)
    name = "mcp__shared__ping"

    for scope, label in ((scope_a, "a"), (scope_b, "b")):
        registry.register(
            name=name,
            toolset="mcp-shared",
            schema={"name": name, "description": label},
            handler=lambda _args, value=label: value,
            scope=scope,
        )
        registry.register_toolset_alias(
            "shared", "mcp-shared", scope=scope
        )

    registry.deregister(name, scope=scope_a)

    assert registry.get_toolset_alias_target("shared", scope=scope_a) is None
    assert (
        registry.get_toolset_alias_target("shared", scope=scope_b)
        == "mcp-shared"
    )
    assert registry.dispatch(name, {}, scope=scope_b) == "b"
    with _scoped_to(home_a):
        assert get_toolset("shared") is None
    with _scoped_to(home_b):
        assert get_toolset("shared")["tools"] == [name]

    registry.deregister(name, scope=scope_b)


# ---------------------------------------------------------------------------
# Single-profile: the change must be inert
# ---------------------------------------------------------------------------


def test_single_profile_state_is_one_bucket(profiles):
    """With one profile the containers behave exactly like dict/set."""
    home_a, _ = profiles

    with _scoped_to(home_a):
        mcp._servers["only"] = _fake_server("only")
        mcp._server_connecting.add("pending")
        mcp._server_connect_errors["broken"] = "boom"

        assert dict(mcp._servers).keys() == {"only"}
        assert mcp._servers.get("only").name == "only"
        assert mcp._servers.get("missing") is None
        assert "only" in mcp._servers
        assert len(mcp._servers) == mcp._servers.total_len() == 1
        assert set(mcp._server_connecting) == {"pending"}
        assert mcp._server_connect_errors.pop("broken") == "boom"

        mcp._server_connecting.update({"more"})
        mcp._server_connecting.difference_update({"pending"})
        assert set(mcp._server_connecting) == {"more"}

    # Exactly one bucket was ever created for each container.
    assert len(mcp._servers._by_scope) == 1
    assert len(mcp._server_connecting._by_scope) == 1


def test_single_profile_discovery_slot_is_claimed_once(profiles):
    """Repeat calls under one profile still spawn a single discovery thread."""
    from hermes_cli import mcp_startup

    home_a, _ = profiles
    logger = SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    runs = []
    gate = threading.Event()

    def _fake_discover():
        runs.append(1)
        gate.wait(timeout=5)

    started = mcp_startup._mcp_discovery_started
    threads = mcp_startup._mcp_discovery_threads
    prior_started, prior_threads = set(started), dict(threads)
    started.clear()
    threads.clear()
    try:
        with patch.object(
            mcp_startup, "_has_configured_mcp_servers", return_value=True
        ), patch.object(
            mcp_startup,
            "_discover_mcp_tools_without_interactive_oauth",
            _fake_discover,
        ):
            with _scoped_to(home_a):
                mcp_startup.start_background_mcp_discovery(
                    logger=logger, thread_name="test-1"
                )
                mcp_startup.start_background_mcp_discovery(
                    logger=logger, thread_name="test-2"
                )
                assert len(threads) == 1
        gate.set()
        for thread in list(threads.values()):
            thread.join(timeout=5)
        assert runs == [1]
    finally:
        gate.set()
        for thread in list(threads.values()):
            thread.join(timeout=5)
        started.clear()
        started.update(prior_started)
        threads.clear()
        threads.update(prior_threads)


def test_single_profile_trust_gate_is_unchanged(profiles):
    """The trust tier keeps its documented single-profile semantics."""
    home_a, _ = profiles

    with _scoped_to(home_a):
        mcp._record_tool_trust_metadata(
            "trusted-srv",
            {"trust": "full"},
            [SimpleNamespace(name="anything", annotations=None)],
        )
        assert mcp._trust_gate_check("trusted-srv", "anything") is None

        mcp._record_tool_trust_metadata(
            "untrusted-srv",
            {"trust": "untrusted"},
            [
                SimpleNamespace(
                    name="read_thing", annotations={"readOnlyHint": True}
                ),
                SimpleNamespace(name="write_thing", annotations=None),
            ],
        )
        # readOnlyHint=True still exempts; write-capable still gates.
        assert mcp._trust_gate_check("untrusted-srv", "read_thing") is None
        assert mcp._trust_gate_check("untrusted-srv", "write_thing") is not None
