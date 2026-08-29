"""Tests for the gateway's destructive-slash-confirm wrapper.

When ``approvals.destructive_slash_confirm`` is True (default), /new,
/reset, and /undo route through the slash-confirm primitive — native
yes/no buttons on Telegram/Discord/Slack, text fallback elsewhere.
When False (after "Always Approve"), the destructive action runs
immediately.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    """Mirror tests/gateway/test_unknown_command.py::_make_runner."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    # No send_slash_confirm override -> button render returns None,
    # _request_slash_confirm falls back to text path.
    adapter.send_slash_confirm = AsyncMock(return_value=None)
    runner.adapters = {Platform.TELEGRAM: adapter}

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()

    runner._running_agents = {}
    runner._pending_messages = {}
    import itertools as _it
    runner._slash_confirm_counter = _it.count(1)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner._thread_metadata_for_source = lambda *a, **kw: None
    runner._reply_anchor_for_event = lambda _e: None
    return runner


@pytest.mark.asyncio
async def test_gate_on_pending_confirm_registered(monkeypatch):
    """When the gate is on, a pending slash-confirm entry is registered for
    the session — the user's /approve reply will resolve it."""
    from tools import slash_confirm as _slash_confirm_mod
    runner = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    session_key = build_session_key(_make_source())
    runner._session_key_for_source = lambda src: session_key
    _slash_confirm_mod.clear(session_key)

    execute = AsyncMock(return_value="reset done")

    await runner._maybe_confirm_destructive_slash(
        event=_make_event("/new"),
        command="new",
        title="/new",
        detail="Discards history.",
        execute=execute,
    )

    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    assert pending["command"] == "new"
    _slash_confirm_mod.clear(session_key)


