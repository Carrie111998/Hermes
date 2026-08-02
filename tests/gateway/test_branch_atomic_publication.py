from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import MessageEvent
from gateway.session import Platform, SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_state import AsyncSessionDB, SessionDB


@pytest.mark.parametrize("mutates_route", [False, True])
@pytest.mark.asyncio
async def test_branch_switch_failure_reports_committed_child(
    tmp_path, monkeypatch, mutates_route
):
    db = SessionDB(tmp_path / "gateway-branch.db")
    parent_id = db.create_session("parent", "telegram", model="test-model")
    db.replace_messages(parent_id, [{"role": "user", "content": "preserve"}])
    db.set_session_title(parent_id, "Parent")

    current = SimpleNamespace(session_id=parent_id)

    class Store:
        async def get_or_create_session(self, _source):
            return current

        async def load_transcript(self, _session_id):
            return db.get_messages_as_conversation(parent_id)

        async def switch_session(self, _session_key, child_id):
            if mutates_route:
                current.session_id = child_id
            raise RuntimeError("route store failed")

    runner: Any = SimpleNamespace()
    runner._session_db = AsyncSessionDB(db)
    runner.async_session_store = Store()
    runner.config = {"model": {"default": "test-model"}}
    runner._session_key_for_source = lambda _source: "telegram:key"
    runner._clear_session_boundary_security_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    monkeypatch.setattr(
        "gateway.slash_commands.t",
        lambda key, **kwargs: f"{key}|{kwargs}",
    )

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1")
    result = await GatewaySlashCommandsMixin._handle_branch_command(
        cast(GatewaySlashCommandsMixin, runner),
        MessageEvent(text="/branch Child", source=source),
    )

    children = [
        row for row in db.list_sessions_rich(limit=20) if row["id"] != parent_id
    ]
    assert len(children) == 1
    child_id = children[0]["id"]
    assert child_id in result
    assert "gateway.branch.branched" in result
    assert db.get_messages_as_conversation(child_id)[0]["content"] == "preserve"
    if mutates_route:
        runner._clear_session_boundary_security_state.assert_called_once_with(
            "telegram:key"
        )
        runner._evict_cached_agent.assert_called_once_with("telegram:key")
    else:
        runner._clear_session_boundary_security_state.assert_not_called()
        runner._evict_cached_agent.assert_not_called()
    db.close()
