"""The MCP schema cache is availability, never live connection state.

Two separate claims, both load-bearing for the profile-scoped gateway work:

1. **A stale cache entry must not suppress discovery.** Cache entries are
   keyed by ``config_fingerprint`` (command/args/url/transport/tool filters)
   and, since SEP-2549, expire against the server's own ``ttlMs`` hint. An
   entry that misses on either count must fall through to the normal eager
   connect — not silently register nothing. Both are covered because the live
   Jonathon cache misses on the *second*: its fingerprint matches the current
   config exactly, and only ``ttl_ms: 0`` makes it unusable.

2. **A cache HIT is not a connection.** ``_register_from_cache_sync`` makes a
   server's tools callable without spawning or connecting it, so
   ``get_mcp_status`` must still report it unconnected while
   ``get_registered_mcp_server_names`` reports it available. Conflating the
   two is what made ``start_background_mcp_discovery``'s retry allowance
   re-run a full discovery pass (with a WARNING) on every agent build for a
   lazily-registered profile.

Temp profile homes and a recorded connect step throughout: no network, no real
MCP server, no live cache touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import mcp_startup
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_profile, mcp_schema_cache, mcp_tool
from tools.registry import registry

_SERVER = "toolhive"
_URL_V3 = "https://toolhive.example/mcp"
_URL_V2 = "https://old-toolhive.example/mcp"


def _write_profile(home: Path, url: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"""
mcp_servers:
  {_SERVER}:
    url: {url}
    lazy: true
""".lstrip(),
        encoding="utf-8",
    )


def _cache_tools() -> list[dict]:
    return [
        {
            "name": "ping",
            "description": "cached tool",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        }
    ]


@pytest.fixture(autouse=True)
def _clean_registry():
    mcp_profile.reset_all_registries()
    try:
        yield
    finally:
        mcp_profile.reset_all_registries()
        for tool_name in list(registry.get_all_tool_names()):
            if tool_name.startswith("mcp__"):
                registry.deregister(tool_name)


@pytest.fixture
def profile(tmp_path: Path):
    """A temp profile home bound as HERMES_HOME for the duration of a test."""
    home = tmp_path / "jonathon"
    _write_profile(home, _URL_V3)
    token = set_hermes_home_override(str(home))
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def connects(monkeypatch) -> list[str]:
    """Record which servers reached the eager connect path; never connect."""
    attempted: list[str] = []

    async def _fake_discover_and_register(name: str, config: dict):
        attempted.append(name)
        return []

    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(
        mcp_tool, "_discover_and_register_server", _fake_discover_and_register
    )
    return attempted


def test_stale_cache_entry_does_not_suppress_discovery(profile, connects):
    """A v2-era entry (different config fingerprint) must miss and fall through."""
    mcp_schema_cache.write_cache_entry(
        _SERVER,
        mcp_schema_cache.config_fingerprint({"url": _URL_V2}),
        tools=_cache_tools(),
    )

    servers = mcp_tool._load_mcp_config()
    assert servers[_SERVER]["url"] == _URL_V3
    mcp_tool.register_mcp_servers(servers)

    assert connects == [_SERVER], "stale cache swallowed the discovery attempt"
    assert _SERVER not in mcp_profile.current_registry().lazy_server_configs
    assert not any(
        name.startswith(f"mcp__{_SERVER}__") for name in registry.get_all_tool_names()
    )


def test_ttl_expired_entry_does_not_suppress_discovery(profile, connects):
    """The shape the live ToolHive cache actually has: matching fp, ``ttl_ms: 0``.

    SEP-2549 lets a server return ``ttlMs`` on ``tools/list``; ToolHive returns
    ``0``, i.e. "do not reuse this". ``get_cached_entry`` honours that, so the
    entry is a permanent miss even though its fingerprint is current — the
    server must be re-probed live every time. Worth pinning separately from the
    fingerprint case: the on-disk file looks like a usable 24-tool cache, and
    only the TTL says otherwise.
    """
    servers = mcp_tool._load_mcp_config()
    mcp_schema_cache.write_cache_entry(
        _SERVER,
        mcp_schema_cache.config_fingerprint(servers[_SERVER]),
        tools=_cache_tools(),
        ttl_ms=0,
    )

    mcp_tool.register_mcp_servers(servers)

    assert connects == [_SERVER], "an expired entry was served as if it were fresh"
    assert not any(
        name.startswith(f"mcp__{_SERVER}__") for name in registry.get_all_tool_names()
    )


def test_cache_hit_registers_tools_without_claiming_a_connection(profile, connects):
    """Cached schemas make tools callable; they must not fake connected state."""
    servers = mcp_tool._load_mcp_config()
    mcp_schema_cache.write_cache_entry(
        _SERVER,
        mcp_schema_cache.config_fingerprint(servers[_SERVER]),
        tools=_cache_tools(),
    )

    mcp_tool.register_mcp_servers(servers)

    assert connects == [], "a valid cache entry must not spawn the server"
    assert f"mcp__{_SERVER}__ping" in registry.get_all_tool_names()

    status = {entry["name"]: entry for entry in mcp_tool.get_mcp_status()}
    assert status[_SERVER]["connected"] is False
    assert status[_SERVER]["status"] == "configured"
    assert mcp_tool.get_registered_mcp_server_names() == {_SERVER}


def test_retry_gate_counts_cache_backed_availability(profile, connects):
    """The zero-result retry must not re-run discovery for a lazy profile.

    ``get_mcp_status`` reports a cache-registered server as ``configured``, so
    a connection-only probe read "discovery produced nothing" and re-ran the
    whole pass — plus a WARNING — on every agent build.
    """
    servers = mcp_tool._load_mcp_config()
    assert mcp_startup._profile_mcp_is_populated() is False

    mcp_schema_cache.write_cache_entry(
        _SERVER,
        mcp_schema_cache.config_fingerprint(servers[_SERVER]),
        tools=_cache_tools(),
    )
    mcp_tool.register_mcp_servers(servers)

    assert mcp_startup._profile_mcp_is_populated() is True


def test_cache_is_read_from_the_active_profiles_home(tmp_path):
    """Two profiles' caches never alias: the path follows HERMES_HOME."""
    home_a = tmp_path / "jonathon"
    home_b = tmp_path / "carol"
    _write_profile(home_a, _URL_V3)
    _write_profile(home_b, _URL_V3)
    fingerprint = mcp_schema_cache.config_fingerprint({"url": _URL_V3})

    token = set_hermes_home_override(str(home_a))
    try:
        mcp_schema_cache.write_cache_entry(
            _SERVER, fingerprint, tools=_cache_tools()
        )
        assert mcp_schema_cache.get_cached_entry(_SERVER, fingerprint) is not None
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(home_b))
    try:
        assert mcp_schema_cache.get_cached_entry(_SERVER, fingerprint) is None
    finally:
        reset_hermes_home_override(token)

    assert (home_a / "cache" / "mcp_schema_cache.json").exists()
    assert not (home_b / "cache" / "mcp_schema_cache.json").exists()
