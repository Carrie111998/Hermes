from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.run import GatewayRunner
from plugins.platforms.slack.adapter import SlackAdapter


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
    profile = getattr(adapter, "_plan_profile", None)
    runner._profile_adapters = (
        {profile: {Platform.SLACK: adapter}} if profile else {}
    )
    return runner


def _real_adapter(tmp_path, *, home=None) -> SlackAdapter:
    config = PlatformConfig(
        enabled=True,
        token="xoxb-test",
        extra={"native_plan_cards": True},
    )
    with patch("hermes_constants.get_hermes_home", return_value=home or tmp_path):
        adapter = SlackAdapter(config)
    adapter._plan_signing_secret_override = "signing-secret"
    return adapter


async def _ingress_plan_event(
    adapter: SlackAdapter,
    *,
    handler=None,
    expect_event: bool = True,
) -> MessageEvent | None:
    state = adapter.record_desired_plan_snapshot(
        session_key="sk",
        session_id="sid",
        team_id="T1",
        channel_id="C1",
        thread_ts="10.0",
        route_user_id="U1",
        chat_type="group",
        todos=[{"id": "user:a", "content": "A", "status": "in_progress"}],
    )
    assert adapter._plan_store.mark_applied(
        "sk", revision=state["desired_revision"],
        snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    if handler is None:
        events = []

        async def handle(event):
            events.append(event)

        adapter.set_message_handler(handle)
    else:
        adapter.set_message_handler(handler)
        events = handler.__self__.events
    body = {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U1"}, "actions": [{"action_ts": "ingress"}],
    }
    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_complete",
        "block_id": "hermes-plan-controls-r1-" + state["desired_hash"][:10],
        "selected_options": [{"value": "user:a"}],
    })
    await asyncio.sleep(0)
    assert len(events) == (1 if expect_event else 0)
    return events[0] if events else None


class _ProfileAuthorizationRunner:
    def __init__(self) -> None:
        self.events = []
        self.sources = []
        self.pairing_store = MagicMock()
        self.pairing_store.is_approved.return_value = True
        self.secondary_pairing_store = MagicMock()
        self.secondary_pairing_store.is_approved.return_value = True
        self.pairing_stores = {"secondary": self.secondary_pairing_store}

    def _is_user_authorized(self, source) -> bool:
        self.sources.append(source)
        store = self.pairing_stores.get(source.profile, self.pairing_store)
        return bool(store.is_approved(source.platform.value, source.user_id))

    async def handle(self, event) -> None:
        self.events.append(event)


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


@pytest.mark.asyncio
async def test_claim_time_rechecks_current_actor_authorization(tmp_path) -> None:
    default_adapter = _real_adapter(tmp_path / "default")
    assert default_adapter._plan_profile is None

    missing_adapter = _real_adapter(
        tmp_path,
        home=tmp_path / "missing-root" / "profiles" / "secondary",
    )
    missing_authorization = _ProfileAuthorizationRunner()
    missing_authorization.pairing_stores = {}
    await _ingress_plan_event(
        missing_adapter,
        handler=missing_authorization.handle,
        expect_event=False,
    )
    assert missing_authorization.events == []
    assert missing_authorization.sources == []
    missing_authorization.pairing_store.is_approved.assert_not_called()

    adapter = _real_adapter(
        tmp_path,
        home=tmp_path / "hermes-root" / "profiles" / "secondary",
    )
    authorization = _ProfileAuthorizationRunner()
    event = await _ingress_plan_event(adapter, handler=authorization.handle)
    assert authorization.sources[-1].profile == "secondary"
    assert authorization.sources[-1].thread_id == "10.0"
    assert event.metadata["slack_plan_action"]["profile"] == "secondary"
    assert adapter._plan_store.get_session("sk")["profile"] == "secondary"
    missing_profile = dict(event.metadata["slack_plan_action"])
    missing_profile.pop("profile")
    assert adapter._plan_store.validate_action(missing_profile) is None
    assert adapter._plan_store.validate_action({
        **event.metadata["slack_plan_action"], "profile": "default",
    }) is None

    authorization.secondary_pairing_store.is_approved.return_value = False
    adapter.send = AsyncMock()
    runner = _runner(adapter)

    original_validate = adapter.validate_plan_action_metadata
    with patch.object(
        adapter, "validate_plan_action_metadata", wraps=original_validate
    ) as validate:
        assert not await runner._validate_slack_plan_action_after_claim(event, "sk")

    validate.assert_called_once_with(event.metadata["slack_plan_action"])
    assert len(authorization.sources) == 2
    assert authorization.sources[-1].profile == "secondary"
    assert authorization.sources[-1].thread_id == "10.0"
    adapter.send.assert_awaited_once()

    authorization.pairing_stores = {}
    adapter.send.reset_mock()
    assert not await runner._validate_slack_plan_action_after_claim(event, "sk")
    assert len(authorization.sources) == 2
    authorization.pairing_store.is_approved.assert_not_called()
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_time_rejects_current_ineligible_task_status(tmp_path) -> None:
    adapter = _real_adapter(tmp_path)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    event = await _ingress_plan_event(adapter)
    current = adapter.record_desired_plan_snapshot(
        session_key="sk",
        session_id="sid",
        team_id="T1",
        channel_id="C1",
        thread_ts="10.0",
        route_user_id="U1",
        chat_type="group",
        todos=[{"id": "user:a", "content": "A", "status": "pending"}],
    )
    assert adapter._plan_store.mark_applied(
        "sk", revision=current["desired_revision"],
        snapshot_hash=current["desired_hash"], message_ts="20.0",
    )
    metadata = {
        **event.metadata["slack_plan_action"],
        "revision": current["desired_revision"],
        "snapshot_hash": current["desired_hash"],
        "task_ids": ["user:a"],
        "complete_task_ids": ["user:a"],
        "reopen_task_ids": [],
    }
    event.metadata["slack_plan_action"] = metadata

    adapter.send = AsyncMock()
    runner = _runner(adapter)
    original_validate = adapter.validate_plan_action_metadata
    with patch.object(
        adapter, "validate_plan_action_metadata", wraps=original_validate
    ) as validate:
        assert not await runner._validate_slack_plan_action_after_claim(event, "sk")

    assert metadata["revision"] == adapter._plan_store.get_session("sk")["desired_revision"]
    assert metadata["snapshot_hash"] == adapter._plan_store.get_session("sk")["desired_hash"]
    assert runner.session_store._entries["sk"].session_id == "sid"
    validate.assert_called_once_with(event.metadata["slack_plan_action"])
    adapter.send.assert_awaited_once()
    assert "stale" in adapter.send.call_args.args[1].lower()


def test_final_revalidation_is_between_session_claim_and_agent_turn() -> None:
    source = inspect.getsource(GatewayRunner._handle_message)
    claim = source.index("_claim_active_session_slot")
    validate = source.index("_validate_slack_plan_action_after_claim")
    start_turn = source.index("_handle_message_with_agent", validate)
    assert claim < validate < start_turn
