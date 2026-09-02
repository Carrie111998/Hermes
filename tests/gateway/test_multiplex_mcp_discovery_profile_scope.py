"""A multiplexed gateway turn must discover ITS OWN profile's MCP servers.

Live failure this reproduces (Aug 2026, ``hermes-gateway.service``): a fresh
Matrix session on profile ``jonathon`` exposed no ToolHive tool AND reported no
connection error, and the journal carried no ToolHive discovery line at all.

Why: the messaging gateway runs MCP discovery exactly ONCE, in
``gateway.run.start_gateway`` (``await loop.run_in_executor(None,
discover_mcp_tools)``), under the LAUNCH profile's ``HERMES_HOME``. That reads
``~/.hermes/config.yaml``'s ``mcp_servers`` and nothing else. The per-turn path
--- ``GatewayRunner._run_agent`` → ``_profile_runtime_scope`` →
``_run_agent_inner`` → ``TurnRunner.run_sync`` → ``AIAgent(...)`` --- never ran
discovery for the profile the turn resolved to.

Before the profile-scoped registry (``tools/mcp_profile.py``) that was merely
*wrong* (every profile shared the launch profile's connections); afterwards it
is *silent*: the launch profile's servers live in the launch profile's
registry, so ``_make_check_fn``'s availability probe reads the selected
profile's (empty) registry and filters the tools out of the snapshot without
ever attempting a connection. Hence "no tool, no error, no log line".

The TUI/desktop surface never showed this because it starts discovery from
``tui_gateway.entry.ensure_mcp_discovery_started`` inside
``server._start_agent_build`` --- which is exactly what
``tests/hermes_cli/test_mcp_startup_profile_scope.py`` drives. The messaging
gateway has no such call, so those tests passed while the live path failed.

Everything below runs against temp profile homes and a deterministic fake
discovery; no real credentials, no network, no live state.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest import mock

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from hermes_cli import mcp_startup
from tools import mcp_profile, mcp_tool
from tools.registry import registry

# Both profiles configure the SAME logical server name: the key that used to
# collapse, and the reason "some profile discovered toolhive" is not the same
# claim as "THIS profile discovered toolhive".
_SERVER = "toolhive"
_TOOL = f"mcp__{_SERVER}__ping"
_TOOLSET = f"mcp-{_SERVER}"

_JOIN_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_profile(home: Path, url: str) -> None:
    """A realistic profile home: config.yaml only, no .env, no secrets."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"""
mcp_servers:
  {_SERVER}:
    url: {url}
    connect_timeout: 1
