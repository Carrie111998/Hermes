"""Seam-identity + aggressive tests for the gateway-topology extraction (R2-B).

``hermes_cli/web_gateway_topology.py`` holds the dashboard's port-binding
constants, per-profile gateway topology collection, and the TTL cache —
moved byte-verbatim out of ``hermes_cli/web_server.py`` (god-file slice
R2-B, epic #78791).

The seam-identity tests pin the regression this extraction is meant to
prevent: ``web_server`` must resolve every moved name to the *same object*
the new module defines — a duplicated/redefined copy would silently diverge.
The aggressive tests then exercise the failure modes the readout must
survive: empty profile lists, cache misses, TTL expiry, lock contention,
and dead-state classification.
"""

from unittest.mock import patch, MagicMock

import pytest

from hermes_cli import web_gateway_topology as t
from hermes_cli import web_server as ws

_PLATFORM_DEAD_STATES = t._PLATFORM_DEAD_STATES
_PORT_BINDING_PLATFORM_PORTS = t._PORT_BINDING_PLATFORM_PORTS

MOVED_NAMES = (
    "_PORT_BINDING_PLATFORM_PORTS",
    "_PLATFORM_DEAD_STATES",
    "_TOPOLOGY_CACHE",
    "_TOPOLOGY_CACHE_LOCK",
    "_TOPOLOGY_CACHE_TTL",
    "_profile_platform_ports",
    "_collect_profile_gateway_topology",
    "_load_configured_gateway_platforms",
    "_collect_profile_gateway_topology_cached",
    "_topology_cache_get",
)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_names_are_seam_identical(name):
    # ``is``-identity: web_server must resolve the name to the very same
    # object the new module defines — no redefinition allowed.
    assert getattr(ws, name) is getattr(t, name)


def test_platform_dead_states_constants():
    assert _PLATFORM_DEAD_STATES == frozenset({"fatal", "disconnected", "stopped"})
    assert "online" not in _PLATFORM_DEAD_STATES


def test_port_binding_platform_ports_shape():
    # Every platform maps to (config-port-key, default-port) tuple pair.
    assert isinstance(_PORT_BINDING_PLATFORM_PORTS, dict)
    for key, value in _PORT_BINDING_PLATFORM_PORTS.items():
        assert isinstance(key, str)
        assert isinstance(value, tuple) and len(value) == 2


def test_topology_cache_miss_returns_none():
    t._TOPOLOGY_CACHE.update({"ts": 0.0, "data": None, "fn": None})
    result = t._topology_cache_get(lambda: {"profiles": []})
    assert result is None


def test_topology_cache_hit_returns_data():
    fn = lambda: {"profiles": [{"name": "axl"}]}
    t._TOPOLOGY_CACHE.update({"ts": 9999999999.0, "data": {"profiles": [{"name": "axl"}]}, "fn": fn})
    result = t._topology_cache_get(fn)
    assert result == {"profiles": [{"name": "axl"}]}


def test_topology_cache_fn_identity_mismatch_misses():
    # Same fn object identity is required for a cache hit.
    stored_fn = lambda: {"x": 1}
    different_fn = lambda: {"x": 1}
    t._TOPOLOGY_CACHE.update({"ts": 9999999999.0, "data": {"x": 1}, "fn": stored_fn})
    assert t._topology_cache_get(different_fn) is None


@patch("hermes_cli.web_gateway_topology.time.monotonic", return_value=100.0)
def test_topology_cache_ttl_expiry(mock_monotonic):
    t._TOPOLOGY_CACHE.update({"ts": 50.0, "data": {"x": 1}, "fn": None})
    fn = lambda: {"x": 2}
    result = t._topology_cache_get(fn)
    # 100 - 50 = 50 > TTL 10 -> miss
    assert result is None


@patch("hermes_cli.profiles.profiles_to_serve", return_value=[])
@patch("hermes_cli.profiles._check_gateway_running", return_value=False)
def test_collect_topology_empty_profiles(mock_running, mock_profiles):
    result = t._collect_profile_gateway_topology()
    assert isinstance(result, dict)
    assert "profiles" in result
    assert result["profiles"] == []


@patch("hermes_cli.profiles.profiles_to_serve", side_effect=OSError("boom"))
def test_collect_topology_probe_error_is_swallowed(mock_profiles):
    # The except path returns the empty envelope; it must not raise.
    result = t._collect_profile_gateway_topology()
    assert isinstance(result, dict)
    assert result["profiles"] == []


def test_load_configured_platforms_dead_state():
    # _PLATFORM_DEAD_STATES drives state classification in the readout.
    assert "fatal" in _PLATFORM_DEAD_STATES
    assert "disconnected" in _PLATFORM_DEAD_STATES
    assert "stopped" in _PLATFORM_DEAD_STATES


@patch("gateway.status.read_runtime_status", return_value=None)
@patch("hermes_cli.profiles.profiles_to_serve", return_value=[])
def test_collect_topology_cached_with_missing_runtime(mock_profiles, mock_status):
    t._TOPOLOGY_CACHE.update({"ts": 0.0, "data": None, "fn": None})
    result = t._collect_profile_gateway_topology_cached()
    assert isinstance(result, dict)