@pytest.mark.asyncio
async def test_busy_new_waits_for_exactly_one_confirmation_before_mutating_session():
    runner = _make_runner()
    event = _make_event("/new")
    captured_execute = []

    async def _confirm_once(**kwargs):
        captured_execute.append(kwargs["execute"])
        return "confirmation requested"

    runner._maybe_confirm_destructive_slash = AsyncMock(side_effect=_confirm_once)
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner._handle_reset_command = AsyncMock(return_value="reset done")

    result = await runner._busy_new_command(
        event,
        build_session_key(event.source),
        event.source,
    )

    assert result == "confirmation requested"
    runner._maybe_confirm_destructive_slash.assert_awaited_once()
    runner._interrupt_and_clear_session.assert_not_awaited()
    runner._handle_reset_command.assert_not_awaited()

    approved_result = await captured_execute[0]()

    assert approved_result == "reset done"
    runner._interrupt_and_clear_session.assert_awaited_once()
    runner._handle_reset_command.assert_awaited_once_with(event)
    runner._maybe_confirm_destructive_slash.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_idle_new_guards_reset_against_racing_inbound():
    """Approval owns the adapter lifecycle even when its busy snapshot is idle."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    runner = _make_runner()
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = ""
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    event = _make_event("/new")
    session_key = build_session_key(event.source)
    captured_execute = []
    reset_started = asyncio.Event()
    finish_reset = asyncio.Event()
    follow_up_processed = asyncio.Event()
    handled: list[str] = []

    async def _confirm_once(**kwargs):
        captured_execute.append(kwargs["execute"])
        return "confirmation requested"

    async def _reset(_event):
        reset_started.set()
        await finish_reset.wait()
        return "reset done"

    async def _handle_follow_up(incoming):
        handled.append(incoming.text)
        follow_up_processed.set()
        return ""

    runner._maybe_confirm_destructive_slash = AsyncMock(side_effect=_confirm_once)
    runner._handle_reset_command = AsyncMock(side_effect=_reset)
    adapter._message_handler = _handle_follow_up

    result = await runner._handle_new_command(event)
    assert result == "confirmation requested"
    assert session_key not in adapter._active_sessions

    approval_task = asyncio.create_task(captured_execute[0]())
    await asyncio.wait_for(reset_started.wait(), timeout=1)

    assert session_key in adapter._active_sessions
    await adapter.handle_message(_make_event("racing follow up"))
    assert not follow_up_processed.is_set()
    assert session_key in adapter._pending_messages
    runner._interrupt_and_clear_session.assert_not_awaited()

    finish_reset.set()
    assert await approval_task == "reset done"
    await asyncio.wait_for(follow_up_processed.wait(), timeout=1)
    follow_up_task = adapter._session_tasks.get(session_key)
    if follow_up_task is not None:
        await follow_up_task

    assert handled == ["racing follow up"]
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_approved_new_holds_preapproval_pending_until_reset_completes():
    """The old owner cannot start input queued before /new approval."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = ""
    session_key = build_session_key(_make_source())
    active_started = asyncio.Event()
    reset_started = asyncio.Event()
    finish_reset = asyncio.Event()
    follow_started = asyncio.Event()
    order: list[str] = []

    async def _handle(incoming):
        if incoming.text == "active turn":
            active_started.set()
            await asyncio.Event().wait()
        order.append("follow entered")
        follow_started.set()
        return ""

    async def _execute_reset():
        order.append("reset entered")
        reset_started.set()
        await finish_reset.wait()
        order.append("reset completed")
        return "reset done"

    adapter._message_handler = _handle
    await adapter.handle_message(_make_event("active turn"))
    await asyncio.wait_for(active_started.wait(), timeout=1)

    await adapter.handle_message(_make_event("queued before approval"))
    assert session_key in adapter._pending_messages
    assert not follow_started.is_set()

    approval_task = asyncio.create_task(
        adapter._run_approved_active_session_command(session_key, _execute_reset)
    )
    await asyncio.wait_for(reset_started.wait(), timeout=1)

    assert not follow_started.is_set()
    assert order == ["reset entered"]
    assert session_key in adapter._pending_messages

    finish_reset.set()
    assert await approval_task == "reset done"
    await asyncio.wait_for(follow_started.wait(), timeout=1)
    follow_task = adapter._session_tasks.get(session_key)
    if follow_task is not None:
        await follow_task

    assert order == ["reset entered", "reset completed", "follow entered"]
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_resolve_always_persists_opt_out_and_runs_execute(monkeypatch):
    """Resolving with 'always' must (a) flip the config gate to False,
    (b) run execute, and (c) include a one-time opt-out note in the reply."""
    from tools import slash_confirm as _slash_confirm_mod
    runner = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    session_key = build_session_key(_make_source())
    runner._session_key_for_source = lambda src: session_key
    _slash_confirm_mod.clear(session_key)

    saved: dict = {}

    def _fake_save(path, value):
        saved[path] = value
        return True

    import cli as cli_mod
    monkeypatch.setattr(cli_mod, "save_config_value", _fake_save)

    execute = AsyncMock(return_value="✨ fresh")

    await runner._maybe_confirm_destructive_slash(
        event=_make_event("/new"),
        command="new",
        title="/new",
        detail="Discards history.",
        execute=execute,
    )

    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    resolved = await _slash_confirm_mod.resolve(
        session_key, pending["confirm_id"], "always",
    )

    execute.assert_awaited_once()
    assert saved.get("approvals.destructive_slash_confirm") is False
    assert resolved is not None
    assert "✨ fresh" in resolved
    assert "config.yaml" in resolved


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_path", ["typed", "menu"])
@pytest.mark.parametrize(
    ("scope_name", "revoked_value"),
    [
        pytest.param("allowed_chats", ["-999"], id="allowed-chats"),
        pytest.param("allowed_topics", ["8"], id="allowed-topics"),
        pytest.param("ignored_threads", [7], id="ignored-threads"),
    ],
)
@pytest.mark.parametrize("choice", ["once", "always"])
async def test_stale_telegram_slash_confirm_revalidates_hard_scope_before_resolution(
    monkeypatch, entry_path, scope_name, revoked_value, choice
):
    """A prompt cannot outlive the Telegram chat/topic scope that created it."""
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools import slash_confirm as slash_confirm_mod

    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            typing_indicator=False,
            extra={
                "allowed_chats": ["-100"],
                "allowed_topics": ["7"],
                "ignored_threads": [],
            },
        )
    )
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=901)
    )
    query = AsyncMock()
    query.data = "hm:new"
    query.from_user = SimpleNamespace(
        id=42,
        first_name="Alexey",
        full_name="Alexey",
        is_bot=False,
    )
    query.message = SimpleNamespace(
        chat_id=-100,
        message_id=900,
        message_thread_id=7,
        is_topic_message=True,
        chat=SimpleNamespace(
            id=-100,
            type="supergroup",
            is_forum=True,
            title="Security test",
            full_name=None,
        ),
    )
    event = adapter._main_menu_event(query, "/new")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get(
            "group_sessions_per_user", True
        ),
        thread_sessions_per_user=adapter.config.extra.get(
            "thread_sessions_per_user", False
        ),
        profile=adapter._session_key_profile(event.source),
    )
    slash_confirm_mod.clear(session_key)

    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": True}
    }
    runner._session_key_for_source = lambda _source: session_key
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner._handle_reset_command = AsyncMock(return_value="reset done")

    saved = MagicMock(return_value=True)
    monkeypatch.setattr("cli.save_config_value", saved)
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *_args, **_kwargs: True
    )

    async def _handle_new(incoming):
        return await runner._handle_new_command(incoming)

    adapter._message_handler = _handle_new

    try:
        if entry_path == "menu":
            await adapter._handle_main_menu_callback(query, "hm:new")
        else:
            await adapter.handle_message(event)
        request_task = adapter._session_tasks.get(session_key)
        if request_task is not None:
            await request_task

        pending = slash_confirm_mod.get_pending(session_key)
        assert pending is not None
        confirm_id = pending["confirm_id"]
        assert adapter._slash_confirm_state[confirm_id] == session_key

        adapter.config.extra[scope_name] = revoked_value
        query.data = f"sc:{choice}:{confirm_id}"
        query.answer.reset_mock()
        query.edit_message_text.reset_mock()
        adapter._send_message_with_thread_fallback.reset_mock()

        for _ in range(2):
            await adapter._handle_callback_query(
                SimpleNamespace(callback_query=query), SimpleNamespace()
            )

        assert query.answer.await_count == 2
        assert all(
            "no longer available" in call.kwargs["text"].lower()
            for call in query.answer.await_args_list
        )
        query.edit_message_text.assert_not_awaited()
        adapter._send_message_with_thread_fallback.assert_not_awaited()
        runner._interrupt_and_clear_session.assert_not_awaited()
        runner._handle_reset_command.assert_not_awaited()
        saved.assert_not_called()
        assert adapter._slash_confirm_state[confirm_id] == session_key
        assert slash_confirm_mod.get_pending(session_key)["confirm_id"] == confirm_id
    finally:
        slash_confirm_mod.clear(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_path", ["typed", "menu"])
@pytest.mark.parametrize("choice", ["cancel", "once", "always"])
async def test_busy_new_mutates_adapter_only_after_approval(
    monkeypatch, entry_path, choice
):
    from contextlib import suppress

    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools import slash_confirm as slash_confirm_mod

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    query = AsyncMock()
    query.data = "hm:new"
    query.from_user = SimpleNamespace(
        id=42,
        first_name="Alexey",
        full_name="Alexey",
        is_bot=False,
    )
    query.message = SimpleNamespace(
        chat_id=100,
        message_id=900,
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(
            id=100,
            type="private",
            title=None,
            full_name="Alexey",
        ),
    )
    event = adapter._main_menu_event(query, "/new")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get(
            "group_sessions_per_user", True
        ),
        thread_sessions_per_user=adapter.config.extra.get(
            "thread_sessions_per_user", False
        ),
        profile=adapter._session_key_profile(event.source),
    )
    slash_confirm_mod.clear(session_key)

    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": True}
    }
    runner._session_key_for_source = lambda _source: session_key
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner._handle_reset_command = AsyncMock(return_value="reset done")
    monkeypatch.setattr("cli.save_config_value", lambda *_args: True)

    async def _handle_new(incoming):
        return await runner._busy_new_command(
            incoming, session_key, incoming.source
        )

    adapter._message_handler = _handle_new
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="reply")
    )
    adapter.send_slash_confirm = AsyncMock(
        return_value=SendResult(success=True, message_id="confirm")
    )
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *_args, **_kwargs: True
    )

    cancelled = []

    async def _active_turn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("cancelled")
            raise

    original_guard = asyncio.Event()
    active_task = asyncio.create_task(_active_turn())
    adapter._active_sessions[session_key] = original_guard
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    try:
        if entry_path == "menu":
            await adapter._handle_main_menu_callback(query, "hm:new")
        else:
            await adapter.handle_message(event)

        pending = slash_confirm_mod.get_pending(session_key)
        assert pending is not None
        assert adapter._active_sessions.get(session_key) is original_guard
        assert adapter._session_tasks.get(session_key) is active_task
        assert active_task.done() is False
        assert cancelled == []
        runner._interrupt_and_clear_session.assert_not_awaited()
        runner._handle_reset_command.assert_not_awaited()

        await slash_confirm_mod.resolve(
            session_key, pending["confirm_id"], choice
        )

        if choice == "cancel":
            assert adapter._active_sessions.get(session_key) is original_guard
            assert adapter._session_tasks.get(session_key) is active_task
            assert active_task.done() is False
            assert cancelled == []
            runner._interrupt_and_clear_session.assert_not_awaited()
            runner._handle_reset_command.assert_not_awaited()
        else:
            assert cancelled == ["cancelled"]
            assert session_key not in adapter._session_tasks
            assert session_key not in adapter._active_sessions
            runner._interrupt_and_clear_session.assert_awaited_once()
            runner._handle_reset_command.assert_awaited_once_with(event)
    finally:
        slash_confirm_mod.clear(session_key)
        if not active_task.done():
            active_task.cancel()
        with suppress(asyncio.CancelledError):
            await active_task


