"""Bound requester identity must survive the MCP event-loop hop.

``run_coroutine_threadsafe`` creates the task inside the MCP loop thread,
so it copies that thread's ContextVars — not the scheduling thread's.
OAuth ``per_user`` capture would fail closed (or inherit a stale principal)
without ``_wrap_with_session_principal``. Mirrors
``test_mcp_loop_profile_override.py`` for HERMES_HOME.
"""
import threading

import pytest

from gateway.session_context import (
    apply_bound_session_principal,
    get_bound_session_principal,
    reset_session_vars,
    set_session_vars,
)


@pytest.fixture
def mcp_loop():
    import tools.mcp_tool as mcp_tool

    mcp_tool._ensure_mcp_loop()
    yield mcp_tool
    mcp_tool._stop_mcp_loop()


@pytest.fixture(autouse=True)
def _reset_principal():
    reset_session_vars()
    yield
    reset_session_vars()


async def _read_principal():
    principal = get_bound_session_principal()
    if principal is None:
        return None
    return (principal.platform, principal.scope_id, principal.user_id)


def test_bound_principal_propagates_to_mcp_loop(mcp_loop):
    assert mcp_loop._run_on_mcp_loop(_read_principal(), timeout=10) is None

    set_session_vars(platform="slack", scope_id="T1", user_id="U-alice")
    assert mcp_loop._run_on_mcp_loop(_read_principal(), timeout=10) == (
        "slack",
        "T1",
        "U-alice",
    )
    assert mcp_loop._run_on_mcp_loop(lambda: _read_principal(), timeout=10) == (
        "slack",
        "T1",
        "U-alice",
    )

    reset_session_vars()
    assert mcp_loop._run_on_mcp_loop(_read_principal(), timeout=10) is None


def test_empty_scope_id_propagates(mcp_loop):
    set_session_vars(platform="telegram", scope_id="", user_id="12345")
    assert mcp_loop._run_on_mcp_loop(_read_principal(), timeout=10) == (
        "telegram",
        "",
        "12345",
    )


def test_concurrent_principals_do_not_interfere(mcp_loop):
    results: dict = {}

    def scoped_call(key, platform, scope_id, user_id):
        reset_session_vars()
        set_session_vars(platform=platform, scope_id=scope_id, user_id=user_id)
        results[key] = mcp_loop._run_on_mcp_loop(_read_principal(), timeout=10)

    threads = [
        threading.Thread(
            target=scoped_call, args=("a", "slack", "T1", "U-alice")
        ),
        threading.Thread(
            target=scoped_call, args=("b", "slack", "T1", "U-bob")
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert results == {
        "a": ("slack", "T1", "U-alice"),
        "b": ("slack", "T1", "U-bob"),
    }


def test_wrap_is_noop_without_principal(mcp_loop):
    async def trivial():
        return 42

    coro = trivial()
    wrapped = mcp_loop._wrap_with_session_principal(coro)
    assert wrapped is coro
    coro.close()


def test_oauth_capture_on_loop_uses_caller_principal(tmp_path, monkeypatch, mcp_loop):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "mcp:\n  oauth:\n    identity_mode: per_user\n",
        encoding="utf-8",
    )
    from hermes_cli.config import load_config
    from tools.mcp_oauth_identity import connection_registry_token, resolve_mcp_oauth_scope

    loaded = load_config()
    assert loaded.get("mcp", {}).get("oauth", {}).get("identity_mode") == "per_user"

    set_session_vars(platform="slack", scope_id="T1", user_id="U-alice")
    expected = connection_registry_token(
        "github",
        resolve_mcp_oauth_scope(uses_oauth=True),
    )

    async def capture():
        server = mcp_loop.MCPServerTask("github")
        server._auth_type = "oauth"
        server._capture_oauth_identity({"auth": "oauth", "url": "https://example"})
        return server._registry_key

    assert mcp_loop._run_on_mcp_loop(capture(), timeout=10) == expected


def test_apply_bound_session_principal_restores():
    reset_session_vars()
    assert get_bound_session_principal() is None
    from gateway.session_context import BoundSessionPrincipal

    principal = BoundSessionPrincipal("slack", "T1", "U-alice")
    with apply_bound_session_principal(principal):
        bound = get_bound_session_principal()
        assert bound is not None
        assert bound.user_id == "U-alice"
    assert get_bound_session_principal() is None
