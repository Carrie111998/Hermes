import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


def _runner(adapter, *, session_id="session-1"):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.MATRIX: adapter}
    runner._session_db = SimpleNamespace(
        set_session_title=AsyncMock(return_value=True),
    )
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_id=session_id)
        )
    )
    runner._adapter_title_state_lock = threading.Lock()
    runner._adapter_title_generations = {}
    runner._adapter_title_pending = {}
    runner._adapter_title_apply_locks = {}
    runner._session_key_for_source = lambda _source: "matrix:room"
    runner._is_session_run_current = lambda _key, generation: generation == 7
    return runner


def _source():
    return SessionSource(
        platform=Platform.MATRIX,
        chat_id="!room:example.org",
        chat_type="dm",
        user_id="@user:example.org",
    )


@pytest.mark.asyncio
async def test_successful_current_session_rename_persists_then_updates_adapter():
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock(return_value=True))
    runner = _runner(adapter)
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        with patch("hermes_cli.goals.GoalManager") as manager_cls:
            manager_cls.return_value.is_active.return_value = False
            result = await runner._rename_current_gateway_chat(
                _source(), "matrix:room", "session-1", 7, "Fortress", generation
            )
    finally:
        runner._release_adapter_title_generation("session-1")

    assert result == {"success": True, "title": "Fortress", "visible_base": "Fortress"}
    runner._session_db.set_session_title.assert_awaited_once_with("session-1", "Fortress")
    adapter.on_session_semantic_base_changed.assert_awaited_once_with(_source(), "Fortress")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["resumed", "stale-run"])
async def test_stale_or_resumed_turn_cannot_rename(failure):
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock(return_value=True))
    runner = _runner(adapter, session_id="session-2" if failure == "resumed" else "session-1")
    if failure == "stale-run":
        runner._is_session_run_current = lambda *_args: False
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        result = await runner._rename_current_gateway_chat(
            _source(), "matrix:room", "session-1", 7, "Stale", generation
        )
    finally:
        runner._release_adapter_title_generation("session-1")

    assert result["success"] is False
    runner._session_db.set_session_title.assert_not_awaited()
    adapter.on_session_semantic_base_changed.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_rename_latest_wins():
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock(return_value=True))
    runner = _runner(adapter)
    first_validation_started = asyncio.Event()
    release_first_validation = asyncio.Event()
    calls = 0

    async def validating(_source):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_validation_started.set()
            await release_first_validation.wait()
        return SimpleNamespace(session_id="session-1")

    runner._async_session_store.get_or_create_session.side_effect = validating
    old_generation = runner._reserve_adapter_title_generation("session-1")
    old = asyncio.create_task(runner._rename_current_gateway_chat(
        _source(), "matrix:room", "session-1", 7, "Old", old_generation
    ))
    await first_validation_started.wait()
    new_generation = runner._reserve_adapter_title_generation("session-1")
    new = asyncio.create_task(runner._rename_current_gateway_chat(
        _source(), "matrix:room", "session-1", 7, "New", new_generation
    ))
    release_first_validation.set()
    old_result, new_result = await asyncio.gather(old, new)
    runner._release_adapter_title_generation("session-1")
    runner._release_adapter_title_generation("session-1")

    assert old_result["success"] is False
    assert new_result["success"] is True
    runner._session_db.set_session_title.assert_awaited_once_with("session-1", "New")
    adapter.on_session_semantic_base_changed.assert_awaited_once_with(_source(), "New")


@pytest.mark.asyncio
async def test_active_goal_precedes_tool_title_but_title_is_persisted_fallback():
    adapter = SimpleNamespace(on_session_semantic_base_changed=AsyncMock(return_value=True))
    runner = _runner(adapter)
    manager = MagicMock()
    manager.is_active.return_value = True
    manager.state = SimpleNamespace(goal="Active fortress goal")
    generation = runner._reserve_adapter_title_generation("session-1")
    try:
        with patch("hermes_cli.goals.GoalManager", return_value=manager):
            result = await runner._rename_current_gateway_chat(
                _source(), "matrix:room", "session-1", 7, "Fallback title", generation
            )
    finally:
        runner._release_adapter_title_generation("session-1")

    runner._session_db.set_session_title.assert_awaited_once_with(
        "session-1", "Fallback title"
    )
    adapter.on_session_semantic_base_changed.assert_awaited_once_with(
        _source(), "Active fortress goal"
    )
    assert result["visible_base"] == "Active fortress goal"