""".lstrip(),
        encoding="utf-8",
    )


def _key(home: Path) -> str:
    return mcp_profile.canonical_profile_key(str(home))


def _join_all_discovery(timeout: float = _JOIN_TIMEOUT_S) -> None:
    for thread in mcp_startup.discovery_threads():
        thread.join(timeout=timeout)


class _FakeDiscovery:
    """Stands in for ``tools.mcp_tool.discover_mcp_tools``.

    Real up to the transport: it loads the ambient profile's MCP config through
    the REAL ``_load_mcp_config`` (so a wrong ``HERMES_HOME`` or a missing
    secret scope shows up here), then registers one tool per configured server
    into the REAL process-global tool registry behind the REAL profile-scoped
    ``_make_check_fn`` availability probe.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: list[tuple[str, dict]] = []
        self.fail_for: set[str] = set()

    def __call__(self) -> list[str]:
        key = mcp_profile.current_profile_key()
        servers = mcp_tool._load_mcp_config()
        with self.lock:
            self.seen.append((key, dict(servers.get(_SERVER) or {})))
        if key in self.fail_for:
            raise RuntimeError("simulated discovery failure")
        names: list[str] = []
        reg = mcp_profile.current_registry()
        for name, cfg in servers.items():
            # Mirror the lazy-registration path: the server is *available* to
            # this profile (first call would connect it), which is what
            # ``_make_check_fn`` reports.
            reg.lazy_server_configs[name] = dict(cfg)
            tool_name = f"mcp__{name}__ping"
            registry.register(
                name=tool_name,
                toolset=f"mcp-{name}",
                schema={
                    "name": tool_name,
                    "description": f"Fake tool from MCP server '{name}'",
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=lambda **_kw: "",
                check_fn=mcp_tool._make_check_fn(name),
                is_async=False,
                description=f"Fake tool from MCP server '{name}'",
            )
            # What makes `mcp-<server>` reachable from get_all_toolsets(), so
            # the tool survives an unrestricted snapshot (real path does this).
            registry.register_toolset_alias(name, f"mcp-{name}")
            mcp_tool._track_mcp_tool_server(tool_name, name)
            names.append(tool_name)
        return names

    def configs_for(self, home: Path) -> list[dict]:
        with self.lock:
            return [cfg for key, cfg in self.seen if key == _key(home)]

    def urls_for(self, home: Path) -> list[str]:
        return [cfg.get("url") for cfg in self.configs_for(home)]


def _visible_tools() -> set[str]:
    """The tool catalog AIAgent would snapshot under the ambient profile scope.

    ``skip_tool_search_assembly=True`` reads the real, availability-filtered
    catalog rather than the tool_search/tool_describe bridge the assembly step
    may collapse it into — the collapse is an orthogonal presentation layer
    that still routes to these same tools, so it would hide the very fact
    under test.
    """
    from model_tools import get_tool_definitions

    return {
        t["function"]["name"]
        for t in get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }


def _make_runner(*, multiplex: bool) -> GatewayRunner:
    """A bare runner carrying only what the profile-scoping wrapper needs.

    ``set_multiplex_active`` mirrors ``GatewayRunner.__init__`` (which
    ``__new__`` skips): it is the flag ``registry.check_fn_cache_scope`` reads
    to decide whether tool-availability caching is profile-keyed. Without it a
    process-wide check_fn cache would let one profile's ``mcp__…`` entry
    answer for every other profile — the isolation this suite asserts.
    """
    from agent.secret_scope import set_multiplex_active

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    set_multiplex_active(multiplex)
    return runner


def _drive_turn(runner: GatewayRunner, profile_home: Path | None) -> dict:
    """Run the REAL ``GatewayRunner._run_agent`` for one turn.

    ``_run_agent_inner`` stands in for everything downstream of the profile
    scope and records what an ``AIAgent`` built at that point would see.
    """
    observed: dict = {}

    async def _inner(*_a, **_kw):
        from hermes_constants import get_hermes_home

        observed["home"] = str(get_hermes_home())
        observed["tools"] = _visible_tools()
        return {"final_response": "ok"}

    runner._run_agent_inner = _inner
    source = mock.MagicMock()
    source.profile = profile_home.name if profile_home is not None else None

    async def _go():
        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=profile_home,
        ):
            await runner._run_agent("hi", "", [], source, "sid")

    try:
        asyncio.run(_go())
    finally:
        try:
            runner._shutdown_executor()
            # The executor is per-event-loop, but the RUNNER outlives every
            # turn in a live gateway — and carries the per-profile MCP
            # readiness floor. Re-open it so a test can drive two turns
            # through one runner the way the gateway does.
            runner._executor_closing = False
        except Exception:
            pass
    _join_all_discovery()
    return observed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_mcp_state():
    """Leave no coordinator entry, profile registry, or tool registration."""
    from agent.secret_scope import is_multiplex_active, set_multiplex_active

    was_multiplex = is_multiplex_active()
    mcp_startup.reset_discovery_state()
    mcp_profile.reset_all_registries()
    try:
        yield
    finally:
        _join_all_discovery()
        mcp_startup.reset_discovery_state()
        mcp_profile.reset_all_registries()
        for tool_name in list(registry.get_all_tool_names()):
            if tool_name.startswith("mcp__"):
                registry.deregister(tool_name)
        set_multiplex_active(was_multiplex)


@pytest.fixture
def discovery(monkeypatch) -> _FakeDiscovery:
    fake = _FakeDiscovery()
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", fake)
    return fake


@pytest.fixture
def profiles(tmp_path: Path) -> tuple[Path, Path]:
    home_a = tmp_path / "jonathon"
    home_b = tmp_path / "carol"
    _write_profile(home_a, "https://toolhive-a.example/mcp")
    _write_profile(home_b, "https://toolhive-b.example/mcp")
    return home_a, home_b


# ---------------------------------------------------------------------------
# 1. The live failure
# ---------------------------------------------------------------------------


def test_multiplexed_turn_discovers_the_selected_profiles_mcp_config(
    profiles, discovery
):
    """The reported bug: a Matrix turn for a profile never ran ITS discovery."""
    home_a, _home_b = profiles

    observed = _drive_turn(_make_runner(multiplex=True), home_a)

    assert discovery.urls_for(home_a) == ["https://toolhive-a.example/mcp"], (
        "the turn's profile scope never reached MCP discovery — this is the "
        "live 'no tool, no error, no log line' failure"
    )
    assert observed["home"] == str(home_a)


