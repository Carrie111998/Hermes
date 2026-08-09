"""MCP servers read the Desktop connection mode from per-call ``_meta`` (#82140).

An MCP server can't read the gateway's contextvars, and its stdio env is fixed
at spawn time while the mode is per-session — one gateway can serve a local
Desktop client and a remote one at once. Per-call ``_meta`` is the only vehicle
that is both live and session-correct.
"""

import pytest

import gateway.session_context as sc
from gateway.session_context import set_desktop_connection_mode
from tools.mcp_tool import (
    MCP_DESKTOP_CONNECTION_MODE_META_KEY as META_KEY,
    _call_tool_meta,
    _call_tool_supports_meta,
)


@pytest.fixture(autouse=True)
def _reset_mode():
    saved = sc._DESKTOP_CONNECTION_MODE.get()
    sc._DESKTOP_CONNECTION_MODE.set(sc._UNSET)
    try:
        yield
    finally:
        sc._DESKTOP_CONNECTION_MODE.set(saved)


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_bound_mode_becomes_call_meta(mode):
    set_desktop_connection_mode(mode)
    assert _call_tool_meta() == {META_KEY: mode}


def test_remote_like_saved_mode_is_normalized_before_it_leaves():
    set_desktop_connection_mode("cloud")
    assert _call_tool_meta() == {META_KEY: "remote"}


def test_non_desktop_session_sends_no_meta():
    """CLI/TUI/messaging requests keep exactly today's shape."""
    assert _call_tool_meta() is None


def test_meta_carries_the_mode_and_nothing_else():
    """No base URL, host, token, SSH key, or auth mode may ride along."""
    set_desktop_connection_mode("remote")
    meta = _call_tool_meta()
    assert list(meta) == [META_KEY]
    assert meta[META_KEY] in {"local", "remote"}


def test_meta_key_does_not_squat_the_reserved_spec_prefix():
    """MCP reserves `modelcontextprotocol.io/` for the spec itself."""
    assert not META_KEY.startswith("modelcontextprotocol.io/")
    assert "/" in META_KEY


class TestSdkCapabilityProbe:
    def test_probe_is_boolean_and_never_raises_without_the_sdk(self):
        _call_tool_supports_meta.cache_clear()
        try:
            assert isinstance(_call_tool_supports_meta(), bool)
        finally:
            _call_tool_supports_meta.cache_clear()

    def test_probe_reports_false_when_the_sdk_lacks_meta(self, monkeypatch):
        """An older SDK degrades to today's request shape instead of raising."""
        import sys
        import types

        class _Session:
            async def call_tool(self, name, arguments=None):  # no `meta` param
                ...

        module = types.ModuleType("mcp")
        module.ClientSession = _Session
        monkeypatch.setitem(sys.modules, "mcp", module)
        _call_tool_supports_meta.cache_clear()
        try:
            assert _call_tool_supports_meta() is False
        finally:
            _call_tool_supports_meta.cache_clear()

    def test_probe_reports_true_when_the_sdk_accepts_meta(self, monkeypatch):
        import sys
        import types

        class _Session:
            async def call_tool(self, name, arguments=None, meta=None):
                ...

        module = types.ModuleType("mcp")
        module.ClientSession = _Session
        monkeypatch.setitem(sys.modules, "mcp", module)
        _call_tool_supports_meta.cache_clear()
        try:
            assert _call_tool_supports_meta() is True
        finally:
            _call_tool_supports_meta.cache_clear()
