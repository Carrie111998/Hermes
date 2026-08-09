"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
import asyncio
import json
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
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_fires_finalize_hook(mock_invoke_hook):
    """Regression test for #14981.

    When ``_session_expiry_watcher`` sweeps a session that has aged past
    its reset policy (idle timeout, scheduled reset), it must fire
    ``on_session_finalize`` so plugin providers get the same final-pass
    extraction opportunity they'd get from /new or CLI shutdown.  Before
    the fix, the expiry path evicted the agent but silently skipped the
    hook.
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
    replacement_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired-replacement",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.peek_session_id.return_value = replacement_entry.session_id
    runner._clear_conversation_scope = MagicMock()

    def reset_with_terminal_completion(*_args, **_kwargs):
        runner._prepare_terminal_route_completion_sync(
            expired_entry.session_id,
            (object(), object(), "auto_reset:session_expired"),
            session_key,
        )
        return replacement_entry

    runner.session_store.reset_session.side_effect = reset_with_terminal_completion

    runner._evict_cached_agent = MagicMock(return_value=None)
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    # The watcher starts with `await asyncio.sleep(0.2)` and loops while
    # `self._running`.  Patch sleep so the 60s initial delay is instant, and
    # make the expiry hook invocation flip `_running` false so the loop
    # exits cleanly after one pass.
    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    # Look for the finalize call targeting the expired session.
    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0] and c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1].get("session_id") for c in finalize_calls}
    assert "sess-expired" in session_ids, (
        f"on_session_finalize was not fired during idle expiry; "
        f"got session_ids={session_ids} (regression of #14981)"
    )


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
    replacement_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired-replacement",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store.peek_session_id.return_value = replacement_entry.session_id

    def reset_with_terminal_completion(*args, **kwargs):
        runner._prepare_terminal_route_completion_sync(
            expired_entry.session_id,
            (object(), object(), "auto_reset:session_expired"),
            session_key,
        )
        return replacement_entry

    runner.session_store.reset_session.side_effect = reset_with_terminal_completion

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

    assert session_key not in runner._last_resolved_model, (
        "session-expiry finalization did not clear the expired session's "
        "_last_resolved_model entry (#58403)"
    )
    assert runner._last_resolved_model["agent:main:telegram:dm:other"] == "keep-me", (
        "session-expiry finalization must only clear the expired session's "
        "own key, not unrelated sessions' cached entries"
    )


def test_idle_expiry_confirms_exact_computer_use_release_before_finalizing():
    from datetime import timedelta
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = __import__("time").time()
    runner._last_agent_cache_pressure_sweep_ts = __import__("time").time()

    session_key = "agent:main:telegram:dm:cua-expiry"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-cua-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired.return_value = True
    replacement = SessionEntry(
        session_key=session_key,
        session_id="sess-cua-replacement",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.peek_session_id.return_value = replacement.session_id

    def reset_with_terminal_completion(*_args, **_kwargs):
        runner._prepare_terminal_route_completion_sync(
            expired_entry.session_id,
            (object(), object(), "auto_reset:session_expired"),
            session_key,
        )
        return replacement

    runner.session_store.reset_session.side_effect = reset_with_terminal_completion
    runner._evict_cached_agent = MagicMock(return_value=None)
    runner._cleanup_agent_resources = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)
    runner._sweep_agent_cache_under_pressure = MagicMock()

    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0)

    def stop_after_finalize(*args, **kwargs):
        runner._running = False
        return []

    with patch("gateway.run.asyncio.sleep", side_effect=fast_sleep), patch(
        "hermes_cli.lifecycle.finalize_session", side_effect=stop_after_finalize
    ):
        asyncio.run(runner._session_expiry_watcher(interval=0))

    runner.session_store.reset_session.assert_called_once_with(
        session_key,
        reset_reason="session_expired",
        expected_session_id=expired_entry.session_id,
    )
    runner.session_store.set_expiry_finalized.assert_not_called()


def test_idle_expiry_retires_old_cua_id_and_publishes_fresh_route(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tools.computer_use import tool as computer_use

    config = GatewayConfig()
    config.sessions_dir = tmp_path
    runner = GatewayRunner(config)
    runner.session_store._db = None
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="expiry-race",
        user_id="expiry-user",
        chat_type="dm",
    )
    old_entry = runner.session_store.get_or_create_session(source)
    old_id = old_entry.session_id
    backend = MagicMock()
    backend.permission_mode = "standard"
    backend.start.return_value = None
    backend.stop.return_value = None
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner._running = True

    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0)

    def stop_after_finalize(*args, **kwargs):
        runner._running = False

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend
    ), patch("gateway.run.asyncio.sleep", side_effect=fast_sleep), patch(
        "hermes_cli.lifecycle.finalize_session", side_effect=stop_after_finalize
    ):
        computer_use._get_backend(old_id)
        asyncio.run(runner._session_expiry_watcher(interval=0))
        replacement = runner.session_store._entries[old_entry.session_key]
        assert replacement.session_id != old_id
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend(old_id)

    persisted = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert persisted[old_entry.session_key]["session_id"] == replacement.session_id
    computer_use.reset_backend_for_tests()


def test_idle_expiry_never_marks_finalized_when_computer_use_release_fails():
    from datetime import timedelta
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = __import__("time").time()

    session_key = "agent:main:telegram:dm:cua-failed-expiry"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-cua-failed",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired.return_value = True
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)
    runner._sweep_agent_cache_under_pressure = MagicMock()

    attempts = 0

    def fail_reset(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            runner._running = False
        raise RuntimeError("terminal reset failed")

    runner.session_store.reset_session.side_effect = fail_reset
    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=fast_sleep), patch(
        "hermes_cli.lifecycle.finalize_session"
    ) as finalize:
        asyncio.run(runner._session_expiry_watcher(interval=0))

    assert attempts == 3
    finalize.assert_not_called()
    runner.session_store.set_expiry_finalized.assert_not_called()
    assert expired_entry.expiry_finalized is False