def test_profile_mcp_tool_reaches_the_agent_tool_snapshot(profiles, discovery):
    """Registration alone is not enough: the tool must survive the snapshot."""
    home_a, _home_b = profiles

    observed = _drive_turn(_make_runner(multiplex=True), home_a)

    assert _TOOL in observed["tools"]


# ---------------------------------------------------------------------------
# 2. Isolation: the fix must not re-open the cross-profile hole
# ---------------------------------------------------------------------------


def test_two_profiles_with_one_server_name_discover_independently(
    profiles, discovery
):
    home_a, home_b = profiles

    _drive_turn(_make_runner(multiplex=True), home_a)
    _drive_turn(_make_runner(multiplex=True), home_b)

    assert discovery.urls_for(home_a) == ["https://toolhive-a.example/mcp"]
    assert discovery.urls_for(home_b) == ["https://toolhive-b.example/mcp"]


def test_a_profile_that_never_discovered_sees_no_mcp_tool(profiles, discovery):
    """``mcp__toolhive__ping`` is ONE process-global registry entry.

    Profile A registering it must not hand profile B a tool backed by A's
    connection — the availability probe is what keeps the two apart.
    """
    home_a, home_b = profiles

    _drive_turn(_make_runner(multiplex=True), home_a)

    # B's turn runs with discovery disabled so only A's registration exists.
    discovery.fail_for.add(_key(home_b))
    observed_b = _drive_turn(_make_runner(multiplex=True), home_b)

    assert _TOOL not in observed_b["tools"]


def test_failed_profile_does_not_suppress_a_healthy_profile(profiles, discovery):
    home_a, home_b = profiles
    discovery.fail_for.add(_key(home_a))

    _drive_turn(_make_runner(multiplex=True), home_a)
    observed_b = _drive_turn(_make_runner(multiplex=True), home_b)

    assert _TOOL in observed_b["tools"]
    assert discovery.urls_for(home_b) == ["https://toolhive-b.example/mcp"]


# ---------------------------------------------------------------------------
# 3. Cost / compatibility
# ---------------------------------------------------------------------------


def test_repeated_turns_for_one_profile_do_not_respawn_discovery(
    profiles, discovery
):
    home_a, _home_b = profiles
    runner = _make_runner(multiplex=True)

    _drive_turn(runner, home_a)
    _drive_turn(_make_runner(multiplex=True), home_a)

    assert len(discovery.configs_for(home_a)) == 1


def test_single_profile_gateway_is_unchanged(profiles, discovery):
    """Multiplexing off is a pass-through: no scope, no extra discovery."""
    home_a, _home_b = profiles

    _drive_turn(_make_runner(multiplex=False), home_a)

    assert discovery.seen == []


# ---------------------------------------------------------------------------
# 4. Startup warm-up + failure visibility
# ---------------------------------------------------------------------------


def test_startup_warms_every_served_profile_except_the_active_one(
    profiles, discovery
):
    """``start_gateway()`` covers the active profile; the rest start here.

    Without this the FIRST inbound message for a served profile is the one
    that pays for discovery, so a server slower than ``mcp_discovery_timeout``
    misses that turn's tool snapshot.
    """
    home_a, home_b = profiles
    runner = _make_runner(multiplex=True)

    runner._warm_profile_mcp_discovery(
        [("jonathon", home_a), ("carol", home_b)], active="carol"
    )
    _join_all_discovery()

    assert discovery.urls_for(home_a) == ["https://toolhive-a.example/mcp"]
    assert discovery.configs_for(home_b) == []


def test_discovery_failure_is_reported_against_its_profile(
    profiles, discovery, caplog
):
    """A failed profile must be diagnosable, not silently MCP-less.

    The live symptom was a profile with no MCP tool, no error and no log line;
    an unattributed "zero servers" warning would not have been actionable
    either, so the profile home has to be in the message.
    """
    home_a, _home_b = profiles
    discovery.fail_for.add(_key(home_a))

    with caplog.at_level("WARNING", logger="gateway.run"), caplog.at_level(
        "WARNING", logger="hermes_cli.mcp_startup"
    ):
        _drive_turn(_make_runner(multiplex=True), home_a)

    assert any(str(home_a) in record.getMessage() for record in caplog.records), (
        f"no profile-attributed MCP warning; saw: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