@pytest.mark.asyncio
async def test_cancelled_approved_new_releases_guard_and_next_inbound_continues():
    """Cancellation during approved reset must not strand a guard with no owner."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    event = _make_event("/new")
    session_key = build_session_key(event.source)
    old_cancelled = asyncio.Event()
    reset_started = asyncio.Event()
    keep_reset_running = asyncio.Event()

    async def _active_turn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancelled.set()
            raise

    async def _execute_reset():
        reset_started.set()
        await keep_reset_running.wait()

    original_guard = asyncio.Event()
    active_task = asyncio.create_task(_active_turn())
    adapter._active_sessions[session_key] = original_guard
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    approval_task = asyncio.create_task(
        adapter._run_approved_active_session_command(session_key, _execute_reset)
    )
    await asyncio.wait_for(reset_started.wait(), timeout=1)
    assert old_cancelled.is_set()
    assert active_task.done()
    assert session_key not in adapter._session_tasks

    approval_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await approval_task

    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks

    next_inbound_processed = asyncio.Event()

    async def _handle_next(_event):
        next_inbound_processed.set()
        return ""

    adapter._message_handler = _handle_next
    await adapter.handle_message(_make_event("continue"))
    await asyncio.wait_for(next_inbound_processed.wait(), timeout=1)

    next_task = adapter._session_tasks.get(session_key)
    if next_task is not None:
        with suppress(asyncio.CancelledError):
            await next_task


@pytest.mark.asyncio
async def test_cancelled_approved_new_drains_queued_follow_up_once():
    """A cancelled reset still hands queued input to one fresh owner."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = ""
    session_key = build_session_key(_make_source())
    reset_started = asyncio.Event()
    follow_up_processed = asyncio.Event()
    handled: list[str] = []

    async def _active_turn():
        await asyncio.Event().wait()

    async def _execute_reset():
        reset_started.set()
        await asyncio.Event().wait()

    async def _handle_follow_up(event):
        handled.append(event.text)
        follow_up_processed.set()
        return ""

    adapter._message_handler = _handle_follow_up
    original_guard = asyncio.Event()
    active_task = asyncio.create_task(_active_turn())
    adapter._active_sessions[session_key] = original_guard
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    approval_task = asyncio.create_task(
        adapter._run_approved_active_session_command(session_key, _execute_reset)
    )
    await asyncio.wait_for(reset_started.wait(), timeout=1)
    assert active_task.done()

    await adapter.handle_message(_make_event("follow up"))
    assert session_key in adapter._pending_messages
    assert not follow_up_processed.is_set()

    approval_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await approval_task

    await asyncio.wait_for(follow_up_processed.wait(), timeout=1)
    follow_up_task = adapter._session_tasks.get(session_key)
    if follow_up_task is not None:
        await follow_up_task

    assert handled == ["follow up"]
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_base_exception_during_approved_new_drains_queued_follow_up_once():
    """A reset BaseException cannot orphan input behind its command guard."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    class ResetAborted(BaseException):
        pass

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60
    session_key = build_session_key(_make_source())
    reset_started = asyncio.Event()
    abort_reset = asyncio.Event()
    follow_up_processed = asyncio.Event()
    handled: list[str] = []

    async def _active_turn():
        await asyncio.Event().wait()

    async def _execute_reset():
        reset_started.set()
        await abort_reset.wait()
        raise ResetAborted

    async def _handle_follow_up(event):
        handled.append(event.text)
        follow_up_processed.set()
        return ""

    adapter._message_handler = _handle_follow_up
    active_task = asyncio.create_task(_active_turn())
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    approval_task = asyncio.create_task(
        adapter._run_approved_active_session_command(session_key, _execute_reset)
    )
    await asyncio.wait_for(reset_started.wait(), timeout=1)
    assert active_task.done()

    await adapter.handle_message(_make_event("follow up"))
    assert session_key in adapter._text_debounce_store()
    assert session_key not in adapter._pending_messages
    assert not follow_up_processed.is_set()

    abort_reset.set()
    with pytest.raises(ResetAborted):
        await approval_task

    await asyncio.wait_for(follow_up_processed.wait(), timeout=1)
    follow_up_task = adapter._session_tasks.get(session_key)
    if follow_up_task is not None:
        await follow_up_task

    assert handled == ["follow up"]
    assert session_key not in adapter._text_debounce_store()
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_cancelled_approved_new_restores_cancellation_resistant_owner():
    """Caller cancellation during old-turn drain must propagate and restore ownership."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    session_key = build_session_key(_make_source())
    old_cancel_received = asyncio.Event()
    release_old_turn = asyncio.Event()
    reset_calls = 0

    async def _cancellation_resistant_turn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancel_received.set()
            await release_old_turn.wait()

    async def _execute_reset():
        nonlocal reset_calls
        reset_calls += 1
        return "reset"

    original_guard = asyncio.Event()
    active_task = asyncio.create_task(_cancellation_resistant_turn())
    adapter._active_sessions[session_key] = original_guard
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    approval_task = asyncio.create_task(
        adapter._run_approved_active_session_command(session_key, _execute_reset)
    )
    await asyncio.wait_for(old_cancel_received.wait(), timeout=1)
    approval_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await approval_task
        assert reset_calls == 0
        assert adapter._active_sessions.get(session_key) is original_guard
        assert adapter._session_tasks.get(session_key) is active_task
        assert not active_task.done()
    finally:
        release_old_turn.set()
        await active_task
        adapter._active_sessions.pop(session_key, None)
        adapter._session_tasks.pop(session_key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_path", ["typed", "menu"])
@pytest.mark.parametrize("choice", ["once", "cancel"])
async def test_idle_new_decision_handles_turn_started_while_confirming(
    monkeypatch, entry_path, choice
):
    """The decision must act on approval-time state, never request-time state."""
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools import slash_confirm as slash_confirm_mod

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    query = AsyncMock()
    query.data = "hm:new"
    query.from_user = SimpleNamespace(
        id=42,
        first_name="Alexey",
        full_name="Alexey",
        is_bot=False,
    )
    query.message = SimpleNamespace(
        chat_id=100,
        message_id=900,
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(
            id=100,
            type="private",
            title=None,
            full_name="Alexey",
        ),
    )
    event = adapter._main_menu_event(query, "/new")
    session_key = build_session_key(event.source)
    slash_confirm_mod.clear(session_key)

    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": True}
    }
    runner._session_key_for_source = lambda _source: session_key
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner._handle_reset_command = AsyncMock(return_value="reset done")
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="reply")
    )
    adapter.send_slash_confirm = AsyncMock(
        return_value=SendResult(success=True, message_id="confirm")
    )
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *_args, **_kwargs: True
    )

    request_finished = asyncio.Event()

    async def _handle_new(incoming):
        try:
            return await runner._handle_new_command(incoming)
        finally:
            request_finished.set()

    adapter._message_handler = _handle_new

    if entry_path == "menu":
        await adapter._handle_main_menu_callback(query, "hm:new")
    else:
        await adapter.handle_message(event)
    await asyncio.wait_for(request_finished.wait(), timeout=1)
    request_task = adapter._session_tasks.get(session_key)
    if request_task is not None:
        await request_task

    pending = slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    assert session_key not in adapter._active_sessions
    runner._interrupt_and_clear_session.assert_not_awaited()
    runner._handle_reset_command.assert_not_awaited()

    # Approval must derive the key the live adapter owns.  The runner's session
    # resolver can be independently reconfigured while confirmation is pending.
    runner._session_key_for_source = lambda _source: "runner-only:stale-key"

    active_cancelled = asyncio.Event()

    async def _new_active_turn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            active_cancelled.set()
            raise

    active_guard = asyncio.Event()
    active_task = asyncio.create_task(_new_active_turn())
    adapter._active_sessions[session_key] = active_guard
    adapter._session_tasks[session_key] = active_task
    adapter._background_tasks.add(active_task)
    await asyncio.sleep(0)

    try:
        resolved = await slash_confirm_mod.resolve(
            session_key, pending["confirm_id"], choice
        )

        if choice == "cancel":
            assert resolved is not None
            assert not active_cancelled.is_set()
            assert not active_task.done()
            assert adapter._session_tasks.get(session_key) is active_task
            assert adapter._active_sessions.get(session_key) is active_guard
            runner._interrupt_and_clear_session.assert_not_awaited()
            runner._handle_reset_command.assert_not_awaited()
        else:
            assert resolved == "reset done"
            assert active_cancelled.is_set()
            assert active_task.done()
            assert session_key not in adapter._session_tasks
            assert session_key not in adapter._active_sessions
            runner._interrupt_and_clear_session.assert_awaited_once_with(
                session_key,
                event.source,
                interrupt_reason="Session reset requested",
                invalidation_reason="new_command",
            )
            runner._handle_reset_command.assert_awaited_once_with(event)
    finally:
        slash_confirm_mod.clear(session_key)
        if not active_task.done():
            active_task.cancel()
        with suppress(asyncio.CancelledError):
            await active_task

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_path", "command_text"),
    [
        ("typed", "/new"),
        ("typed", "/reset"),
        ("menu", "/new"),
    ],
)
async def test_idle_confirmation_disabled_new_resets_once_before_racing_follow_up(
    monkeypatch, entry_path, command_text
):
    """An idle immediate reset must not cancel its own adapter owner task."""
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = ""
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="reply")
    )
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": False}
    }
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()

    reset_started = asyncio.Event()
    finish_reset = asyncio.Event()
    follow_up_processed = asyncio.Event()
    order: list[str] = []
    command_owner: list[asyncio.Task] = []

    async def _reset(_event):
        order.append("reset entered")
        reset_started.set()
        await finish_reset.wait()
        order.append("reset completed")
        return "reset done"

    async def _route(incoming):
        if incoming.text in {"/new", "/reset"}:
            owner = asyncio.current_task()
            assert owner is not None
            command_owner.append(owner)
            return await runner._handle_new_command(incoming)
        order.append("follow entered")
        follow_up_processed.set()
        return ""

    runner._handle_reset_command = AsyncMock(side_effect=_reset)
    adapter._message_handler = _route
    event = _make_event(command_text)
    session_key = build_session_key(event.source)

    if entry_path == "menu":
        query = AsyncMock()
        query.data = "hm:new"
        query.from_user = SimpleNamespace(id="u1", first_name="tester")
        query.message = SimpleNamespace(
            chat_id="c1",
            message_id="m1",
            message_thread_id=None,
            is_topic_message=False,
            chat=SimpleNamespace(
                id="c1", type="private", title=None, full_name="tester"
            ),
        )
        monkeypatch.setattr(
            adapter,
            "_is_callback_user_authorized",
            lambda *_args, **_kwargs: True,
        )
        await adapter._handle_main_menu_callback(query, "hm:new")
    else:
        await adapter.handle_message(event)

    await asyncio.wait_for(reset_started.wait(), timeout=1)
    assert adapter._session_tasks.get(session_key) is command_owner[0]
    assert not command_owner[0].cancelling()

    await adapter.handle_message(_make_event("racing follow up"))
    assert session_key in adapter._pending_messages
    assert not follow_up_processed.is_set()
    assert order == ["reset entered"]

    finish_reset.set()
    await asyncio.wait_for(follow_up_processed.wait(), timeout=1)
    follow_up_task = adapter._session_tasks.get(session_key)
    if follow_up_task is not None:
        await follow_up_task

    runner._handle_reset_command.assert_awaited_once()
    runner._interrupt_and_clear_session.assert_not_awaited()
    assert order == ["reset entered", "reset completed", "follow entered"]
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_idle_new_after_always_approve_resets_once_before_racing_follow_up(
    monkeypatch,
):
    """The persisted Always Approve state keeps later idle /new ordered."""
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools import slash_confirm as slash_confirm_mod

    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", typing_indicator=False)
    )
    adapter._busy_text_mode = ""
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="reply")
    )
    adapter.send_slash_confirm = AsyncMock(
        return_value=SendResult(success=True, message_id="confirm")
    )
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    confirm_required = True
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": confirm_required}
    }
    runner._adapter_for_source = lambda _source: adapter
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()

    reset_calls = 0
    second_reset_started = asyncio.Event()
    finish_second_reset = asyncio.Event()
    follow_up_processed = asyncio.Event()
    order: list[str] = []

    def _save(path, value):
        nonlocal confirm_required
        assert (path, value) == (
            "approvals.destructive_slash_confirm",
            False,
        )
        confirm_required = False
        return True

    monkeypatch.setattr("cli.save_config_value", _save)

    async def _reset(_event):
        nonlocal reset_calls
        reset_calls += 1
        if reset_calls == 2:
            order.append("reset entered")
            second_reset_started.set()
            await finish_second_reset.wait()
            order.append("reset completed")
        return "reset done"

    async def _route(incoming):
        if incoming.text == "/new":
            return await runner._handle_new_command(incoming)
        order.append("follow entered")
        follow_up_processed.set()
        return ""

    runner._handle_reset_command = AsyncMock(side_effect=_reset)
    adapter._message_handler = _route
    event = _make_event("/new")
    session_key = build_session_key(event.source)
    slash_confirm_mod.clear(session_key)

    try:
        await adapter.handle_message(event)
        first_owner = adapter._session_tasks.get(session_key)
        if first_owner is not None:
            await first_owner
        pending = slash_confirm_mod.get_pending(session_key)
        assert pending is not None

        resolved = await slash_confirm_mod.resolve(
            session_key, pending["confirm_id"], "always"
        )
        assert resolved is not None
        assert confirm_required is False
        assert reset_calls == 1

        await adapter.handle_message(event)
        await asyncio.wait_for(second_reset_started.wait(), timeout=1)
        await adapter.handle_message(_make_event("racing follow up"))
        assert session_key in adapter._pending_messages
        assert not follow_up_processed.is_set()

        finish_second_reset.set()
        await asyncio.wait_for(follow_up_processed.wait(), timeout=1)
        follow_up_task = adapter._session_tasks.get(session_key)
        if follow_up_task is not None:
            await follow_up_task

        assert reset_calls == 2
        assert order == ["reset entered", "reset completed", "follow entered"]
        runner._interrupt_and_clear_session.assert_not_awaited()
        assert session_key not in adapter._pending_messages
        assert session_key not in adapter._active_sessions
        assert session_key not in adapter._session_tasks
    finally:
        slash_confirm_mod.clear(session_key)
