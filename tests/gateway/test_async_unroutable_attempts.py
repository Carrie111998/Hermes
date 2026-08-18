"""Durable async completion routing must precede delivery claiming."""

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


@pytest.fixture
def durable_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    event = {
        "type": "async_delegation",
        "delegation_id": "unroute-test",
        "session_key": "raw-cli-session",
        "status": "completed",
        "summary": "done",
        "dispatched_at": 1.0,
        "completed_at": 2.0,
    }
    from tools import async_delegation

    async_delegation._persist_dispatch({
        "delegation_id": event["delegation_id"],
        "session_key": event["session_key"],
        "origin_ui_session_id": "",
        "parent_session_id": event.get("parent_session_id"),
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, event)
    return event


def _row():
    from tools import async_delegation

    with async_delegation._connect() as conn:
        return conn.execute(
            "SELECT delivery_state, delivery_attempts, delivery_claim "
            "FROM async_delegations WHERE delegation_id=?",
            ("unroute-test",),
        ).fetchone()


def _runner(adapters):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    return runner


def _api_adapter():
    return SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_unroutable_raw_completion_does_not_claim(durable_event, monkeypatch):
    from tools import async_delegation

    def claim_should_not_run(*_args):
        raise AssertionError("unroutable completion was claimed")

    monkeypatch.setattr(async_delegation, "claim_completion_delivery", claim_should_not_run)
    runner = _runner({Platform.TELEGRAM: SimpleNamespace(handle_message=AsyncMock())})

    result = await runner._deliver_completion_notification("done", durable_event)

    assert result is None
    assert _row() == ("pending", 0, None)


@pytest.mark.asyncio
async def test_raw_completion_delivers_with_api_server(durable_event, monkeypatch):
    from gateway import wake

    posts = []
    async def fake_deliver_wake(adapter, *, text, session_id):
        posts.append(session_id)

    monkeypatch.setattr(wake, "deliver_wake", fake_deliver_wake)
    api = _api_adapter()
    runner = _runner({Platform.API_SERVER: api})

    assert await runner._deliver_completion_notification("done", durable_event)
    assert posts == ["raw-cli-session"]
    assert _row()[0:2] == ("delivered", 1)


@pytest.mark.asyncio
async def test_structured_parent_completion_still_acks(durable_event):
    from tools import async_delegation

    durable_event.update({
        "delegation_id": "telegram-test",
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "parent_session_id": "live-parent",
    })
    async_delegation._persist_dispatch({
        "delegation_id": "telegram-test", "session_key": durable_event["session_key"],
        "origin_ui_session_id": "", "parent_session_id": "live-parent", "dispatched_at": 1.0,
    })
    async_delegation._persist_completion(durable_event, durable_event)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner({Platform.TELEGRAM: adapter})
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(return_value={"ended_at": None}),
    )

    assert await runner._deliver_completion_notification("done", durable_event)
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_adapter_exception_releases_claim(durable_event):
    from tools import async_delegation

    durable_event.update({
        "delegation_id": "exception-test", "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram", "chat_type": "dm", "chat_id": "123",
    })
    async_delegation._persist_dispatch({
        "delegation_id": "exception-test", "session_key": durable_event["session_key"],
        "origin_ui_session_id": "", "parent_session_id": None, "dispatched_at": 1.0,
    })
    async_delegation._persist_completion(durable_event, durable_event)
    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=RuntimeError("temporary")))
    runner = _runner({Platform.TELEGRAM: adapter})

    assert await runner._deliver_completion_notification("done", durable_event) is False
    with async_delegation._connect() as conn:
        row = conn.execute(
            "SELECT delivery_state, delivery_attempts, delivery_claim FROM async_delegations "
            "WHERE delegation_id='exception-test'"
        ).fetchone()
    assert row == ("pending", 1, None)


@pytest.mark.asyncio
async def test_terminal_parent_is_claimed_and_dropped(durable_event):
    from tools import async_delegation

    durable_event.update({
        "delegation_id": "terminal-test", "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram", "chat_type": "dm", "chat_id": "123",
        "parent_session_id": "gone-parent",
    })
    async_delegation._persist_dispatch({
        "delegation_id": "terminal-test", "session_key": durable_event["session_key"],
        "origin_ui_session_id": "", "parent_session_id": "gone-parent", "dispatched_at": 1.0,
    })
    async_delegation._persist_completion(durable_event, durable_event)
    runner = _runner({Platform.TELEGRAM: SimpleNamespace(handle_message=AsyncMock())})
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value=None))

    assert await runner._deliver_completion_notification("done", durable_event) is None
    with async_delegation._connect() as conn:
        row = conn.execute(
            "SELECT delivery_state, delivery_attempts FROM async_delegations "
            "WHERE delegation_id='terminal-test'"
        ).fetchone()
    assert row == ("dropped", 1)


@pytest.mark.asyncio
async def test_ownership_handoff_leaves_row_for_api_server(durable_event, monkeypatch):
    from gateway import wake

    posts = []
    async def fake_deliver_wake(adapter, *, text, session_id):
        posts.append(session_id)

    monkeypatch.setattr(wake, "deliver_wake", fake_deliver_wake)
    telegram = _runner({Platform.TELEGRAM: SimpleNamespace(handle_message=AsyncMock())})
    assert await telegram._deliver_completion_notification("done", durable_event) is None
    assert _row() == ("pending", 0, None)

    api = _runner({Platform.API_SERVER: _api_adapter()})
    assert await api._deliver_completion_notification("done", durable_event)
    assert posts == ["raw-cli-session"]
    assert _row()[0] == "delivered"
