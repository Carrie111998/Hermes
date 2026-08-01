from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.run import GatewayRunner


def _source(platform=Platform.SLACK) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="C1",
        chat_type="group",
        user_id="U1",
        thread_id="10.0",
        scope_id="T1",
    )


def _runner(adapter) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner.config = MagicMock()
    runner.session_store = MagicMock()
    runner.session_store._entries = {"sk": SimpleNamespace(session_id="sid")}
    return runner


def test_todo_completion_persists_before_threadsafe_wake_without_coroutine() -> None:
    order = []
    adapter = MagicMock()
    adapter.record_desired_plan_snapshot.side_effect = lambda **kwargs: (
        order.append(("persist", kwargs)),
        {"desired_revision": 4},
    )[1]
    runner = _runner(adapter)
    result = '{"todos":[{"id":"a","content":"A","status":"pending"}]}'

    loop = MagicMock()
    loop.is_closed.return_value = False
    loop.call_soon_threadsafe.side_effect = lambda callback: (
        order.append(("wake", callback)),
        callback(),
    )
    state = runner._record_slack_plan_tool_completion(
        event_type="tool.completed",
        tool_name="todo",
        is_error=False,
        result=result,
        source=_source(),
        session_key="sk",
        session_id="sid",
        loop=loop,
    )

    assert state["desired_revision"] == 4
    assert [item[0] for item in order] == ["persist", "wake"]
    loop.call_soon_threadsafe.assert_called_once_with(adapter.request_plan_reconcile)
    adapter.request_plan_reconcile.assert_called_once_with()
    persisted = order[0][1]
    assert persisted["team_id"] == "T1"
    assert persisted["thread_ts"] == "10.0"
    assert persisted["route_user_id"] == "U1"
    assert persisted["chat_type"] == "group"
    assert persisted["todos"][0]["id"] == "a"


@pytest.mark.parametrize(
    ("event_type", "tool_name", "is_error", "platform"),
    [
        ("tool.started", "todo", False, Platform.SLACK),
        ("tool.completed", "terminal", False, Platform.SLACK),
        ("tool.completed", "todo", True, Platform.SLACK),
        ("tool.completed", "todo", False, Platform.DISCORD),
    ],
)
def test_bridge_ignores_non_success_non_todo_non_slack(event_type, tool_name, is_error, platform) -> None:
    adapter = MagicMock()
    runner = _runner(adapter)
    assert runner._record_slack_plan_tool_completion(
        event_type=event_type,
        tool_name=tool_name,
        is_error=is_error,
        result='{"todos":[]}',
        source=_source(platform),
        session_key="sk",
        session_id="sid",
        loop=MagicMock(),
    ) is None
    adapter.record_desired_plan_snapshot.assert_not_called()


def test_bridge_isolates_projection_failure_from_agent_progress() -> None:
    adapter = MagicMock()
    adapter.record_desired_plan_snapshot.side_effect = OSError("disk unavailable")
    runner = _runner(adapter)
    loop = MagicMock()
    assert runner._record_slack_plan_tool_completion(
        event_type="tool.completed", tool_name="todo", is_error=False,
        result='{"todos":[]}', source=_source(), session_key="sk",
        session_id="sid", loop=loop,
    ) is None
    loop.call_soon_threadsafe.assert_not_called()


def test_gateway_plan_bridge_has_no_detached_coroutine_scheduling() -> None:
    source = inspect.getsource(GatewayRunner._record_slack_plan_tool_completion)
    for forbidden in (
        "run_coroutine_threadsafe",
        "safe_schedule_threadsafe",
        "create_task",
        "reconcile_plan_card",
    ):
        assert forbidden not in source
    assert "call_soon_threadsafe" in source


@pytest.mark.asyncio
async def test_runner_revalidates_stale_action_and_sends_notice() -> None:
    adapter = MagicMock()
    adapter.validate_plan_action_metadata.return_value = False
    adapter.send = AsyncMock()
    runner = _runner(adapter)
    event = MessageEvent(
        text="trusted action",
        message_type=MessageType.TEXT,
        source=_source(),
        internal=True,
        metadata={"slack_plan_action": {
            "session_key": "sk", "session_id": "sid", "revision": 1,
        }},
    )

    assert not await runner._validate_slack_plan_action_after_claim(event, "sk")
    adapter.send.assert_awaited_once()
    assert "stale" in adapter.send.call_args.args[1].lower()

    adapter.validate_plan_action_metadata.return_value = True
    adapter.send.reset_mock()
    assert await runner._validate_slack_plan_action_after_claim(event, "sk")
    adapter.send.assert_not_awaited()

    assert not await runner._validate_slack_plan_action_after_claim(event, "other")
    adapter.send.assert_awaited_once()

    runner.session_store._entries["sk"] = SimpleNamespace(session_id="new-session")
    adapter.send.reset_mock()
    assert not await runner._validate_slack_plan_action_after_claim(event, "sk")
    adapter.send.assert_awaited_once()


def test_final_revalidation_is_between_session_claim_and_agent_turn() -> None:
    source = inspect.getsource(GatewayRunner._handle_message)
    claim = source.index("_claim_active_session_slot")
    validate = source.index("_validate_slack_plan_action_after_claim")
    start_turn = source.index("_handle_message_with_agent", validate)
    assert claim < validate < start_turn
