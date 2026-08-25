"""reload.mcp revision-aware coalescing (review on #20379, finding 1).

The TUI's config poll sends the ``mcp_rev`` it observed with each reload
request. The server must guarantee that a success response means THAT
revision (or a newer one) was actually loaded:

- The leader re-hashes the MCP-relevant config after discovery and repeats
  until the hash is stable, so a config edit racing a slow reload can't be
  silently skipped.
- A follower that waited behind a leader coalesces only when the leader's
  loaded revision matches the follower's requested revision; otherwise it
  re-runs the full reload itself.
- A failed reload returns a JSON-RPC error and does not advance the
  generation, so a follower behind a failed leader re-runs too.

Each test file runs in its own subprocess (run_tests.sh isolation), but the
fixtures still restore the module globals they touch.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool
import tui_gateway.server as srv
from hermes_constants import (
    hermes_home_key,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.registry import registry


@pytest.fixture()
def reload_env(monkeypatch):
    """Neutralize side effects and expose call-counting fakes."""
    calls = {"discover": 0, "shutdown": 0}
    rev_box = {"rev": "rev-a"}

    monkeypatch.setattr(mcp_tool, "shutdown_current_profile_mcp_servers", lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1))
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda: calls.__setitem__("discover", calls["discover"] + 1))
    monkeypatch.setattr(srv, "_compute_mcp_rev", lambda: rev_box["rev"])

    saved = (
        srv._mcp_reload_gen,
        srv._mcp_reload_loaded_rev,
        srv._mcp_reload_loaded_scope,
    )
    srv._mcp_reload_gen = 0
    srv._mcp_reload_loaded_rev = ""
    srv._mcp_reload_loaded_scope = ""
    yield calls, rev_box
    (
        srv._mcp_reload_gen,
        srv._mcp_reload_loaded_rev,
        srv._mcp_reload_loaded_scope,
    ) = saved


def _reload(rev: str | None = None, rid: int = 1) -> dict:
    params: dict = {"session_id": "no-such-session", "confirm": True}
    if rev is not None:
        params["rev"] = rev
    return srv._methods["reload.mcp"](rid, params)


def test_success_reports_loaded_rev(reload_env):
    calls, rev_box = reload_env
    rev_box["rev"] = "rev-a"

    envelope = _reload(rev="rev-a")

    assert envelope["result"]["status"] == "reloaded"
    assert envelope["result"]["loaded_rev"] == "rev-a"
    assert calls["discover"] == 1
    assert srv._mcp_reload_gen == 1


def test_failed_reload_is_an_error_and_no_generation_advance(reload_env, monkeypatch):
    """The exact client-facing contract: a failure must NOT look like an ack.
    quietRpc on the TUI side collapses this error to null and keeps the
    revision un-accepted, so the next poll retries."""
    calls, _ = reload_env

    def _boom():
        raise RuntimeError("flapping server")

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", _boom)

    envelope = _reload(rev="rev-b")

    assert "error" in envelope
    assert srv._mcp_reload_gen == 0
    assert srv._mcp_reload_loaded_rev == ""


def test_leader_rehashes_until_stable_when_config_changes_mid_reload(reload_env, monkeypatch):
    """Revision A starts a reload; the config changes to revision B while
    discovery is connecting servers. The leader must not mark A complete —
    it re-hashes after discovery and reloads again until stable, so the
    reported loaded_rev is what discovery actually read."""
    calls, _ = reload_env

    # Hash sequence: before pass 1 → rev-a; after pass 1 → rev-b (config
    # changed mid-discovery); after pass 2 → rev-b (stable).
    hashes = iter(["rev-a", "rev-b", "rev-b"])
    monkeypatch.setattr(srv, "_compute_mcp_rev", lambda: next(hashes))

    envelope = _reload(rev="rev-a")

    assert envelope["result"]["loaded_rev"] == "rev-b"
    assert calls["discover"] == 2  # pass 1 read stale config, pass 2 converged
    assert srv._mcp_reload_gen == 1


class _WaiterLock:
    """Lock wrapper that signals when a FOLLOWER enters the blocking ``with``
    path. The handler snapshots ``gen_before`` on the line before ``with``,
    so once ``waiting`` fires the snapshot is already taken and it is safe to
    let the leader complete — no sleep-based ordering."""

    def __init__(self):
        self._lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True) -> bool:
        return self._lock.acquire(blocking)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self):
        self.waiting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _run_leader_follower(reload_env, monkeypatch, follower_rev):
    """Drive the A-then-B overlap deterministically: the leader blocks inside
    discovery until the follower is queued on the lock, then completes."""
    calls, _rev_box = reload_env

    lock = _WaiterLock()
    monkeypatch.setattr(srv, "_mcp_reload_lock", lock)

    leader_in_discovery = threading.Event()
    release_leader = threading.Event()

    def _slow_discover():
        calls["discover"] += 1
        if calls["discover"] == 1:
            leader_in_discovery.set()
            assert release_leader.wait(timeout=10)

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", _slow_discover)

    results: dict = {}

    lt = threading.Thread(target=lambda: results.__setitem__("leader", _reload(rev="rev-a", rid=1)), daemon=True)
    lt.start()
    assert leader_in_discovery.wait(timeout=10)

    ft = threading.Thread(target=lambda: results.__setitem__("follower", _reload(rev=follower_rev, rid=2)), daemon=True)
    ft.start()
    # The follower has snapshotted gen_before once it blocks on the lock.
    assert lock.waiting.wait(timeout=10)
    release_leader.set()

    lt.join(timeout=10)
    ft.join(timeout=10)
    assert not lt.is_alive() and not ft.is_alive()

    return results, calls


def test_follower_with_matching_rev_coalesces(reload_env, monkeypatch):
    results, calls = _run_leader_follower(reload_env, monkeypatch, follower_rev="rev-a")

    assert results["leader"]["result"]["status"] == "reloaded"
    assert results["follower"]["result"]["status"] == "reloaded"
    assert results["follower"]["result"].get("coalesced") is True
    # Only the leader ran discovery.
    assert calls["discover"] == 1


def test_legacy_request_without_rev_still_coalesces_on_generation(reload_env, monkeypatch):
    """Manual /reload-mcp and older clients send no rev — generation-only
    coalescing (the pre-existing contract) still applies."""
    results, calls = _run_leader_follower(reload_env, monkeypatch, follower_rev=None)  # type: ignore[arg-type]

    assert results["follower"]["result"].get("coalesced") is True
    assert calls["discover"] == 1


def test_same_revision_does_not_coalesce_across_profiles(
    reload_env, monkeypatch, tmp_path
):
    """An equal config hash is not proof that two profiles share MCP state."""
    calls, _ = reload_env
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    srv._sessions["session-a"] = {
        "profile_home": str(home_a),
        "agent": SimpleNamespace(),
    }
    srv._sessions["session-b"] = {
        "profile_home": str(home_b),
        "agent": SimpleNamespace(),
    }
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_session_info", lambda *_a, **_k: {})
    monkeypatch.setattr(srv, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(mcp_tool, "refresh_agent_mcp_tools", lambda *_a, **_k: set())

    lock = _WaiterLock()
    monkeypatch.setattr(srv, "_mcp_reload_lock", lock)
    leader_in_discovery = threading.Event()
    release_leader = threading.Event()
    discovered_scopes = []

    def _discover():
        calls["discover"] += 1
        discovered_scopes.append(mcp_tool._mcp_scope_key())
        if calls["discover"] == 1:
            leader_in_discovery.set()
            assert release_leader.wait(timeout=10)

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", _discover)

    def _call(sid, rid):
        return srv._methods["reload.mcp"](
            rid,
            {"session_id": sid, "confirm": True, "rev": "rev-a"},
        )

    results = {}
    leader = threading.Thread(
        target=lambda: results.__setitem__("a", _call("session-a", 1)),
        daemon=True,
    )
    follower = threading.Thread(
        target=lambda: results.__setitem__("b", _call("session-b", 2)),
        daemon=True,
    )
    try:
        leader.start()
        assert leader_in_discovery.wait(timeout=10)
        follower.start()
        assert lock.waiting.wait(timeout=10)
        release_leader.set()
        leader.join(timeout=10)
        follower.join(timeout=10)
        assert not leader.is_alive() and not follower.is_alive()

        assert results["a"]["result"]["status"] == "reloaded"
        assert results["b"]["result"]["status"] == "reloaded"
        assert results["b"]["result"].get("coalesced") is not True
        assert discovered_scopes == [
            hermes_home_key(home_a),
            hermes_home_key(home_b),
        ]
    finally:
        release_leader.set()
        srv._sessions.pop("session-a", None)
        srv._sessions.pop("session-b", None)


class _ProfileServer:
    """Small live-server witness with the real registry cleanup contract."""

    def __init__(self, name: str, scope: str, tool_name: str):
        self.name = name
        self.session = object()
        self._registered_scope = scope
        self._registered_tool_names = [tool_name]
        self.shutdown_calls = 0

    def _deregister_tools(self):
        for tool_name in list(self._registered_tool_names):
            registry.deregister(tool_name, scope=self._registered_scope)
            mcp_tool._forget_mcp_tool_server(tool_name)
        self._registered_tool_names = []

    async def shutdown(self):
        self.shutdown_calls += 1
        self._deregister_tools()
        self.session = None


def _under_profile(home, callback):
    token = set_hermes_home_override(str(home))
    try:
        return callback()
    finally:
        reset_hermes_home_override(token)


def _install_profile_server(home, label: str):
    scope = hermes_home_key(home)
    tool_name = "mcp__shared_ping"
    server = _ProfileServer("shared", scope, tool_name)
    registry.register(
        name=tool_name,
        toolset="mcp-shared",
        schema={
            "name": tool_name,
            "description": "profile ownership witness",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda: label,
        scope=scope,
    )
    mcp_tool._servers["shared"] = server
    mcp_tool._mcp_tool_server_names[tool_name] = "shared"
    mcp_tool._server_trust_levels["shared"] = (
        mcp_tool._TRUST_UNTRUSTED if label == "B" else mcp_tool._TRUST_FULL
    )
    return server, tool_name


def test_reload_mcp_preserves_sibling_profile_runtime(tmp_path, monkeypatch):
    """The real reload.mcp route for A must leave B callable and unchanged."""
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    (home_a / "config.yaml").write_text("mcp_servers: {}\n")
    (home_b / "config.yaml").write_text("mcp_servers: {}\n")

    mcp_tool._ensure_mcp_loop()
    server_a, tool_name = _under_profile(
        home_a, lambda: _install_profile_server(home_a, "A")
    )
    server_b, _ = _under_profile(
        home_b, lambda: _install_profile_server(home_b, "B")
    )
    b_session = server_b.session

    srv._sessions["reload-a"] = {
        "profile_home": str(home_a),
        "agent": SimpleNamespace(),
    }
    monkeypatch.setattr(srv, "_compute_mcp_rev", lambda: "same-rev")
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_session_info", lambda *_a, **_k: {})
    monkeypatch.setattr(srv, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(mcp_tool, "refresh_agent_mcp_tools", lambda *_a, **_k: set())

    discovered_scopes = []
    replacement = {}

    def _discover_a():
        discovered_scopes.append(mcp_tool._mcp_scope_key())
        replacement["server"], _ = _install_profile_server(home_a, "A2")
        return [tool_name]

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", _discover_a)

    try:
        before_b_entry = _under_profile(
            home_b, lambda: registry.get_entry(tool_name)
        )
        assert before_b_entry is not None
        assert before_b_entry.handler() == "B"

        envelope = srv._methods["reload.mcp"](
            77,
            {"session_id": "reload-a", "confirm": True, "rev": "same-rev"},
        )
        assert envelope["result"]["status"] == "reloaded"
        assert discovered_scopes == [hermes_home_key(home_a)]

        def _assert_b_survived():
            assert mcp_tool._servers["shared"] is server_b
            assert server_b.session is b_session
            assert server_b.shutdown_calls == 0
            assert mcp_tool._server_trust_levels["shared"] == mcp_tool._TRUST_UNTRUSTED
            assert mcp_tool._mcp_tool_server_names[tool_name] == "shared"
            entry = registry.get_entry(tool_name)
            assert entry is before_b_entry
            assert entry.handler() == "B"

        _under_profile(home_b, _assert_b_survived)

        def _assert_a_reloaded():
            assert server_a.shutdown_calls == 1
            assert server_a.session is None
            assert mcp_tool._servers["shared"] is replacement["server"]
            assert registry.get_entry(tool_name).handler() == "A2"

        _under_profile(home_a, _assert_a_reloaded)
    finally:
        srv._sessions.pop("reload-a", None)
        _under_profile(home_a, mcp_tool.shutdown_current_profile_mcp_servers)
        _under_profile(home_b, mcp_tool.shutdown_current_profile_mcp_servers)
