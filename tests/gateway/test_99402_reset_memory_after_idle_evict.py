"""Regression test for #99402: /new after an idle-evicted agent loses memory.

A mode="none" gateway session's agent can be soft-evicted by the idle-TTL
sweep without firing ``on_session_end`` (correct — eviction is a cache
detail, the session continues). But when the user then runs ``/new``, the
handler used to skip the whole memory chain because ``_old_agent is None``:
``_cleanup_agent_resources`` → ``shutdown_memory_provider`` →
``on_session_end`` never ran, and the old transcript never reached memory
providers that extract at session end (Cognee ``improve()``, MEMORY.md
synthesis, …).

The fix compensates on the cache-miss branch: reload the transcript from
the session DB and fire ``on_session_end`` on a throwaway MemoryManager
initialized with the OLD session id.
"""
import asyncio
import threading
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


def _make_runner(cache: dict) -> "GatewayRunner":
    """Bare GatewayRunner whose agent cache is ``cache`` ({} = idle-evicted)."""
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
        session_key=session_key, session_id="sess-old",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    new_entry = SessionEntry(
        session_key=session_key, session_id="sess-new",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.reset_session.return_value = new_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    runner._agent_cache_lock = threading.RLock()
    runner._agent_cache = cache

    runner._session_db = MagicMock()
    runner._session_db.get_messages_as_conversation.return_value = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi!"},
    ]
    runner._session_db.get_session_title.return_value = None
    return runner


def _fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.name = "fakeprov"
    provider.is_available.return_value = True
    return provider


@pytest.mark.asyncio
async def test_new_after_idle_evict_fires_on_session_end_from_db():
    """Cache miss at /new: the old transcript must still reach the memory
    provider's on_session_end, scoped to the OLD session id."""
    runner = _make_runner(cache={})  # idle-evicted — no cached agent
    provider = _fake_provider()

    with patch(
        "tools.memory_tool.get_builtin_memory_config",
        return_value={"provider": "fakeprov"},
    ), patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ) as loaded, patch(
        "hermes_cli.profiles.get_active_profile_name", return_value="default"
    ):
        await asyncio.wait_for(
            runner._handle_reset_command(_make_event("/new")), timeout=5
        )

    loaded.assert_called_once_with("fakeprov")
    provider.on_session_end.assert_called_once_with([
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi!"},
    ])
    provider.initialize.assert_called_once()
    init_kwargs = provider.initialize.call_args.kwargs
    assert init_kwargs["session_id"] == "sess-old"
    assert init_kwargs["gateway_session_key"] == build_session_key(_make_source())
    provider.shutdown.assert_called_once()
    # The reset itself still rotated the session.
    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_new_with_cached_agent_skips_compensation():
    """Cache hit at /new keeps the established path: the cached agent's own
    teardown fires on_session_end — the compensation manager must NOT run."""
    session_key = build_session_key(_make_source())
    agent = MagicMock()
    runner = _make_runner(cache={session_key: agent})
    # Loud failure if the compensation path is reached on a cache hit.
    runner._commit_memory_for_evicted_session = MagicMock(
        side_effect=AssertionError("compensation ran with a cached agent")
    )

    await asyncio.wait_for(
        runner._handle_reset_command(_make_event("/new")), timeout=5
    )

    agent.shutdown_memory_provider.assert_called_once()
    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_new_after_idle_evict_without_memory_config_is_noop():
    """No external memory provider configured: /new after eviction must not
    build anything — the DB transcript read is skipped and reset proceeds."""
    runner = _make_runner(cache={})

    with patch(
        "tools.memory_tool.get_builtin_memory_config", return_value={}
    ), patch(
        "plugins.memory.load_memory_provider",
        side_effect=AssertionError("provider loaded with no memory config"),
    ):
        await asyncio.wait_for(
            runner._handle_reset_command(_make_event("/new")), timeout=5
        )

    runner._session_db.get_messages_as_conversation.assert_not_called()
    runner.session_store.reset_session.assert_called_once()
