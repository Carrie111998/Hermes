"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
        profile="reviewer",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("hermes_cli.lifecycle.invoke_hook")
@patch("hermes_cli.plugins.invoke_hook")
async def test_gateway_new_carries_bound_cwd_and_explicit_transition(
    mock_plugin_hook, mock_lifecycle_hook, tmp_path
):
    from agent.runtime_cwd import set_session_cwd

    runner = _make_runner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_session_cwd(str(workspace))

    await runner._handle_reset_command(_make_event("/new"))

    finalize = next(
        call for call in mock_plugin_hook.call_args_list
        if call.args and call.args[0] == "on_session_finalize"
    )
    reset = next(
        call for call in mock_lifecycle_hook.call_args_list
        if call.args and call.args[0] == "on_session_reset"
    )
    for call, expected_session in ((finalize, "sess-old"), (reset, "sess-new")):
        assert call.kwargs["session_id"] == expected_session
        assert call.kwargs["old_session_id"] == "sess-old"
        assert call.kwargs["new_session_id"] == "sess-new"
        assert call.kwargs["reason"] == "new_session"
        assert call.kwargs["cwd"] == str(workspace)
        assert call.kwargs["profile_name"] == "reviewer"


@pytest.mark.asyncio
@patch("hermes_cli.lifecycle.invoke_hook")
@patch("hermes_cli.plugins.invoke_hook")
async def test_gateway_new_omits_unknown_profile(
    mock_plugin_hook, mock_lifecycle_hook
):
    runner = _make_runner()
    event = _make_event("/new")
    event.source.profile = None

    await runner._handle_reset_command(event)

    finalize = next(
        call for call in mock_plugin_hook.call_args_list
        if call.args and call.args[0] == "on_session_finalize"
    )
    reset = next(
        call for call in mock_lifecycle_hook.call_args_list
        if call.args and call.args[0] == "on_session_reset"
    )
    assert "profile_name" not in finalize.kwargs
    assert "profile_name" not in reset.kwargs


@pytest.mark.asyncio
@patch("hermes_cli.lifecycle.finalize_session")
async def test_gateway_shutdown_carries_session_profile(mock_finalize):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    session_key = "agent:reviewer:telegram:dm:42"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sess-reviewer",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="42",
            profile="reviewer",
        ),
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: entry}
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)

    async def _cleanup(_agent, *, context):
        assert context == "shutdown finalize"

    runner._cleanup_agent_resources_off_loop = _cleanup
    agent = MagicMock()
    agent.session_id = "sess-reviewer"
    agent._session_messages = []

    await runner._finalize_shutdown_agents({session_key: agent})

    mock_finalize.assert_called_once()
    assert mock_finalize.call_args.kwargs["profile_name"] == "reviewer"


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_fires_finalize_hook(mock_invoke_hook):
    """Idle expiry emits one fail-closed, profile-bound finalize event."""
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="42",
            profile="reviewer",
        ),
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    original_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await original_sleep(0)

    def _hook_and_stop(*_args, **_kwargs):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    expired_call = next(
        call
        for call in mock_invoke_hook.call_args_list
        if call.args
        and call.args[0] == "on_session_finalize"
        and call.kwargs.get("session_id") == "sess-expired"
    )
    assert expired_call.kwargs["old_session_id"] == "sess-expired"
    assert expired_call.kwargs["cwd"] == ""
    assert expired_call.kwargs["profile_name"] == "reviewer"


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_clears_last_resolved_model(mock_invoke_hook):
    """Regression test for #58403.

    ``_session_expiry_watcher`` permanently finalizes an expired session and
    already drops ``_session_model_overrides`` / the reasoning override /
    ``_pending_model_notes`` — a resumed conversation must not inherit stale
    per-session state. It missed ``_last_resolved_model``: without clearing
    it, a resumed session could serve a cached model from before it went
    idle on a transient config-cache miss, exactly the #58403 class the
    /new and compression-exhausted-reset paths already guard against.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {
        session_key: "gpt-5",
        "agent:main:telegram:dm:other": "keep-me",
    }

    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    expired_call = next(
        call for call in mock_invoke_hook.call_args_list
        if call.args and call.args[0] == "on_session_finalize"
    )
    assert "profile_name" not in expired_call.kwargs

    assert session_key not in runner._last_resolved_model, (
        "session-expiry finalization did not clear the expired session's "
        "_last_resolved_model entry (#58403)"
    )
    assert runner._last_resolved_model["agent:main:telegram:dm:other"] == "keep-me", (
        "session-expiry finalization must only clear the expired session's "
        "own key, not unrelated sessions' cached entries"
    )
